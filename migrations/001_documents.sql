-- Core document tables.
--
-- document_id is a UUID derived from jurisdiction, session and identifier, so
-- an upsert from any adapter lands on the same row.

CREATE TABLE IF NOT EXISTS documents (
    document_id           UUID PRIMARY KEY,
    jurisdiction          TEXT        NOT NULL,
    session               TEXT        NOT NULL,
    identifier            TEXT        NOT NULL,
    citation              TEXT        NOT NULL,
    title                 TEXT        NOT NULL DEFAULT '',
    summary               TEXT        NOT NULL DEFAULT '',
    kind                  TEXT        NOT NULL DEFAULT 'bill',
    chamber               TEXT        NOT NULL DEFAULT 'unknown',
    status                TEXT        NOT NULL DEFAULT 'unknown',
    introduced_on         DATE,
    last_action_on        DATE,
    metadata_fingerprint  TEXT        NOT NULL DEFAULT '',
    source_url            TEXT,
    observed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    extras                JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT documents_citation_unique UNIQUE (citation),
    CONSTRAINT documents_dates_ordered
        CHECK (last_action_on IS NULL OR introduced_on IS NULL OR last_action_on >= introduced_on)
);

CREATE INDEX IF NOT EXISTS documents_jurisdiction_session_idx
    ON documents (jurisdiction, session);
CREATE INDEX IF NOT EXISTS documents_status_idx
    ON documents (status) WHERE status <> 'unknown';
CREATE INDEX IF NOT EXISTS documents_last_action_idx
    ON documents (last_action_on DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS documents_observed_idx
    ON documents (observed_at DESC);

CREATE TABLE IF NOT EXISTS document_versions (
    document_id    UUID        NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    semantic_hash  TEXT        NOT NULL,
    byte_hash      TEXT        NOT NULL,
    label          TEXT        NOT NULL DEFAULT '',
    published_on   DATE,
    length         INTEGER     NOT NULL DEFAULT 0,
    text           TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, semantic_hash)
);

CREATE INDEX IF NOT EXISTS document_versions_published_idx
    ON document_versions (document_id, published_on DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS document_actions (
    document_id        UUID        NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    occurred_on        DATE        NOT NULL,
    description_hash   TEXT        NOT NULL,
    sequence           INTEGER     NOT NULL DEFAULT 0,
    description        TEXT        NOT NULL,
    kind               TEXT        NOT NULL DEFAULT 'unknown',
    chamber            TEXT        NOT NULL DEFAULT 'unknown',
    committee          TEXT,
    resulting_status   TEXT,
    PRIMARY KEY (document_id, occurred_on, description_hash)
);

CREATE INDEX IF NOT EXISTS document_actions_order_idx
    ON document_actions (document_id, occurred_on, sequence);
