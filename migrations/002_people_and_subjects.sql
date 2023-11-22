-- Sponsors and subjects.
--
-- Both are replaced wholesale on each write rather than merged, so a sponsor
-- withdrawn upstream disappears here too.

CREATE TABLE IF NOT EXISTS document_sponsors (
    document_id  UUID    NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    role         TEXT    NOT NULL DEFAULT 'unknown',
    party        TEXT,
    district     TEXT,
    chamber      TEXT    NOT NULL DEFAULT 'unknown',
    position     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (document_id, name)
);

CREATE INDEX IF NOT EXISTS document_sponsors_name_idx ON document_sponsors (lower(name));
CREATE INDEX IF NOT EXISTS document_sponsors_primary_idx
    ON document_sponsors (document_id) WHERE role = 'primary';

CREATE TABLE IF NOT EXISTS document_subjects (
    document_id UUID NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    subject     TEXT NOT NULL,
    PRIMARY KEY (document_id, subject)
);

CREATE INDEX IF NOT EXISTS document_subjects_subject_idx ON document_subjects (subject);

-- Unmapped source vocabulary, kept so the canonical list can be extended
-- deliberately rather than by guessing.
CREATE TABLE IF NOT EXISTS subject_vocabulary_gaps (
    jurisdiction  TEXT        NOT NULL,
    raw_subject   TEXT        NOT NULL,
    occurrences   INTEGER     NOT NULL DEFAULT 1,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, raw_subject)
);
