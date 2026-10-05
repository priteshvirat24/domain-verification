"""Pilot test for resolving remaining unresolved domains in the 113k dataset.
Tests 100 unresolved rows (50 UNVERIFIED_INACTIVE and 50 NOT_FOUND) using:
- Direct www / apex DNS & HTTP fallback probing
- Multi-query SERP candidate extraction (up to 5 candidates)
- Proof Ladder / Elimination Engine verification
- Categorized reason assignment for unresolvable rows
"""
import asyncio
import csv
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from verifier.normalization import normalize_domain
from verifier.proof_ladder import evaluate_proof_ladder, legal_identity_key
from verifier.recover_inactive import clean_org_for_search, COUNTRY_MAP, extract_candidate_domains
from verifier.models import DomainRecord


async def test_pilot():
    search_conn = sqlite3.connect("verifier/search_cache.sqlite")
    dom_conn = sqlite3.connect("verifier/live_validation_4000_cache.sqlite")
    s_cur = search_conn.cursor()
    d_cur = dom_conn.cursor()

    unresolved_inactive = []
    unresolved_not_found = []

    with open("final_super_merged_master_populated.csv", "r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for r in reader:
            st = r.get("Domain Status") or ""
            if st == "UNVERIFIED_INACTIVE" and len(unresolved_inactive) < 50:
                unresolved_inactive.append(r)
            elif st == "NOT_FOUND" and len(unresolved_not_found) < 50:
                unresolved_not_found.append(r)
            if len(unresolved_inactive) >= 50 and len(unresolved_not_found) >= 50:
                break

    test_rows = unresolved_inactive + unresolved_not_found
    print(f"Loaded {len(test_rows)} pilot rows (50 inactive, 50 not found)")

    client = httpx.AsyncClient(timeout=3.0, follow_redirects=True, verify=False)
    resolved_count = 0
    reasons_breakdown = {}

    try:
        for i, r in enumerate(test_rows, 1):
            org = (r.get("Organization Name") or r.get("original_company_name") or "").strip()
            country_code = (r.get("country code") or r.get("Country (Group)") or "").strip().upper()
            country_name = COUNTRY_MAP.get(country_code, country_code)
            orig_dom = (r.get("Original Legacy Domain") or r.get("Domain URL") or "").strip()
            status = r.get("Domain Status")

            clean_name = clean_org_for_search(org)
            queries = [
                f'"{clean_name}" {country_name} official website',
                f'"{clean_name}" official website',
                f'{clean_name} {country_name} official website',
            ]

            found_results = None
            for q in queries:
                row = s_cur.execute("SELECT results_json FROM searches WHERE query = ?", (q,)).fetchone()
                if row:
                    found_results = json.loads(row[0])
                    break

            verified_domain = None
            verified_reason = None

            # 1. If has legacy domain and inactive, probe www and apex first
            if orig_dom and status == "UNVERIFIED_INACTIVE":
                norm_orig = normalize_domain(orig_dom)
                host = norm_orig.normalized_domain
                if host:
                    targets = [f"www.{host}" if not host.startswith("www.") else host, host]
                    for t in targets:
                        for proto in ["https", "http"]:
                            try:
                                resp = await client.get(f"{proto}://{t}/", headers={"User-Agent": "Mozilla/5.0"})
                                if resp.status_code in (200, 301, 302, 307, 308):
                                    soup = BeautifulSoup(resp.text[:50000], "html.parser")
                                    title = (soup.title.string or "").strip() if soup.title else ""
                                    footers = " ".join(f.get_text() for f in soup.find_all(["footer", "small"]))
                                    h1s = [h.get_text().strip() for h in soup.find_all("h1")]
                                    dom_rec = DomainRecord(
                                        domain=t,
                                        registered_domain=norm_orig.registered_domain or t,
                                        requested_url=f"{proto}://{t}/",
                                        final_url=str(resp.url),
                                        http_status=resp.status_code,
                                        dns_status="ACTIVE",
                                        pages=[{
                                            "url": str(resp.url),
                                            "status": resp.status_code,
                                            "title": title,
                                            "visible_text": soup.get_text()[:4000],
                                            "footer": footers,
                                            "h1": h1s,
                                        }]
                                    )
                                    eval_res = evaluate_proof_ladder(
                                        {"Organization Name": org, "Country": country_code},
                                        normalize_domain(t),
                                        dom_rec,
                                        network_healthy=True,
                                    )
                                    if eval_res.get("decision") == "ACCEPT":
                                        verified_domain = f"https://{t}"
                                        verified_reason = f"Legacy domain recovered via {t}: {eval_res.get('decision_reason')}"
                                        break
                            except Exception:
                                continue
                        if verified_domain:
                            break

            # 2. Search discovery candidates if not verified via legacy
            if not verified_domain and found_results:
                cands = extract_candidate_domains(found_results, max_candidates=5)
                for cand in cands:
                    p_data = {}
                    d_row = d_cur.execute("SELECT payload FROM domains WHERE domain = ?", (cand,)).fetchone()
                    if d_row and d_row[0] != "{}":
                        try:
                            p_data = json.loads(d_row[0])
                        except Exception:
                            pass
                    
                    pages = p_data.get("pages") or []
                    if not pages:
                        # Quick live fetch
                        try:
                            headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
                            for proto in ["https", "http"]:
                                try:
                                    resp = await client.get(f"{proto}://{cand}/", headers=headers)
                                    if resp.status_code in (200, 301, 302, 307, 308):
                                        soup = BeautifulSoup(resp.text[:50000], "html.parser")
                                        title = (soup.title.string or "").strip() if soup.title else ""
                                        footers = " ".join(f.get_text() for f in soup.find_all(["footer", "small"]))
                                        h1s = [h.get_text().strip() for h in soup.find_all("h1")]
                                        pages = [{
                                            "url": str(resp.url),
                                            "status": resp.status_code,
                                            "title": title,
                                            "visible_text": soup.get_text()[:4000],
                                            "footer": footers,
                                            "h1": h1s,
                                        }]
                                        break
                                except Exception:
                                    continue
                        except Exception:
                            pass

                    if pages:
                        try:
                            dom_rec = DomainRecord(
                                domain=cand,
                                registered_domain=p_data.get("registered_domain") or cand,
                                requested_url=p_data.get("requested_url") or f"https://{cand}/",
                                final_url=p_data.get("final_url") or f"https://{cand}/",
                                http_status=p_data.get("http_status") or 200,
                                dns_status=p_data.get("dns_status") or "ACTIVE",
                                pages=pages,
                            )
                            norm = normalize_domain(cand)
                            eval_res = evaluate_proof_ladder(
                                {"Organization Name": org, "Country": country_code},
                                norm,
                                dom_rec,
                                network_healthy=True,
                            )
                            if eval_res.get("decision") == "ACCEPT":
                                verified_domain = f"https://{cand}"
                                verified_reason = eval_res.get("decision_reason")
                                break
                        except Exception as e:
                            continue

            if verified_domain:
                resolved_count += 1
                print(f"[{i}/100] RESOLVED: {org} ({country_code}) -> {verified_domain} | Reason: {verified_reason}")
            else:
                if not found_results:
                    reason = "NO_SEARCH_INDEX_RECORD: No search engine indexed results found for company."
                elif not extract_candidate_domains(found_results, max_candidates=5):
                    reason = "NO_OFFICIAL_CANDIDATE: Search returned only directories, social media, or aggregator profiles."
                else:
                    reason = "REJECTED_PROOF_LADDER: Discovered domains failed ownership proof (unrelated brand or no entity link)."
                reasons_breakdown[reason] = reasons_breakdown.get(reason, 0) + 1

    finally:
        await client.aclose()

    print("\n=== PILOT SUMMARY ===")
    print(f"Total tested: {len(test_rows)}")
    print(f"Successfully resolved: {resolved_count} ({resolved_count/len(test_rows)*100:.1f}%)")
    print(f"Unresolved breakdown: {reasons_breakdown}")

if __name__ == "__main__":
    asyncio.run(test_pilot())
