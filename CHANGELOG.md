# Changelog

## Unreleased

Nothing yet.

## 0.1.0

First version of the platform that replaces the per-source Lambda handlers.

### Ingestion

- Jurisdiction registry covering Congress and twenty states, with per-source
  politeness policies, session patterns and fetch strategies.
- Scrapy adapters for California, Texas, New York, Illinois, Ohio and the
  federal bulk feed, over a shared spider base that owns session resolution,
  the frontier and the request metadata every middleware depends on.
- Selenium fetching for the postback and single-page portals, behind a pooled,
  recycling driver and a session abstraction that hands plain markup back to
  the same parsers the HTTP adapters use.
- Absolute per-host request budgets enforced by a token bucket shared across
  spiders and the browser fetcher, with off-hours allowances.
- Every successful fetch archived verbatim before anything parses it.

### Transform and quality

- One canonical document schema, with status derived from the docket rather
  than read from the portal.
- Sponsor, subject and date normalisation shared across every adapter.
- Change detection that distinguishes a new document, a text revision, a
  substitution, a status move and a metadata-only edit.
- Nine batch quality checks behind a gate with a configurable error budget and
  per-check waivers.

### Orchestration

- Watermarks with a safety lag, optimistic concurrency and a no-rewind rule.
- Resumable checkpoints with a versioned payload.
- Backfill planning that chunks by date, orders newest first, and interleaves
  across jurisdictions.
- Run bookkeeping, freshness alerting and a scheduler that treats browser
  slots as the scarce resource.

### Delivery

- Airflow DAGs for incremental ingest, backfill, freshness monitoring and a
  reindex behind an alias flip.
- Numbered SQL migrations applied by a small runner with a checksum ledger.
- Multi-stage images for the worker and the scheduler, a local stack, and CI
  covering lint, types, tests, DAG conventions, image build and a daily live
  portal shape check.
