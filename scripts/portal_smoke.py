#!/usr/bin/env python
"""Probe live portals for shape changes.

Not a crawl: one request per jurisdiction, checking that the page still looks
like the page the adapter expects. Runs on a schedule in CI so a portal
redesign is caught in a morning rather than in a week's worth of empty
batches.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from statehouse.config.jurisdictions import default_registry
from statehouse.config.settings import load_settings
from statehouse.scraping.middlewares.retry import classify_response

#: Fragments each adapter's parser depends on. If one disappears the adapter
#: will start producing empty rows, so it is worth failing loudly here.
EXPECTED_MARKERS: dict[str, tuple[str, ...]] = {
    "us": ("BILLSTATUS",),
    "ca": ("bill_results", "billSearchClient"),
    "tx": ("tblBills", "LegSess"),
    "ny": ("bill-results",),
    "fl": ("Session",),
    "il": ("grplist", "DocTypeID"),
    "oh": ("legislation",),
    "ga": ("api",),
    "wa": ("legislation",),
}


@dataclass
class Probe:
    jurisdiction: str
    url: str
    ok: bool
    status: int | None = None
    size: int = 0
    missing_markers: tuple[str, ...] = ()
    error: str = ""


def probe(code: str, url: str, *, user_agent: str, timeout: float) -> Probe:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        return Probe(code, url, ok=False, status=exc.code, error=f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return Probe(code, url, ok=False, error=str(exc))

    failure = classify_response(status, body)
    if failure is not None:
        return Probe(code, url, ok=False, status=status, size=len(body), error=str(failure))

    text = body.decode("utf-8", errors="replace")
    missing = tuple(m for m in EXPECTED_MARKERS.get(code, ()) if m not in text)
    return Probe(
        code,
        url,
        ok=not missing,
        status=status,
        size=len(body),
        missing_markers=missing,
        error="expected markers missing" if missing else "",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jurisdictions", default="all")
    parser.add_argument("--report", default="")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    settings = load_settings()
    registry = default_registry()
    codes = [c.strip() for c in args.jurisdictions.split(",") if c.strip()]
    targets = registry.resolve_many(codes)

    results = [
        probe(
            entry.code,
            entry.portal_url,
            user_agent=settings.user_agent,
            timeout=args.timeout,
        )
        for entry in targets
    ]

    for result in results:
        state = "ok  " if result.ok else "FAIL"
        detail = result.error or f"{result.size} bytes"
        print(f"{state} {result.jurisdiction:<4} {detail}")

    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([asdict(r) for r in results], indent=2, sort_keys=True), encoding="utf-8"
        )

    failed = [r for r in results if not r.ok]
    if failed:
        print(f"{len(failed)} portal(s) failed the shape check", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
