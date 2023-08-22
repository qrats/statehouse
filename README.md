# statehouse

Ingests bills, resolutions and regulatory documents from state legislatures
and Congress into one schema.

Replaces the old per-source Lambda handlers, which had drifted into six copies
of the same half-working pagination loop with no shared normalisation and no
way to tell whether a source had gone quiet.

## Where things go

| Path | What lives there |
| --- | --- |
| `src/statehouse/core` | Models, enums, errors, identity, hashing, clock |
| `src/statehouse/config` | Runtime settings and the jurisdiction registry |
| `src/statehouse/utils` | Text, dates, URLs, retry |

More layers land as they are written. The plan is scraping and browser
fetching, then transform and quality, then orchestration and loading.

## Running the tests

```sh
make install
make test
```
