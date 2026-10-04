# Domain-to-company verification pipeline

This pipeline tests whether a domain is officially related to a legal entity or corporate group. Reachability and name similarity are diagnostic signals, not verification.

## Input assessment

`Domains List_V2.xlsx` has 93,918 data rows and the columns `Country`, `Sales Territory ID`, `Sales Territory Name`, `Organization ID`, `Organization Name`, and `Domain Name`. It contains about 40,196 distinct hosts, 4,453 blank domains, and 5,576 values with URL paths. `harveynorman.com.au` appears 814 times. The code normalizes hosts for a shared fetch cache and retains the original value and requested path per row.

## Install and run locally

Use Python 3.12. Install `verifier/requirements.txt` from the repository root. Browser fallback also needs Chromium (`python -m playwright install chromium` and `python -m patchright install chromium`).

```bash
python -m verifier --input 'Domains List_V2.xlsx' --sample-size 75 --output-dir verifier/dry_run_output
python -m verifier --input 'Domains List_V2.xlsx' --full-run --output-dir verifier/full_output
```

The default command is a 75-row dry run. The full dataset requires `--full-run`; inspect dry-run evidence and results first. `--dynamic` and `--stealth` enable browser fallbacks. `--proxy-url` supplies a proxy for the stealth tier. `--search-ambiguous` needs `TAVILY_API_KEY`; set `--max-external-searches` to cap API usage. `--offline` reads cached evidence without making requests, useful for output/schema checks; it does **not** verify uncached domains.

Local outputs:

- `results.csv`: every selected input row once, source columns preserved, appended decision and provenance fields.
- `evidence.jsonl`: one structured record per evidence item.
- `review.csv`: prioritized `MISMATCH`, `PROBABLE`, `UNVERIFIED`, `BLOCKED`, and `REDIRECT` rows.
- `inactive_recovery.csv`: blank, parked, expired, or explicitly nonexistent domains for separate discovery work.
- `audit_300.csv`: a stratified set for human labeling; its labels and accuracy are intentionally blank.
- `manifest.json`: input and code hashes, sample seed, selected row IDs, and execution settings.
- `summary.json`: counts and rates; ground-truth accuracy stays null without labels.

The SQLite cache keeps domain pages, input path pages, and entity-level evidence separately for 30 days. Duplicate domains reuse the crawl, while each organization/domain pair receives its own decision. Browser fetches are opt-in and only used after HTTP indicates a JS shell or access protection. The crawler fetches the homepage and up to seven high-priority identity/legal pages; it does not crawl an entire site. Requests use a global concurrency bound, per-host pacing, robots checks, safe redirects, timeout, and finite retry/backoff.

## Decision rules

- `VERIFIED_EXACT`: official legal/footer or Organization JSON-LD identifies the entity.
- `VERIFIED_GROUP`: an official group site identifies itself and explicitly relates the entity to the group. An identical brand token alone cannot establish the relationship.
- `PROBABLE`: corroborating site signals exist but legal/group proof is absent; it remains in the review queue.
- `UNVERIFIED`: accessible site or missing input without adequate relationship evidence.
- `MISMATCH`: strong, fetched/curated authoritative evidence explicitly supports an unrelated owner. A different homepage name alone is insufficient.
- `INACTIVE`: no domain, parking, expiry, HTTP 410, or explicit NXDOMAIN with independent network health. `inactive_subtype` distinguishes these cases.
- `REDIRECT`: cross-registered-domain redirect that remains unverified; `destination_verification_status` records the destination assessment. A verified destination retains its verified status and `is_redirected=true`.
- `BLOCKED`: robots, protection, persistent access errors, or an environment unable to reach the site.

External search is discovery only. A search snippet is never evidence: the returned source URL is fetched and the relevant text retained. A previously reviewed external-evidence JSONL can be passed with `--external-evidence`; each entry needs `organization`, `domain`, `source_url`, `source_type`, `source_title`, `evidence_text`, `relationship`, `strength`, and `fetched_at`.

Persist manual decisions with `--reviewer-decisions decisions.jsonl`. Each JSONL record needs the exact `organization`, `country`, and `domain`, plus `decision`, `reason`, `answer_type`, `evidence_url`, `evidence_text`, `reviewer`, `reviewed_at`, and `fetched_at`. A decision without its traceable source is ignored. The file is matched by full legal name, country, and normalized domain so a group review cannot silently transfer to a different entity.

## Apify Actor

The Actor definition is [`.actor/actor.json`](../.actor/actor.json); its image uses the supplied Scrapling repository. Supply an HTTPS CSV/XLSX URL as `input_file`, or a path inside the image. `dry_run` defaults to true. Set `dry_run: false` only after reviewing the sample. The Actor writes separate named result, evidence, and review datasets, plus `PROGRESS` and `SUMMARY` records in a named key-value store. The dataset and cache names are derived from input-file contents and `run_key`. On restart it reads existing row/evidence IDs and skips published items; persisted domain and pair caches avoid recrawling. Change `run_key` to start a fresh verification run.

Example input:

```json
{
  "input_file": "https://your-storage.example/data/company-master.xlsx",
  "organization_column": "Organization Name",
  "domain_column": "Domain Name",
  "dry_run": true,
  "sample_size": 75,
  "concurrency": 20,
  "use_dynamic": false,
  "use_stealth": false,
  "use_proxy": false
}
```

If `use_proxy` is enabled, configure Apify Proxy in the Actor account; the proxy is reserved for stealth escalation. Keep `TAVILY_API_KEY` in an Actor secret, never in the input file.

## Validation and present environment

Run `python -m unittest discover -s verifier/tests -v`. Tests cover exact identity, subsidiary/group evidence, weak mentions, mismatch restraint, redirects, parking, blocked versus inactive DNS, duplicate-domain reuse, row preservation, and export counts.

The original `dry_run_output` predates the strict proof rules and should not be used as a current validation. The October 2026 live pilot and 4,000-row run use fresh domain caches. No full 93,918-row run has been started. Apify and external search require credentials; without them, blocked sites and subsidiaries needing outside proof remain in the review set. Accuracy cannot be claimed until the 300-row audit is hand labeled.
