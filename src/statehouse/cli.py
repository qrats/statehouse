"""Command-line entry point.

Deliberately argparse rather than a framework: this runs inside the Airflow
image, and every dependency in that image is one more thing that can conflict
with the scheduler's pins.

Subcommands:

``jurisdictions``  list the registry
``plan``           show what the scheduler would run now
``backfill``       print a backfill plan
``check``          run the quality checks over a JSON batch
``version``        print the version
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date

from statehouse.config.jurisdictions import JurisdictionRegistry, default_registry
from statehouse.config.settings import load_settings
from statehouse.core.errors import StatehouseError
from statehouse.core.models import Document, Watermark
from statehouse.orchestration.backfill import BackfillPlanner
from statehouse.orchestration.scheduler import IngestScheduler
from statehouse.quality.gate import QualityGate
from statehouse.quality.report import render_text, summarise
from statehouse.transform.normalize import normalise_document
from statehouse.utils.dates import parse_datetime
from statehouse.version import VERSION

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_GATE_BLOCKED = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="statehouse", description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("jurisdictions", help="list configured jurisdictions")
    listing.add_argument("--enabled-only", action="store_true")
    listing.add_argument("--tag", default="", help="filter by tag")

    plan = sub.add_parser("plan", help="show what would be ingested now")
    plan.add_argument("--watermarks", default="", help="path to a watermark JSON file")
    plan.add_argument("--max-parallel", type=int, default=8)
    plan.add_argument("--max-browser", type=int, default=2)

    backfill = sub.add_parser("backfill", help="print a backfill plan")
    backfill.add_argument("jurisdiction")
    backfill.add_argument("--through", default=date.today().isoformat())
    backfill.add_argument("--from-year", type=int, default=None)
    backfill.add_argument("--chunk-days", type=int, default=30)
    backfill.add_argument("--limit", type=int, default=0)

    check = sub.add_parser("check", help="run quality checks over a JSON batch")
    check.add_argument("path", help="file of scraped records, or - for stdin")
    check.add_argument("--jurisdiction", required=True)
    check.add_argument("--session", required=True)
    check.add_argument("--max-errors", type=int, default=0)

    sub.add_parser("version", help="print the version")
    return parser


def _emit(payload: object, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, default=str, indent=2, sort_keys=True))
    else:
        print(text)


def cmd_jurisdictions(args: argparse.Namespace, registry: JurisdictionRegistry) -> int:
    entries = registry.enabled() if args.enabled_only else [registry[c] for c in registry]
    if args.tag:
        entries = [entry for entry in entries if args.tag in entry.tags]
    payload = [
        {
            "code": entry.code,
            "name": entry.name,
            "method": entry.method.value,
            "enabled": entry.enabled,
            "requests_per_minute": entry.politeness.requests_per_minute,
            "tags": list(entry.tags),
        }
        for entry in entries
    ]
    lines = [
        f"{row['code']:<4} {row['method']:<7} {'on ' if row['enabled'] else 'off'} "
        f"{row['requests_per_minute']:>4}/min  {row['name']}"
        for row in payload
    ]
    _emit(payload, args.json, "\n".join(lines) or "no jurisdictions matched")
    return EXIT_OK


def _load_watermarks(path: str) -> list[Watermark]:
    if not path:
        return []
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return [
        Watermark(
            jurisdiction=entry["jurisdiction"],
            stream=entry.get("stream", "default"),
            position=entry.get("position", ""),
            observed_through=parse_datetime(entry.get("observed_through")),
            updated_at=parse_datetime(entry.get("updated_at")),
            revision=int(entry.get("revision", 0)),
        )
        for entry in raw
    ]


def cmd_plan(args: argparse.Namespace, registry: JurisdictionRegistry) -> int:
    scheduler = IngestScheduler(max_parallel=args.max_parallel, max_browser=args.max_browser)
    plan = scheduler.plan(registry.enabled(), _load_watermarks(args.watermarks))
    payload = {
        "selected": [
            {"code": c.code, "score": c.score, "reason": c.reason, "lag_seconds": c.lag_seconds}
            for c in plan.selected
        ],
        "deferred": [{"code": c.code, "reason": c.reason} for c in plan.deferred],
        "browser_slots_used": plan.browser_slots_used,
    }
    lines = [f"selected: {', '.join(plan.codes) or 'none'}"]
    lines += [f"  deferred {c.code}: {c.reason}" for c in plan.deferred]
    _emit(payload, args.json, "\n".join(lines))
    return EXIT_OK


def cmd_backfill(args: argparse.Namespace, registry: JurisdictionRegistry) -> int:
    jurisdiction = registry[args.jurisdiction]
    planner = BackfillPlanner(chunk_days=args.chunk_days)
    plan = planner.plan(
        jurisdiction,
        through=date.fromisoformat(args.through),
        from_year=args.from_year,
    )
    slices = plan.take(args.limit) if args.limit else plan.slices
    payload = {
        "jurisdiction": plan.jurisdiction,
        "sessions": plan.sessions,
        "skipped_sessions": plan.skipped_sessions,
        "total_days": plan.total_days,
        "slices": [
            {"session": s.session, "start": s.start, "end": s.end, "label": s.label}
            for s in slices
        ],
    }
    lines = [f"{len(slices)} slices over {plan.total_days} days"]
    lines += [f"  {s.label}  {s.start} .. {s.end}" for s in slices]
    _emit(payload, args.json, "\n".join(lines))
    return EXIT_OK


def cmd_check(args: argparse.Namespace, _registry: JurisdictionRegistry) -> int:
    raw = sys.stdin.read() if args.path == "-" else open(args.path, encoding="utf-8").read()
    records = json.loads(raw)
    if isinstance(records, dict):
        records = [records]

    documents: list[Document] = []
    failures: list[str] = []
    for record in records:
        try:
            documents.append(
                normalise_document(
                    record, jurisdiction=args.jurisdiction, session=args.session
                )
            )
        except StatehouseError as exc:
            failures.append(str(exc))

    decision = QualityGate(max_errors=args.max_errors).evaluate(documents)
    report = summarise(args.jurisdiction, "cli", len(documents), list(decision.findings))
    payload = {
        "documents": len(documents),
        "unnormalisable": failures,
        "passed": decision.passed,
        "blocked_by": list(decision.blocked_by),
        "report": report.to_dict(),
    }
    _emit(payload, args.json, render_text(report))
    return EXIT_OK if decision.passed and not failures else EXIT_GATE_BLOCKED


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code rather than exiting."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    settings = load_settings()
    registry = default_registry()

    handlers = {
        "jurisdictions": cmd_jurisdictions,
        "plan": cmd_plan,
        "backfill": cmd_backfill,
        "check": cmd_check,
    }
    if args.command == "version":
        _emit({"version": VERSION, "environment": settings.environment}, args.json, VERSION)
        return EXIT_OK

    try:
        return handlers[args.command](args, registry)
    except StatehouseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
