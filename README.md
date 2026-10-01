# Enterprise Domain Verification & Inactive Recovery Platform

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Architecture](https://img.shields.io/badge/Architecture-Elimination--First-emerald.svg)](#-verification-decision-logic)
[![Crawler](https://img.shields.io/badge/Crawler-Scrapling%20%2B%20Camoufox-orange.svg)](#-open-source-zero-apify-architecture)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](#)

A high-performance, evidence-based verification and recovery engine designed to inspect, validate, and reconcile enterprise-scale corporate domain datasets. 

The platform supports both **cloud-assisted unblocking (Apify)** and a **100% self-hosted, open-source stack (Camoufox + Scrapling + curl-impersonate + Base64 SERP decoder)** with zero monthly API costs.

---

## 🏛️ System Architecture

### 1. Complete End-to-End Pipeline

![Complete Verification Architecture](assets/complete_verifier_flowchart.jpg)

```mermaid
flowchart TD
    subgraph S1 ["1. Ingestion & Normalization"]
        IN["Input Records (Excel / CSV)"] --> NORM["normalization.py<br>• IDNA ASCII Hostname<br>• Public Suffix List (tldextract)<br>• Legal & Territory Suffix Stripping"]
    end

    subgraph S2 ["2. Multi-Tiered Network Fetcher"]
        NORM --> DNS{"DNS & SSRF Check<br>dns_status()"}
        DNS -->|Failed / NXDOMAIN| INACT_DEAD["Mark INACTIVE<br>(HTTP 0, DNS Failure)"]
        DNS -->|Resolved Global IP| SCRAP["TieredFetcher (Scrapling)<br>curl-impersonate Chrome TLS"]
        SCRAP -->|HTTP 403 / 429 Anti-Bot| CAMOU["Open-Source Unblocking<br>Camoufox C++ Anti-Detect / Apify"]
        SCRAP -->|HTTP 200 OK| EXT["extractor.py<br>HTML / DOM Stream Parser"]
        CAMOU -->|HTTP 200 OK| EXT
    end

    subgraph S3 ["3. Signal & Evidence Extraction"]
        EXT --> FACTS["Observable Facts Extractor<br>• JSON-LD Organization / LegalName<br>• Page Title, H1, Meta Description<br>• Copyright & Footer Legal Entities<br>• Contact Emails, Phones, Addresses<br>• Canonical & Cross-Domain Redirects"]
    end

    subgraph S4 ["4. Dual Decision Arbiter Engines"]
        FACTS --> V2["V2 Human-Analyst Engine<br>(Graduated Scoring: +40, +25, +20, +15, +30, -50)"]
        FACTS --> ELIM["Elimination-First Engine<br>(Presumption of Validity + Elimination Matrix C_A..C_I)"]
        
        V2 --> DEC1["Classifications:<br>• VERIFIED_EXACT<br>• VERIFIED_ENTITY<br>• VERIFIED_GROUP<br>• STRONG_MATCH / PROBABLE<br>• MISMATCH / UNVERIFIED"]
        ELIM --> DEC2["Classifications:<br>• VALID / VALID_GROUP<br>• MISMATCH (Contradiction Found)<br>• INACTIVE / BLOCKED / REDIRECT"]
    end

    subgraph S5 ["5. Inactive Domain Recovery & Replacement"]
        INACT_DEAD --> SERP["Zero-Cost SERP Discovery<br>Bing Harvester + Camoufox + 400-Worker Prober"]
        SERP --> CAND["Extract Candidate Active FQDNs"]
        CAND --> SCRAP
        DEC1 & DEC2 --> RECON["replace_and_populate_super_merged.py<br>• Replace dead domains with verified active hosts<br>• Retain dead domain in 'Original Legacy Domain'<br>• Export SUPER_MERGED_MASTER_FINAL_POPULATED.xlsx"]
    end
```

---

## ⚡ Open-Source (Zero-Apify) Architecture

To eliminate recurring SaaS costs, API quotas, and third-party rate limits, the platform can operate **100% locally with open-source tooling**:

![Open-Source Zero-Apify Architecture](assets/open_source_zero_apify_architecture.jpg)

```mermaid
flowchart TD
    subgraph S1 ["1. Target Pool"]
        INACT["Unresolved Inactive Rows<br>(final_super_merged_master_populated.csv)"]
    end

    subgraph S2 ["2. Zero-Cost SERP Discovery (Replacing Apify Google Scraper)"]
        INACT --> PROBE["Phase A: Fast Async Apex/WWW Prober<br>(dns.asyncresolver + aiohttp @ 400 workers)<br>• Probes apex {host} & www.{host}<br>• Recovers live sites in milliseconds without search engine"]
        
        INACT --> BING["Phase B: Multi-Session Bing SERP Harvester<br>(curl_cffi AsyncSession @ 24-50 workers)"]
        BING --> DECODE["Native Base64 URL Decoder<br>decode_bing_url()<br>• Decodes /ck/a?!...&u=a1... redirect tokens<br>• Extracts target FQDN with 0 network hops"]
        
        BING -->|Headless JS / CAPTCHA Fallback| CAMOU["Camoufox (C++ Patched Firefox)<br>• C++ level canvas/WebGL/audio fingerprint spoofing<br>• Zero bot flags, zero API costs"]
    end

    subgraph S3 ["3. High-Speed Local Crawling (Replacing Apify Web Crawler)"]
        PROBE & DECODE & CAMOU --> CRAWL["Scrapling + curl_cffi Crawler<br>(150 - 300 Async Workers)<br>• Authentic Chrome/Safari TLS & JA3/JA4 fingerprints<br>• HTTP/2 multiplexing, host pacing<br>• Bypasses Cloudflare & Akamai WAF locally"]
        CRAWL --> CACHE[("Local SQLite WAL Cache<br>• search_cache.sqlite (63,800+ SERPs)<br>• live_validation_4000_cache.sqlite")]
    end

    subgraph S4 ["4. Deterministic Verification"]
        CRAWL --> ELIM["9-Point Elimination Engine<br>Checks C_A through C_I<br>(Legal, Brand, Country, Operator, Redirection)"]
    end

    subgraph S5 ["5. Master Reconciliation"]
        ELIM --> RECON["SUPER_MERGED_MASTER_FINAL_POPULATED.xlsx<br>• Inactive domain replaced with verified active URL<br>• Dead domain archived to 'Original Legacy Domain'<br>• Status updated to REPLACED_INACTIVE"]
    end
```

### Key Open-Source Components:
1. **Scrapling + `curl_cffi`**: Python bindings to `curl-impersonate` that replicate authentic desktop Chrome/Safari TLS Client Hello signatures (JA3, JA4, ciphers, ALPN). Crawls up to 300 pages/second locally without triggering Cloudflare/Akamai bot challenges.
2. **Camoufox (`AsyncCamoufox`)**: C++ patched anti-detect browser engine based on Firefox. Injects human-like noise into canvas and WebGL draw calls and overrides hardware metrics at the browser engine level.
3. **In-Memory Base64 URL Decoder**: Bing wraps search results in redirect tokens (`/ck/a?!...&u=a1...`). Our native decoder unwraps the destination URL directly in memory with **zero network latency** and **zero extra HTTP requests**.
4. **400-Worker Apex/WWW Prober**: Uses `dns.asyncresolver` and `aiohttp` to test apex and `www.` host variants concurrently, reviving misconfigured active domains without hitting search engines.

---

## 🔬 Verification Decision Logic

The platform features two decision modes:

### 1. V2 Human-Analyst Scoring Engine (`v2_human_decision.py`)
Computes graduated confidence points based on observable evidence:

| Signal Evaluated | Score Weight | Description |
| :--- | :---: | :--- |
| **Exact Legal Entity** | **+40** | Verbatim match in JSON-LD `legalName`, registration filings, or footer copyright. |
| **Core Entity Name** | **+25** | Clean organization name identified in `<title>`, `<h1>`, or `<meta description>`. |
| **Domain-Brand Match** | **+20** | Registered domain label aligns with distinctive organization brand tokens. |
| **Country / ccTLD Match** | **+15** | Domain extension (`.my`, `.au`, `.sg`) or page address matches target country. |
| **Official Contact Proof** | **+15** | Contact email address domain matches the target root domain. |
| **Corporate Group Link** | **+30** | Domain matches parent corporate group or multi-brand umbrella portal. |
| **Contradictory Penalty** | **-50** | Commercial entity mapped to sovereign government `.gov` portal or competitor. |

#### Classification Outcomes:
- **`VERIFIED_EXACT`**: Score $\ge 40$ with verbatim legal entity match.
- **`VERIFIED_GROUP`**: Score $\ge 30$ with verified parent/subsidiary corporate structure.
- **`VERIFIED_ENTITY`**: Score $\ge 35$ with matching brand, domain, and identity.
- **`STRONG_MATCH` / `PROBABLE`**: Multi-signal agreement across independent sources.
- **`MISMATCH`**: Negative score ($< 0$) indicating conflicting ownership.
- **`INACTIVE`**: Confirmed DNS failure (`NXDOMAIN`), HTTP 410, or parked domain.

### 2. Elimination-First Engine (`elimination_engine.py`)
Employs an evidentiary matrix (`C_A` to `C_I`) under the **presumption of validity**: pre-existing mappings are considered valid unless concrete contradictory evidence eliminates them.

---

## 📋 Real Production Examples

### Example 1: `VERIFIED_ENTITY`
- **Input**: `3NTITY BERHAD` (Country: `MY`, Input Domain: `3ntity.com`)
- **Evidence**: Title contains `"3ntity Sdn Bhd"`, domain corresponds to brand token `3ntity`, ccTLD context is `MY`, contact email found `infosales@3ntity.com`, parent group is `BERJAYA GROUP BHD`.
- **Score**: $+25 + 20 + 15 + 15 + 30 = \mathbf{105.0}$ $\to$ **`VERIFIED_ENTITY`** (Confidence: `HIGH`).

### Example 2: `VERIFIED_GROUP`
- **Input**: `ABB AUTOMATION AND ELECTRIFICATION (VIETNAM) COMPANY LIMITED` (Country: `VN`, Domain: `abb.com`)
- **Evidence**: Target redirects to `abb.com/global/en`. Domain corresponds to parent brand token `abb`, sales territory is `ABB LTD - VN`.
- **Score**: $+20 + 30 = \mathbf{50.0}$ $\to$ **`VERIFIED_GROUP`** (Confidence: `HIGH`).

### Example 3: `MISMATCH`
- **Input**: `AGED CARE SERVICES 17 (BONBEACH) PTY LTD` (Commercial entity, Domain: `acnc.gov.au`)
- **Evidence**: Website is official Australian Government registry (`acnc.gov.au`). Private company cannot be represented by a government portal.
- **Score**: $-50$ Contradictory Government Penalty $\to$ **`MISMATCH`** (Confidence: `HIGH`).

### Example 4: `INACTIVE` $\to$ `REPLACED_INACTIVE`
- **Input**: `ABLE CORPORATE SERVICE, INC` (Legacy Domain: `able.co.jp` failed DNS).
- **Recovery**: Automated search query `q = "ABLE CORPORATE SERVICE" Japan official website` discovered candidate `able-cs.co.jp`. Scrapling crawled the target, validated Japanese corporate registration, and updated `SUPER_MERGED_MASTER_FINAL_POPULATED.xlsx` with `Original Legacy Domain: able.co.jp`.

---

## 🚀 Quick Start & Installation

### 1. Requirements
- Python 3.10+
- (Optional) Apify API Token if running with cloud unblocking.

### 2. Setup Virtual Environment

```bash
# Clone the repository
git clone https://github.com/priteshvirat24/domain-verification.git
cd domain-verification

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r verifier/requirements.txt
```

### 3. Setting Up Credentials (Safe & Secure)

> [!NOTE]
> Never hardcode API keys in source files. Tokens are read automatically from environment variables.

```bash
export APIFY_TOKEN="your_apify_api_token_here"
```

---

## 💻 CLI Usage & Commands

### Representative Sample Verification (75 Rows Dry Run)
```bash
python -m verifier --input "Domains List_V2.xlsx" --sample-size 75 --output-dir ./output
```

### High-Concurrency Live Run with Cached Acceleration
```bash
python -m verifier \
  --input "Domains List_V2.xlsx" \
  --sample-size 4000 \
  --output-dir ./output \
  --cache verifier/live_validation_4000_cache.sqlite \
  --concurrency 100
```

### Open-Source Zero-Apify Domain Recovery
```bash
python verifier/max_parallel_resolver.py
```

### Reconcile & Populate Super Merged Master Dataset
```bash
python verifier/replace_and_populate_super_merged.py
```

### Offline Cached Evaluation (Zero Network Calls)
```bash
python -m verifier \
  --input "Domains List_V2.xlsx" \
  --sample-size 4000 \
  --output-dir ./output \
  --cache verifier/live_validation_4000_cache.sqlite \
  --offline
```

### Run Unit Tests
```bash
python -m unittest discover -s verifier/tests -v
```

---

## 📊 Benchmark Results (1,000 Representative Rows Run)

The bundled `run_1000_output/` contains the results of running 1,000 stratified rows across all represented countries, corporate groups, and domain structures:

| Metric / Status | Count / Rate | Description |
| :--- | :--- | :--- |
| **Total Rows Processed** | 1,000 | Stratified random sample across countries & domain types |
| **Unique Domains** | 618 | Deduplicated network queries |
| **VERIFIED_EXACT** | 53 | Verbatim legal entity match in structured data / corporate notice |
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

---

## 📂 Output Files Generated

- **`results.csv`**: Full row-level verification decisions, matched company identifiers, confidence scores, and evidence trails.
- **`review.csv`**: Filtered dataset of rows requiring human review (`BLOCKED`, `PROBABLE`, `UNVERIFIED`, `REDIRECT`, `MISMATCH`).
- **`summary.json`**: Aggregate statistics (accuracy, fetch success rate, verification rate, status distribution).
- **`evidence.jsonl`**: Detailed structured JSON-LD, header text, and official legal entity extracts per verified pair.
- **`SUPER_MERGED_MASTER_FINAL_POPULATED.xlsx`**: Reconciled master corporate dataset with active domains populated and legacy dead domains preserved in audit columns.

---

## 🛡️ Security Guidelines

- **Zero Hardcoded Secrets**: This repository does not contain hardcoded API tokens or private credentials.
- **Environment Driven**: All external integrations read strictly from environment variables (`APIFY_TOKEN`, `TAVILY_API_KEY`).
- **Git-Filtered History**: All previous commit histories have been sanitized using `git-filter-repo`.
