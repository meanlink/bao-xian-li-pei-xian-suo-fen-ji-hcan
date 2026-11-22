-- 补件收件回执：每次收件单独编号，归属到某一批催件，支持撤销误验。
-- 资料核验不再覆盖整份已收清单，而是追加收件记录；同一批催件下的历史收件全部保留。

-- 受理时已经收到的材料（通常早于任何催件），作为收件台账的基线。
ALTER TABLE claims ADD COLUMN IF NOT EXISTS intake_received_documents JSONB;
UPDATE claims SET intake_received_documents = received_documents WHERE intake_received_documents IS NULL;
ALTER TABLE claims ALTER COLUMN intake_received_documents SET DEFAULT '[]'::jsonb;
ALTER TABLE claims ALTER COLUMN intake_received_documents SET NOT NULL;

-- 催件批次序号，每个案件从 1 开始递增。
ALTER TABLE document_followups ADD COLUMN IF NOT EXISTS batch_no INTEGER;

UPDATE document_followups f
SET batch_no = seq.number
FROM (
    SELECT id, row_number() OVER (PARTITION BY claim_id ORDER BY id) AS number
    FROM document_followups
) seq
WHERE f.id = seq.id AND f.batch_no IS NULL;

ALTER TABLE document_followups ALTER COLUMN batch_no SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS document_followups_claim_batch
    ON document_followups (claim_id, batch_no);

-- 同一案件同时只保留一条生效催件；被撤销误验后作废的批次不再占用这个位置。
DROP INDEX IF EXISTS document_followups_one_open_per_claim;
CREATE UNIQUE INDEX IF NOT EXISTS document_followups_one_active_per_claim
    ON document_followups (claim_id) WHERE completed_at IS NULL AND superseded_at IS NULL;

CREATE TABLE IF NOT EXISTS document_receipts (
    id SERIAL PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    receipt_no TEXT NOT NULL CHECK (char_length(btrim(receipt_no)) BETWEEN 3 AND 64),
    followup_id INTEGER REFERENCES document_followups(id) ON DELETE SET NULL,
    documents TEXT[] NOT NULL CHECK (cardinality(documents) > 0),
    received_on DATE NOT NULL,
    recorded_by INTEGER NOT NULL REFERENCES users(id),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    revoked_by INTEGER REFERENCES users(id),
    revoked_at TIMESTAMPTZ,
    revocation_note TEXT CHECK (revocation_note IS NULL OR char_length(btrim(revocation_note)) BETWEEN 6 AND 500),
    CHECK ((revoked_by IS NULL) = (revoked_at IS NULL)),
    CHECK ((revoked_at IS NULL) = (revocation_note IS NULL)),
    UNIQUE (claim_id, receipt_no)
);

CREATE INDEX IF NOT EXISTS document_receipts_claim_history
    ON document_receipts (claim_id, id DESC);
CREATE INDEX IF NOT EXISTS document_receipts_followup
    ON document_receipts (followup_id, id);
