from fastapi import APIRouter, Depends, HTTPException

from src.schemas.claims import DocumentFollowupContact, DocumentReceiptCreate, DocumentReceiptRevoke
from src.services.document_triage_service import (
    TRIAGE_ROLES, latest_followup, list_document_triage, log_followup,
    receipt_rows, record_receipt, revoke_receipt,
)
from src.utils.auth import require_roles
from src.utils.db import get_conn

router = APIRouter(tags=["document triage"])
triage_staff = require_roles(*TRIAGE_ROLES)


def fail(exc):
    if isinstance(exc, LookupError):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


@router.get("/document-triage")
def queue(user=Depends(triage_staff)):
    return list_document_triage()


@router.post("/claims/{claim_id}/document-followup")
def record_contact(claim_id: int, payload: DocumentFollowupContact, user=Depends(triage_staff)):
    try:
        return log_followup(claim_id, payload, user["id"])
    except (LookupError, PermissionError, ValueError) as exc:
        fail(exc)


@router.get("/claims/{claim_id}/document-receipts")
def receipts(claim_id: int, receipt_no: str | None = None, user=Depends(triage_staff)):
    with get_conn() as conn:
        rows = receipt_rows(conn, claim_id, receipt_no or None)
        followup = latest_followup(conn, claim_id)
    return {"items": rows, "latest_batch": followup}


@router.post("/claims/{claim_id}/document-receipts")
def receive_documents(claim_id: int, payload: DocumentReceiptCreate, user=Depends(triage_staff)):
    try:
        return record_receipt(claim_id, payload, user["id"])
    except (LookupError, PermissionError, ValueError) as exc:
        fail(exc)


@router.post("/claims/{claim_id}/document-receipts/{receipt_id}/revoke")
def revoke_documents(claim_id: int, receipt_id: int, payload: DocumentReceiptRevoke, user=Depends(triage_staff)):
    try:
        return revoke_receipt(claim_id, receipt_id, payload.note, user["id"])
    except (LookupError, PermissionError, ValueError) as exc:
        fail(exc)

