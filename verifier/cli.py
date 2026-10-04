"""Local dry-run and explicitly gated full-run entry point."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .cache import SQLiteCache
from .config import Config
from .export import write_outputs
from .input import iter_input
from .pipeline import process_records, representative_rows


async def _run(args) -> None:
    config = Config(concurrency=args.concurrency, max_evidence_pages=args.max_evidence_pages,
                    use_dynamic=args.dynamic, use_stealth=args.stealth, use_proxy=bool(args.proxy_url),
                    use_apify=args.use_apify, apify_token=args.apify_token)
    if args.full_run:
        records = list(iter_input(args.input, args.organization_column, args.domain_column))
    else:
        if args.sample_size <= 0:
            raise ValueError("Sample size must be a positive integer")
        records = representative_rows(args.input, args.sample_size,
                                      args.organization_column, args.domain_column,
                                      seed=args.seed)
    cache = SQLiteCache(args.cache, config.cache_ttl_seconds)
    try:
        output, info = await process_records(records, config, cache, dry_run=not args.full_run,
                                              external_evidence_file=args.external_evidence,
                                              search_ambiguous=args.search_ambiguous,
                                              proxy_url=args.proxy_url, fetch_domains=not args.offline,
                                              batch_domains=args.batch_domains,
                                              max_external_searches=args.max_external_searches,
                                              reviewer_decisions_file=args.reviewer_decisions)
    finally:
        cache.close()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = write_outputs(output, output_dir / "results.csv", output_dir / "review.csv",
                            output_dir / "evidence.jsonl", output_dir / "summary.json",
                            info["unique_domains"])
    summary["network_healthy"] = info["network_healthy"]
    summary["dry_run"] = not args.full_run
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    def digest(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    package_dir = Path(__file__).resolve().parent
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_path": str(Path(args.input).resolve()),
        "input_sha256": digest(Path(args.input)),
        "code_sha256": {p.name: digest(p) for p in sorted(package_dir.glob("*.py"))},
        "sample_seed": None if args.full_run else args.seed,
        "selected_input_row_ids": [r[0] for r in records],
        "config": {"concurrency": args.concurrency, "max_evidence_pages": args.max_evidence_pages,
                   "dynamic": args.dynamic, "stealth": args.stealth, "apify_enabled": bool(args.use_apify and args.apify_token)},
        "output_rows": len(output),
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Evidence-based domain/company verification")
    parser.add_argument("--input", default="Domains List_V2.xlsx")
    parser.add_argument("--output-dir", default="verifier/dry_run_output")
    parser.add_argument("--cache", default="verifier/production_cache.sqlite")
    parser.add_argument("--organization-column")
    parser.add_argument("--domain-column")
    parser.add_argument("--sample-size", type=int, default=75)
    parser.add_argument("--seed", type=int, default=42, help="deterministic sampling random seed")
    parser.add_argument("--batch-domains", type=int, default=50)
    parser.add_argument("--full-run", action="store_true", help="explicitly process the complete input")
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--max-evidence-pages", type=int, default=4)
    parser.add_argument("--dynamic", action="store_true")
    parser.add_argument("--stealth", action="store_true")
    parser.add_argument("--proxy-url")
    parser.add_argument("--external-evidence", help="JSONL of fetched, reviewed authoritative sources")
    parser.add_argument("--reviewer-decisions", help="JSONL of audited organization/country/domain decisions")
    parser.add_argument("--search-ambiguous", action="store_true", help="requires TAVILY_API_KEY or Apify token")
    parser.add_argument("--max-external-searches", type=int, default=100)
    parser.add_argument("--offline", action="store_true", help="inspect sample/output using cached evidence only")
    parser.add_argument("--use-apify", action="store_true", default=True, help="use Apify actors for unblocking and search")
    parser.add_argument("--no-apify", dest="use_apify", action="store_false", help="disable Apify actors")
    parser.add_argument("--apify-token", default=os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN"), help="Apify API Token (can also be set via APIFY_TOKEN env var)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
