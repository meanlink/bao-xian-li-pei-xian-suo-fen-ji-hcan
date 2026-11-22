import hashlib
import json

from src.utils.audit import record_in_transaction
from src.utils.db import get_conn
from src.utils.serialization import row_claim

INTAKE_STATES = ("intake", "initial_review")
TRIAGE_ROLES = ("intake_agent", "risk_manager")
BUCKETS = ("overdue", "due_today", "unscheduled", "scheduled")


def as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return json.loads(value)
    return list(value)


def missing_documents(claim, received=None):
    if received is None:
        received = claim["received_documents"]
    return sorted(set(as_list(claim["required_documents"])) - set(as_list(received)))


def business_date(conn):
    return conn.execute("SELECT (now() AT TIME ZONE 'Asia/Shanghai')::date AS today").fetchone()["today"]


def claim_batches(conn, claim_id):
    return conn.execute(
        """SELECT f.*, recorder.display_name AS recorded_by_name,
                  verifier.display_name AS completed_by_name,
                  superseder.display_name AS superseded_by_name
           FROM document_followups f
           JOIN users recorder ON recorder.id=f.recorded_by
           LEFT JOIN users verifier ON verifier.id=f.completed_by
           LEFT JOIN users superseder ON superseder.id=f.superseded_by
           WHERE f.claim_id=%s ORDER BY f.batch_no""",
        (claim_id,),
    ).fetchall()


def latest_batch(conn, claim_id):
    return conn.execute(
        """SELECT f.*, recorder.display_name AS recorded_by_name,
                  verifier.display_name AS completed_by_name
           FROM document_followups f
           JOIN users recorder ON recorder.id=f.recorded_by
           LEFT JOIN users verifier ON verifier.id=f.completed_by
           WHERE f.claim_id=%s ORDER BY f.batch_no DESC LIMIT 1""",
        (claim_id,),
    ).fetchone()


def active_batch(conn, claim_id):
    return conn.execute(
        """SELECT f.*, recorder.display_name AS recorded_by_name
           FROM document_followups f
           JOIN users recorder ON recorder.id=f.recorded_by
           WHERE f.claim_id=%s AND f.completed_at IS NULL AND f.superseded_at IS NULL
           ORDER BY f.batch_no DESC LIMIT 1""",
        (claim_id,),
    ).fetchone()


def receipt_rows(conn, claim_id, receipt_no=None):
    return conn.execute(
        """SELECT r.*, recorder.display_name AS recorded_by_name,
                  revoker.display_name AS revoked_by_name,
                  f.batch_no, f.contacted_on AS batch_contacted_on,
                  f.due_on AS batch_due_on
           FROM document_receipts r
           JOIN users recorder ON recorder.id=r.recorded_by
           LEFT JOIN users revoker ON revoker.id=r.revoked_by
           LEFT JOIN document_followups f ON f.id=r.followup_id
           WHERE r.claim_id=%s AND (%s::text IS NULL OR r.receipt_no=%s)
           ORDER BY r.id""",
        (claim_id, receipt_no, receipt_no),
    ).fetchall()


def compute_document_state(conn, claim):
    """Derive the document checklist from the append-only receipt ledger.

    Materials requested by the latest follow-up batch are only cleared by receipts
    attributed to that batch.  A late receipt that stays attached to an older batch
    therefore remains on record but cannot offset the new batch's missing items.
    Everything outside the latest batch keeps counting from any non-revoked receipt.
    """
    claim_id = claim["id"]
    required = as_list(claim["required_documents"])
    baseline = set(as_list(claim.get("intake_received_documents")))
    latest = latest_batch(conn, claim_id)
    latest_id = latest["id"] if latest else None
    latest_requested = set(as_list(latest["requested_documents"])) if latest else set()
    receipts = conn.execute(
        "SELECT documents, followup_id FROM document_receipts WHERE claim_id=%s AND revoked_at IS NULL",
        (claim_id,),
    ).fetchall()
    received_ever = set(baseline)
    received_latest = set(baseline)
    for receipt in receipts:
        received_ever.update(receipt["documents"])
        if receipt["followup_id"] == latest_id or receipt["followup_id"] is None:
            received_latest.update(receipt["documents"])
    effective = set()
    for doc in required:
        if doc in latest_requested and latest_id is not None:
            if doc in received_latest:
                effective.add(doc)
        elif doc in received_ever:
            effective.add(doc)
    missing = sorted(set(required) - effective)
    return {
        "received_documents": sorted(effective),
        "missing_documents": missing,
        "received_ever": sorted(received_ever),
        "latest_batch": latest,
        "latest_batch_id": latest_id,
    }


def resolve_receipt_batch(conn, claim, received_on, followup_id=None):
    """Pick the follow-up batch an incoming receipt belongs to.

    An explicit batch always wins.  Otherwise a receipt dated before the active
    batch's contact date is attributed to the batch that was in force on that date,
    so a late shipment stays with the original chase batch instead of offsetting a
    newer batch's missing items.
    """
    if followup_id is not None:
        batch = conn.execute(
            "SELECT * FROM document_followups WHERE id=%s AND claim_id=%s",
            (followup_id, claim["id"]),
        ).fetchone()
        if not batch:
            raise LookupError("催件批次不存在")
        return batch
    active = active_batch(conn, claim["id"])
    if active and received_on >= active["contacted_on"]:
        return active
    batch = conn.execute(
        """SELECT * FROM document_followups
           WHERE claim_id=%s AND contacted_on <= %s
           ORDER BY batch_no DESC LIMIT 1""",
        (claim["id"], received_on),
    ).fetchone()
    if batch:
        return batch
    return active or latest_batch(conn, claim["id"])


def auto_receipt_no(claim, documents, received_on):
    material = "|".join(sorted(set(documents))) + f"|{received_on.isoformat()}"
    digest = hashlib.sha256(f"{claim['id']}|{material}".encode("utf-8")).hexdigest()[:8].upper()
    return f"RCV-{claim['id']:04d}-{digest}"


def apply_document_state(conn, claim, user_id, action, detail=None):
    """Recompute the checklist from receipts, persist it, and close the batch when clear."""
    state = compute_document_state(conn, claim)
    missing = state["missing_documents"]
    if missing and claim["status"] not in INTAKE_STATES:
        raise PermissionError("案件已进入风险分级或后续阶段，不能将资料改回缺项")
    documents_status = "missing" if missing else "complete"
    next_status = "initial_review" if not missing and claim["status"] == "intake" else claim["status"]
    row = conn.execute(
        """UPDATE claims SET received_documents=%s, missing_documents=%s,
               documents_status=%s, status=%s, updated_at=now() WHERE id=%s RETURNING *""",
        (json.dumps(state["received_documents"], ensure_ascii=False),
         json.dumps(missing, ensure_ascii=False), documents_status, next_status, claim["id"]),
    ).fetchone()
    record_in_transaction(conn, user_id, action, claim["id"], {
        **(detail or {}),
        "missing": missing,
        "received_documents": state["received_documents"],
        "documents_status": documents_status,
    })
    if not missing:
        complete_followup(conn, claim["id"], user_id)
    return row


def unique_receipt_no(conn, claim_id, base):
    candidate = base
    suffix = 2
    while conn.execute(
        "SELECT 1 FROM document_receipts WHERE claim_id=%s AND receipt_no=%s",
        (claim_id, candidate),
    ).fetchone():
        candidate = f"{base}-R{suffix}"
        suffix += 1
    return candidate


def _record_receipt_in_conn(conn, claim, documents, received_on, receipt_no, followup_id, user_id,
                            explicit_no=True):
    """Insert one receipt and recompute the checklist, assuming the claim is locked.

    An explicit receipt number identifies one shipment: retrying it never books a
    second receipt and returns the original — even a revoked one — so the historical
    receipt stays queryable.  A generated number that collides with a revoked receipt
    means the material is being re-verified after a revocation, so it gets a suffix
    and a receipt of its own.
    """
    existing = conn.execute(
        "SELECT * FROM document_receipts WHERE claim_id=%s AND receipt_no=%s",
        (claim["id"], receipt_no),
    ).fetchone()
    if existing:
        if existing["revoked_at"] is None:
            state = compute_document_state(conn, claim)
            if not state["missing_documents"]:
                complete_followup(conn, claim["id"], user_id)
            return claim, existing, False
        if explicit_no:
            return claim, existing, False
        receipt_no = unique_receipt_no(conn, claim["id"], receipt_no)

    batch = resolve_receipt_batch(conn, claim, received_on, followup_id)
    receipt = conn.execute(
        """INSERT INTO document_receipts
               (claim_id, receipt_no, followup_id, documents, received_on, recorded_by)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
        (claim["id"], receipt_no, batch["id"] if batch else None,
         documents, received_on, user_id),
    ).fetchone()
    row = apply_document_state(conn, claim, user_id, "document_receipt_recorded", {
        "receipt_id": receipt["id"], "receipt_no": receipt["receipt_no"],
        "documents": documents, "received_on": received_on.isoformat(),
        "followup_id": receipt["followup_id"],
        "batch_no": batch["batch_no"] if batch else None,
    })
    return row, receipt, True


def record_receipt(claim_id, payload, user_id):
    with get_conn() as conn:
        claim = conn.execute("SELECT * FROM claims WHERE id=%s FOR UPDATE", (claim_id,)).fetchone()
        if not claim:
            raise LookupError("案件不存在")
        if claim["status"] not in INTAKE_STATES:
            raise PermissionError("仅受理或初审阶段的案件可以核验资料")
        if payload.received_on > business_date(conn):
            raise ValueError("收件日期不能晚于当前工作日")

        documents = sorted(set(payload.documents))
        required = set(as_list(claim["required_documents"]))
        if not set(documents) <= required:
            raise ValueError("收件材料不在本案所需资料清单内")
        # A generated number is derived from the claim, the material set and the
        # receipt date, so a retry maps back to the same receipt instead of a new one.
        explicit_no = bool(payload.receipt_no)
        receipt_no = payload.receipt_no or auto_receipt_no(claim, documents, payload.received_on)
        row, receipt, created = _record_receipt_in_conn(
            conn, claim, documents, payload.received_on, receipt_no, payload.followup_id, user_id,
            explicit_no=explicit_no,
        )
    return {"receipt": receipt, "idempotent": not created, "claim": row_claim(row)}


def revoke_receipt(claim_id, receipt_id, note, user_id):
    with get_conn() as conn:
        claim = conn.execute("SELECT * FROM claims WHERE id=%s FOR UPDATE", (claim_id,)).fetchone()
        if not claim:
            raise LookupError("案件不存在")
        receipt = conn.execute(
            "SELECT * FROM document_receipts WHERE id=%s AND claim_id=%s",
            (receipt_id, claim_id),
        ).fetchone()
        if not receipt:
            raise LookupError("收件记录不存在")
        if receipt["revoked_at"] is not None:
            raise PermissionError("该收件记录已撤销")
        if claim["status"] not in INTAKE_STATES:
            raise PermissionError("风险分级后不能撤销资料核验")

        latest = latest_batch(conn, claim_id)
        revoked = conn.execute(
            """UPDATE document_receipts
               SET revoked_by=%s, revoked_at=now(), revocation_note=%s
               WHERE id=%s RETURNING *""",
            (user_id, note, receipt_id),
        ).fetchone()
        # Revoking a receipt from the newest chase batch retires that batch, even if it
        # had already been closed: the re-chase must open a new batch, otherwise the
        # original batch's late receipts could offset the re-chase's missing items.
        # Completion history is kept; only the batch's "active" slot is released.
        superseded = None
        if latest and receipt["followup_id"] == latest["id"] and not latest["superseded_at"]:
            superseded = conn.execute(
                """UPDATE document_followups
                   SET superseded_at=now(), superseded_by=%s
                   WHERE id=%s RETURNING batch_no""",
                (user_id, latest["id"]),
            ).fetchone()
        row = apply_document_state(conn, claim, user_id, "document_receipt_revoked", {
            "receipt_id": receipt_id, "receipt_no": receipt["receipt_no"],
            "documents": as_list(receipt["documents"]),
            "followup_id": receipt["followup_id"],
            "batch_no": superseded["batch_no"] if superseded else None,
            "revocation_note": note,
        })
    return {"receipt": revoked, "claim": row_claim(row)}


def list_document_triage():
    with get_conn() as conn:
        today = business_date(conn)
        claims = conn.execute(
            """SELECT c.id, c.claim_no, c.customer_name, c.claimant_phone, c.loss_amount,
                      c.required_documents, c.intake_received_documents
               FROM claims c
               WHERE c.status = ANY(%s)
               ORDER BY c.id""",
            (list(INTAKE_STATES),),
        ).fetchall()
        data = []
        for claim in claims:
            state = compute_document_state(conn, claim)
            if not state["missing_documents"]:
                continue
            data.append({
                "claim": claim,
                "state": state,
                "active": active_batch(conn, claim["id"]),
                "batches": claim_batches(conn, claim["id"]),
                "receipts": receipt_rows(conn, claim["id"]),
            })

    today_iso = today.isoformat()
    counts = dict.fromkeys(BUCKETS, 0)
    items = []
    for entry in data:
        claim = entry["claim"]
        active = entry["active"]
        due = active["due_on"] if active else None
        if due is None:
            bucket = "unscheduled"
        elif due < today_iso:
            bucket = "overdue"
        elif due == today_iso:
            bucket = "due_today"
        else:
            bucket = "scheduled"
        counts[bucket] += 1
        items.append({
            "claim_id": claim["id"],
            "claim_no": claim["claim_no"],
            "customer_name": claim["customer_name"],
            "claimant_phone": claim["claimant_phone"],
            "loss_amount": claim["loss_amount"],
            "required_documents": as_list(claim["required_documents"]),
            "received_documents": entry["state"]["received_documents"],
            "missing_documents": entry["state"]["missing_documents"],
            "bucket": bucket,
            "followup": active,
            "latest_batch": entry["state"]["latest_batch"],
            "batches": entry["batches"],
            "receipts": entry["receipts"],
            "recorded_by_name": active["recorded_by_name"] if active else None,
        })
    items.sort(key=lambda item: (
        BUCKETS.index(item["bucket"]),
        item["followup"]["due_on"] if item["followup"] else "",
        -item["loss_amount"],
        item["claim_id"],
    ))
    return {"business_date": today, "counts": counts, "items": items}


def latest_followup(conn, claim_id):
    return latest_batch(conn, claim_id)


def log_followup(claim_id, payload, user_id):
    with get_conn() as conn:
        # Verification takes the same claim lock before changing the document checklist.
        claim = conn.execute("SELECT * FROM claims WHERE id=%s FOR UPDATE", (claim_id,)).fetchone()
        if not claim:
            raise LookupError("案件不存在")
        state = compute_document_state(conn, claim)
        missing = state["missing_documents"]
        if claim["status"] not in INTAKE_STATES or not missing:
            raise PermissionError("仅受理或初审阶段且仍有缺项的案件可以登记补件联系")
        if set(payload.expected_missing_documents) != set(missing):
            raise PermissionError("缺项清单已更新，请刷新队列后重试")
        if payload.contacted_on > business_date(conn):
            raise ValueError("联系日期不能晚于当前工作日")

        current = conn.execute(
            "SELECT id, revision, contacted_on, batch_no FROM document_followups WHERE claim_id=%s AND completed_at IS NULL AND superseded_at IS NULL",
            (claim_id,),
        ).fetchone()
        version = (current["id"], current["revision"]) if current else (None, 0)
        if (payload.followup_id, payload.expected_revision) != version:
            raise PermissionError("补件记录已更新，请刷新队列后重试")
        latest = latest_batch(conn, claim_id)
        if current and payload.contacted_on < current["contacted_on"]:
            raise ValueError("再次联系日期不能早于上次联系日期")
        if not current and latest and payload.contacted_on < latest["contacted_on"]:
            raise ValueError("重新催件的联系日期不能早于上批联系日期")

        values = (payload.channel, payload.contacted_on, payload.due_on, payload.contact_note, missing, user_id)
        if current:
            row = conn.execute(
                """UPDATE document_followups SET revision=revision+1, channel=%s, contacted_on=%s,
                          due_on=%s, contact_note=%s, requested_documents=%s, recorded_by=%s, recorded_at=now()
                   WHERE id=%s RETURNING *""",
                (*values, current["id"]),
            ).fetchone()
        else:
            row = conn.execute(
                """INSERT INTO document_followups
                       (channel, contacted_on, due_on, contact_note, requested_documents,
                        recorded_by, claim_id, batch_no)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (*values, claim_id, (latest["batch_no"] if latest else 0) + 1),
            ).fetchone()
        record_in_transaction(conn, user_id, "document_followup_recorded", claim_id, {
            "followup_id": row["id"], "batch_no": row["batch_no"], "revision": row["revision"],
            "channel": row["channel"], "contacted_on": row["contacted_on"].isoformat(),
            "due_on": row["due_on"].isoformat(), "requested_documents": missing,
            "contact_note": row["contact_note"],
        })
    return row


def complete_followup(conn, claim_id, user_id):
    followup = conn.execute(
        """UPDATE document_followups SET completed_at=now(), completed_by=%s
           WHERE claim_id=%s AND completed_at IS NULL AND superseded_at IS NULL
           RETURNING id, revision, batch_no""",
        (user_id, claim_id),
    ).fetchone()
    if followup:
        record_in_transaction(conn, user_id, "document_followup_completed", claim_id, {
            "followup_id": followup["id"], "batch_no": followup["batch_no"],
            "revision": followup["revision"],
        })
