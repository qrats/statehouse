-- Operational tables: watermarks, checkpoints, runs and the change feed.

CREATE TABLE IF NOT EXISTS watermarks (
    jurisdiction      TEXT        NOT NULL,
    stream            TEXT        NOT NULL DEFAULT 'default',
    position          TEXT        NOT NULL DEFAULT '',
    observed_through  TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    revision          INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (jurisdiction, stream),
    CONSTRAINT watermarks_revision_positive CHECK (revision >= 0)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    jurisdiction TEXT        NOT NULL,
    stream       TEXT        NOT NULL DEFAULT 'default',
    payload      JSONB       NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, stream)
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id             TEXT        PRIMARY KEY,
    jurisdiction       TEXT        NOT NULL,
    state              TEXT        NOT NULL DEFAULT 'pending',
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ,
    documents_seen     INTEGER     NOT NULL DEFAULT 0,
    documents_written  INTEGER     NOT NULL DEFAULT 0,
    documents_skipped  INTEGER     NOT NULL DEFAULT 0,
    fetch_failures     INTEGER     NOT NULL DEFAULT 0,
    error              JSONB,
    metadata           JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ingest_runs_jurisdiction_idx
    ON ingest_runs (jurisdiction, started_at DESC);
CREATE INDEX IF NOT EXISTS ingest_runs_failed_idx
    ON ingest_runs (jurisdiction, started_at DESC) WHERE state = 'failed';

CREATE TABLE IF NOT EXISTS document_changes (
    document_id      UUID        NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
    observed_at      TIMESTAMPTZ NOT NULL,
    kind             TEXT        NOT NULL,
    fields           TEXT[]      NOT NULL DEFAULT '{}',
    previous_status  TEXT,
    current_status   TEXT,
    text_similarity  DOUBLE PRECISION,
    new_actions      INTEGER     NOT NULL DEFAULT 0,
    notes            TEXT[]      NOT NULL DEFAULT '{}',
    PRIMARY KEY (document_id, observed_at)
);

CREATE INDEX IF NOT EXISTS document_changes_feed_idx
    ON document_changes (observed_at DESC)
    WHERE kind NOT IN ('unchanged', 'metadata_only');

CREATE TABLE IF NOT EXISTS quality_findings (
    run_id     TEXT        NOT NULL,
    check_name TEXT        NOT NULL,
    severity   TEXT        NOT NULL,
    subject    TEXT,
    message    TEXT        NOT NULL,
    observed   TEXT,
    expected   TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS quality_findings_run_idx ON quality_findings (run_id);
CREATE INDEX IF NOT EXISTS quality_findings_blocking_idx
    ON quality_findings (severity, created_at DESC)
    WHERE severity IN ('error', 'critical');
