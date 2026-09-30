# Domain Verification Pipeline

A production-grade, evidence-based domain-to-organization verification engine with multi-tiered fetching (Scrapling + curl-impersonate + Playwright) and Apify cloud unblocking.

---

## 🚀 Quick Start

### 1. Requirements
- Python 3.10+
- (Optional) Apify API Token for anti-bot unblocking & Google Search discovery.

### 2. Setup Virtual Environment

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies (including local Scrapling library)
pip install -r verifier/requirements.txt
```

### 3. Running Verification

#### Representative Live Verification (Default: 75 sample rows)
```bash
python -m verifier --input "Domains List_V2.xlsx" --sample-size 75 --output-dir ./output
```

#### Running With High Concurrency & Apify Unblocking
```bash
python -m verifier \
  --input "Domains List_V2.xlsx" \
  --sample-size 4000 \
  --output-dir ./output \
  --cache verifier/live_validation_4000_cache.sqlite \
  --concurrency 100 \
  --use-apify
```

#### Offline Evaluation (Using Cached Evidence)
```bash
python -m verifier \
  --input "Domains List_V2.xlsx" \
  --sample-size 4000 \
  --output-dir ./output \
  --cache verifier/live_validation_4000_cache.sqlite \
  --offline
```

### 4. Running Unit Tests

```bash
python -m unittest discover -s verifier/tests -v
```

---

## 📂 Output Files Generated

- **`results.csv`**: Full row-level verification decisions, matched company identifiers, confidence scores, and evidence trails.
- **`review.csv`**: Filtered dataset of rows requiring human review (`BLOCKED`, `PROBABLE`, `UNVERIFIED`, `REDIRECT`, `MISMATCH`).
- **`summary.json`**: Aggregate statistics (accuracy, fetch success rate, verification rate, status distribution).
- **`evidence.jsonl`**: Detailed structured JSON-LD, header text, and official legal entity extracts per verified pair.

---

## 📊 Benchmark Results (1,000 Representative Rows Run)

The bundled `run_1000_output/` contains the results of running 1,000 stratified rows across all represented countries, corporate groups, and domain structures:

| Metric / Status | Count / Rate | Description |
| :--- | :--- | :--- |
| **Total Rows Processed** | 1,000 | Stratified random sample with seed 42 |
| **Unique Domains** | 618 | Deduplicated network queries |
| **VERIFIED_EXACT** | 53 | Verbatim legal entity match in structured data / official notice |
| **VERIFIED_ENTITY** | 12 | Clean core name match with brand + domain alignment |
| **VERIFIED_GROUP** | 370 | Official parent/group umbrella entity confirmation |
| **STRONG_MATCH** | 8 | Multi-signal agreement (domain, brand, country, site identity) |
| **PROBABLE** | 35 | Brand/contact signals present without full corporate filings |
| **UNVERIFIED** | 264 | Unreachable or insufficient identity proof |
| **MISMATCH** | 31 | Contradictory ownership (e.g. commercial entity pointing to gov registry) |
| **INACTIVE** | 190 | Confirmed DNS failure, domain parking, or HTTP 410 |
| **REDIRECT** | 36 | Cross-domain redirect to external website |
| **BLOCKED** | 1 | Anti-bot block remaining after fetch tiers |
| **Overall Verified Rate** | **44.3%** | Total verified (`EXACT` + `ENTITY` + `GROUP` + `STRONG`) |
| **High Confidence Rate** | **68.6%** | High-certainty automated classification |
