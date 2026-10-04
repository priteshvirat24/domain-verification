"""Unit tests for the Proof Ladder Verification Engine."""
import unittest

from verifier.evidence import website_evidence
from verifier.extractor import extract_page
from verifier.models import DomainRecord, Evidence
from verifier.normalization import NormalizedDomain, normalize_domain
from verifier.verification import verify_row


class VerificationTests(unittest.TestCase):
    def row(self, organization="Acme Singapore Pte Ltd", domain="acme.com", country="SG"):
        return {
            "Organization Name": organization,
            "Domain Name": domain,
            "Country": country,
            "Organization ID": 1,
        }

    def domain(self, html, status=200, final_url="https://acme.com/", dns_status="RESOLVED"):
        page = extract_page(final_url, html, status)
        return DomainRecord(
            "acme.com",
            "acme.com",
            "https://acme.com/",
            final_url=final_url,
            final_registered_domain="acme.com",
            http_status=status,
            dns_status=dns_status,
            domain_active=(200 <= status < 400),
            pages=[page],
        )

    def decide(self, row, domain, more=None):
        e = website_evidence(domain.pages, row["Organization Name"], "acme.com") + (more or [])
        return verify_row(row, normalize_domain(row["Domain Name"]), domain, e)

    def test_exact_legal_schema(self):
        """Gap 4: Full legal name in JSON-LD schema is STRONG proof (Solo Accept)."""
        html = '<script type="application/ld+json">{"@type":"Organization","name":"Acme","legalName":"Acme Singapore Pte Ltd"}</script>'
        result = self.decide(self.row(), self.domain(html))
        self.assertEqual(result["decision"], "ACCEPT")
        self.assertEqual(result["proof_level"], "STRONG")
        self.assertEqual(result["verification_status"], "VERIFIED_EXACT")
        self.assertEqual(result["confidence"], "HIGH")

    def test_group_requires_explicit_relationship_and_site_identity(self):
        """Gap 8: Explicit subsidiary linkage on group site provides STRONG proof."""
        html = (
            '<title>Acme International Group Official</title>'
            '<p>Acme Singapore Pte Ltd is a subsidiary of Acme Group operating in Singapore.</p>'
        )
        result = self.decide(self.row(), self.domain(html))
        self.assertEqual(result["decision"], "ACCEPT")
        self.assertEqual(result["proof_level"], "STRONG")
        self.assertEqual(result["answer_type"], "GROUP_SITE")
        self.assertTrue(result["corporate_group_match"])

    def test_name_in_body_without_supporting_proof_is_unverified(self):
        """Gap 2: Weak clue in body text without supporting proof goes to review."""
        html = '<p>In general market news, someone mentioned Acme Singapore Pte Ltd once.</p>'
        result = self.decide(self.row(), self.domain(html))
        self.assertEqual(result["decision"], "NEEDS_REVIEW")
        self.assertEqual(result["verification_status"], "UNVERIFIED")

    def test_generic_country_text_does_not_verify_entity(self):
        """A title plus a generic country mention cannot prove legal ownership."""
        html = '<h1>Acme Singapore</h1><p>Our headquarters is located in Singapore with local operations.</p>'
        result = self.decide(self.row(), self.domain(html))
        self.assertEqual(result["decision"], "NEEDS_REVIEW")
        self.assertIn(result["verification_status"], ("UNVERIFIED", "PROBABLE"))

    def test_unrelated_owner_mismatch(self):
        """Gap 6: Clear statement of unrelated owner is rejected as MISMATCH."""
        domain = self.domain('<h1>Other Holdings Corporation</h1><p>© 2026 Other Holdings Corp. All rights reserved.</p>')
        e = Evidence(
            'https://registry.gov.example/record',
            'government_registry',
            'Registry states acme.com is registered to unrelated Other Holdings',
            'unrelated',
            'VERY_STRONG',
            source_type='external',
        )
        result = self.decide(self.row(), domain, [e])
        self.assertEqual(result["decision"], "REJECT")
        self.assertEqual(result["verification_status"], "MISMATCH")

    def test_dns_failure_without_healthy_network_is_temporarily_unreachable(self):
        """Gap 5: Local network failure is marked TEMPORARILY_UNREACHABLE."""
        row = self.row()
        domain = DomainRecord("acme.com", "acme.com", "https://acme.com/", dns_status="FAILED", http_status=0)
        result = verify_row(row, normalize_domain(row["Domain Name"]), domain, [], network_healthy=False)
        self.assertEqual(result["decision"], "NEEDS_REVIEW")
        self.assertEqual(result["inactive_subtype"], "TEMPORARILY_UNREACHABLE")

    def test_dns_failure_with_healthy_network_is_domain_does_not_exist(self):
        """Gap 10: Verified NXDOMAIN on healthy network is DOMAIN_DOES_NOT_EXIST."""
        row = self.row()
        domain = DomainRecord("acme.com", "acme.com", "https://acme.com/", dns_status="NXDOMAIN", http_status=0, fetch_error_type="NXDOMAIN")
        result = verify_row(row, normalize_domain(row["Domain Name"]), domain, [], network_healthy=True)
        self.assertEqual(result["decision"], "REJECT")
        self.assertEqual(result["inactive_subtype"], "DOMAIN_DOES_NOT_EXIST")
        self.assertEqual(result["verification_status"], "INACTIVE")

    def test_redirect_destination_evaluated_by_proof_ladder(self):
        """Gap 7: Redirect destination is judged by the proof ladder, not set aside."""
        row = self.row()
        dest_html = '<script type="application/ld+json">{"@type":"Organization","legalName":"Acme Singapore Pte Ltd"}</script>'
        page = extract_page("https://acmegroup.com/sg", dest_html, 200)
        domain = DomainRecord(
            "acme.com", "acme.com", "https://acme.com/",
            final_url="https://acmegroup.com/sg",
            final_registered_domain="acmegroup.com",
            http_status=200, dns_status="RESOLVED", domain_active=True,
            pages=[page],
        )
        normalized = NormalizedDomain("acme.com", "acme.com", "acme.com", "https://acme.com/")
        result = verify_row(row, normalized, domain, [])
        self.assertEqual(result["decision"], "ACCEPT")
        self.assertEqual(result["answer_type"], "OWN_SITE")
        self.assertTrue(result["is_redirected"])
        self.assertEqual(result["redirect_destination"], "https://acmegroup.com/sg")

    def test_parked_domain_rejected(self):
        """Gap 10: Parked domain is rejected with PARKED_OR_FOR_SALE subtype."""
        row = self.row()
        domain = self.domain('<title>acme.com is for sale</title><p>Buy this domain on Sedo Parking.</p>')
        domain.parked = True
        result = self.decide(row, domain)
        self.assertEqual(result["decision"], "REJECT")
        self.assertEqual(result["inactive_subtype"], "PARKED_OR_FOR_SALE")
        self.assertEqual(result["verification_status"], "INACTIVE")

    def test_unreachable_server_never_marked_valid_or_mismatch(self):
        """Gap 5: Server 500 / timeout is TEMPORARILY_UNREACHABLE and sent to review."""
        row = self.row()
        domain = DomainRecord(
            "acme.com", "acme.com", "https://acme.com/",
            final_url="https://acme.com/",
            http_status=500, dns_status="RESOLVED", domain_active=False,
            fetch_error="Internal Server Error 500",
            pages=[],
        )
        result = verify_row(row, normalize_domain(row["Domain Name"]), domain, [])
        self.assertEqual(result["decision"], "NEEDS_REVIEW")
        self.assertEqual(result["inactive_subtype"], "TEMPORARILY_UNREACHABLE")

    def test_blank_domain_cell(self):
        """Gap 7: Empty domain input gives NO_DOMAIN_SUPPLIED."""
        row = self.row(domain="")
        normalized = normalize_domain("")
        result = verify_row(row, normalized, None, [])
        self.assertEqual(result["decision"], "REJECT")
        self.assertEqual(result["inactive_subtype"], "NO_DOMAIN_SUPPLIED")

    def test_whole_word_matching_prevents_fragment_match(self):
        """Gap 3: Short country codes and fragment words must never match inside words."""
        from verifier.proof_ladder import check_word_boundary_match
        self.assertFalse(check_word_boundary_match("in", "Our business is expanding."))
        self.assertFalse(check_word_boundary_match("Star", "Starlight International Ltd"))
        self.assertTrue(check_word_boundary_match("Star", "Star International Ltd"))

    def test_legal_suffixes_distinguish_similarly_named_entities(self):
        html = '<script type="application/ld+json">{"@type":"Organization","legalName":"Acme Australia Pty Ltd"}</script>'
        row = self.row(organization="Acme Australia Pte Ltd", country="AU")
        result = self.decide(row, self.domain(html))
        self.assertNotEqual(result["decision"], "ACCEPT")

    def test_other_footer_owner_is_not_automatically_mismatch(self):
        html = '<title>Onward Holdings</title><footer>© 2026 Onward Holdings Co Ltd</footer><p>O&amp;K Co Ltd is part of our group.</p>'
        result = self.decide(self.row("O&K Co Ltd", "onward.example"), self.domain(html, final_url="https://onward.example/"))
        self.assertNotEqual(result["verification_status"], "MISMATCH")

    def test_legal_page_name_without_operator_context_is_not_exact(self):
        html = '<title>Privacy policy</title><p>Other Holdings operates this site. Acme Singapore Pte Ltd is a customer.</p>'
        page = extract_page("https://acme.com/privacy", html, 200)
        domain = DomainRecord("acme.com", "acme.com", "https://acme.com/", final_url="https://acme.com/privacy", http_status=200, dns_status="RESOLVED", pages=[page])
        result = self.decide(self.row(), domain)
        self.assertNotEqual(result["verification_status"], "VERIFIED_EXACT")

    def test_review_override_requires_traceable_evidence(self):
        row = self.row()
        domain = self.domain('<p>Generic website with no verified relationship to this company.</p>')
        decision = {"decision": "ACCEPT", "reason": "Official register confirms website",
                    "answer_type": "OWN_SITE", "evidence_url": "https://registry.example/123",
                    "evidence_text": "Acme Singapore Pte Ltd official website: acme.com",
                    "reviewer": "analyst-1", "reviewed_at": "2026-10-05", "fetched_at": "2026-10-05"}
        approved = verify_row(row, normalize_domain(row["Domain Name"]), domain, [], reviewer_decision=decision)
        self.assertEqual(approved["verification_status"], "VERIFIED_EXACT")
        self.assertEqual(approved["evidence_url_1"], decision["evidence_url"])
        decision.pop("evidence_text")
        ignored = verify_row(row, normalize_domain(row["Domain Name"]), domain, [], reviewer_decision=decision)
        self.assertNotEqual(ignored["verification_status"], "VERIFIED_EXACT")


if __name__ == "__main__":
    unittest.main()
