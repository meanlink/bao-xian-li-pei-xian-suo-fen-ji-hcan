CREATE TABLE IF NOT EXISTS document_followups (
    id SERIAL PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    channel TEXT NOT NULL CHECK (channel IN ('phone', 'sms', 'email')),
    contacted_on DATE NOT NULL,
    due_on DATE NOT NULL CHECK (due_on >= contacted_on),
    contact_note TEXT NOT NULL CHECK (char_length(btrim(contact_note)) BETWEEN 6 AND 500),
    requested_documents TEXT[] NOT NULL CHECK (cardinality(requested_documents) > 0),
    recorded_by INTEGER NOT NULL REFERENCES users(id),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_by INTEGER REFERENCES users(id),
    completed_at TIMESTAMPTZ,
    CHECK ((completed_by IS NULL) = (completed_at IS NULL))
);

-- 唯一性约束（每个案件同时只有一条生效催件）由 004_document_receipts.sql 建立，
-- 那里同时考虑“被撤销作废”的批次。
CREATE INDEX IF NOT EXISTS document_followups_claim_history
    ON document_followups (claim_id, id DESC);
