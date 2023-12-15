# statehouse

Ingests bills, resolutions and regulatory documents from state legislatures and
Congress, normalises them into one schema, and publishes a change feed and a
search index that clients query by keyword, jurisdiction, subject and date.

The hard part is not any single portal. It is that there are fifty of them,
each with its own idea of what a session is, its own docket vocabulary, its own
tolerance for being scraped, and its own redesign schedule. Everything here is
shaped by that: adapters are thin, the normalisation is shared, and the
platform assumes any given source will break this quarter.

## How it fits together

```
portals ──► scraping ──► extract ──► transform ──► quality ──► load
              │            │            │            │          ├─► warehouse (Postgres)
              │            │            │            │          ├─► raw lake (S3)
              │            │            │            │          └─► search index
              └── raw archive ──────────┘            └─► findings, gate

orchestration: watermarks, checkpoints, backfill planning, run bookkeeping
observability: structured logs, metrics, freshness alerts
```

Four fetch strategies, chosen per jurisdiction in the registry:

- **HTTP** — Scrapy against a server-rendered portal. Cheapest, most common.
- **API** — a documented JSON endpoint. Rare and wonderful.
- **Browser** — Selenium, for the postback grids and single-page apps that
  serve nothing useful without JavaScript. Expensive, so the pool is small and
  the scheduler treats browser slots as the scarce resource.
- **Bulk** — Congress publishes daily XML drops; there is no reason to scrape.

## Layout

| Path | What lives there |
| --- | --- |
| `src/statehouse/core` | Models, enums, errors, identity, hashing, clock |
| `src/statehouse/config` | Runtime settings and the jurisdiction registry |
| `src/statehouse/scraping` | Spiders, middlewares, pipelines, throttle, frontier |
| `src/statehouse/browser` | Driver construction, pooling, portal sessions |
| `src/statehouse/extract` | Tables and text out of fetched bytes |
| `src/statehouse/transform` | Normalisation, status derivation, dedupe, diffing |
| `src/statehouse/quality` | Checks, the gate, reporting |
| `src/statehouse/orchestration` | Watermarks, checkpoints, backfill, runs, scheduling |
| `src/statehouse/load` | Warehouse upserts, raw lake, search indexing |
| `airflow/dags` | Incremental ingest, backfill, freshness monitoring, reindex |
| `migrations` | Numbered SQL, applied by `scripts/migrate.py` |

The pure-Python layers — `core`, `transform`, `quality`, `orchestration` —
import without Scrapy, Selenium, Airflow, boto3 or a database driver. That is
deliberate: it is what makes the interesting logic testable in a second.

## Running it locally

```sh
make install
make test
make up            # Postgres, OpenSearch, MinIO, scheduler, worker
make logs
```

Without the stack, the CLI still works against the registry:

```sh
statehouse jurisdictions --enabled-only
statehouse plan --watermarks var/watermarks.json
statehouse backfill ca --from-year 2019 --chunk-days 30
statehouse check scraped.json --jurisdiction tx --session 2023-2024
```

## Adding a jurisdiction

1. Add an entry to `REGISTRY_ENTRIES` with its portal, timezone, session
   pattern and politeness policy.
2. Write a spider subclassing `JurisdictionSpider` — index URLs, index parse,
   detail parse. Emit loose dictionaries; the transform layer canonicalises.
3. Register the adapter name in `SPIDER_BY_ADAPTER`.
4. Add its expected page markers to the portal smoke check.

The registry entry alone is enough for the scheduler, the backfill planner and
the monitoring DAG to pick it up.

## Conventions worth knowing

- **Watermarks move after the load, never before.** A watermark advanced ahead
  of a write silently skips documents nobody will look for again.
- **Status is derived from the docket, not read from the portal.** Several
  portals' own status fields are stale, and two are wrong for vetoed bills.
- **Every fetch is archived before it is parsed.** A parser bug should be a
  reprocess, not a re-scrape.
- **Time is injected.** Nothing calls `datetime.now()` directly.
- **Ids are derived, never generated.** A re-scrape must land on the same row.
