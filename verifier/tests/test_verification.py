import unittest

from verifier.evidence import website_evidence
from verifier.extractor import extract_page
from verifier.models import DomainRecord, Evidence
from verifier.normalization import NormalizedDomain, normalize_domain
from verifier.verification import verify_row


class VerificationTests(unittest.TestCase):
    def row(self, organization="Acme Singapore Pte Ltd", domain="acme.com"):
        return {"Organization Name": organization, "Domain Name": domain,
                "Country": "SG", "Organization ID": 1}

    def domain(self, html, status=200, final_url="https://acme.com/"):
        page = extract_page(final_url, html, status)
        return DomainRecord("acme.com", "acme.com", "https://acme.com/", final_url=final_url,
                            final_registered_domain="acme.com", http_status=status,
                            dns_status="RESOLVED", domain_active=True, pages=[page])

    def decide(self, row, domain, more=None):
        e = website_evidence(domain.pages, row["Organization Name"], "acme.com") + (more or [])
        return verify_row(row, normalize_domain(row["Domain Name"]), domain, e)

    def test_exact_legal_schema(self):
        html = '<script type="application/ld+json">{"@type":"Organization","name":"Acme","legalName":"Acme Singapore Pte Ltd"}</script>'
        result = self.decide(self.row(), self.domain(html))
        self.assertEqual(result["verification_status"], "VERIFIED_EXACT")
        self.assertEqual(result["confidence"], "HIGH")
        self.assertEqual(result["evidence_url_1"], "https://acme.com/")

    def test_group_requires_explicit_relationship_and_site_identity(self):
        html = ('<script type="application/ld+json">{"@type":"Organization","name":"Acme Group"}</script>'
                '<p>Acme Singapore Pte Ltd is a subsidiary of Acme Group.</p>')
        result = self.decide(self.row(), self.domain(html))
        self.assertEqual(result["verification_status"], "VERIFIED_GROUP")
        self.assertTrue(result["corporate_group_match"])

    def test_name_in_body_is_weak(self):
        html = '<p>Our news mentions Acme Singapore Pte Ltd.</p>'
        result = self.decide(self.row(), self.domain(html))
        self.assertEqual(result["verification_status"], "UNVERIFIED")

    def test_probable_with_contact_and_mention(self):
        html = '<p>Acme Singapore Pte Ltd serves customers.</p><p>Email: info@acme.com</p>'
        result = self.decide(self.row(), self.domain(html))
        self.assertEqual(result["verification_status"], "PROBABLE")
        self.assertTrue(result["evidence_json"])

    def test_competing_site_name_is_not_mismatch(self):
        html = '<script type="application/ld+json">{"@type":"Organization","name":"Other Holdings"}</script>'
        result = self.decide(self.row(), self.domain(html))
        self.assertEqual(result["verification_status"], "UNVERIFIED")

    def test_mismatch_requires_explicit_authoritative_evidence(self):
        domain = self.domain('<h1>Other Holdings</h1>')
        e = Evidence('https://registry.gov.example/record', 'government_registry',
                     'Registry states acme.com is registered to unrelated Other Holdings',
                     'unrelated', 'VERY_STRONG', source_type='external')
        result = self.decide(self.row(), domain, [e])
        self.assertEqual(result["verification_status"], "MISMATCH")
        self.assertEqual(result["evidence_url_1"], e.url)

    def test_dns_failure_without_healthy_network_is_blocked(self):
        row = self.row()
        domain = DomainRecord("acme.com", "acme.com", "https://acme.com/", dns_status="FAILED")
        result = verify_row(row, normalize_domain(row["Domain Name"]), domain, [], network_healthy=False)
        self.assertEqual(result["verification_status"], "BLOCKED")

    def test_dns_failure_with_healthy_network_is_inactive(self):
        row = self.row()
        domain = DomainRecord("acme.com", "acme.com", "https://acme.com/", dns_status="FAILED")
        result = verify_row(row, normalize_domain(row["Domain Name"]), domain, [], network_healthy=True)
        self.assertEqual(result["verification_status"], "INACTIVE")

    def test_cross_domain_redirect_retains_destination_assessment(self):
        row = self.row()
        domain = self.domain('<h1>Other Holdings</h1>', final_url='https://other.com/')
        domain.final_registered_domain = 'other.com'
        normalized = NormalizedDomain('acme.com', 'acme.com', 'acme.com', 'https://acme.com/')
        result = verify_row(row, normalized, domain, [])
        self.assertEqual(result["verification_status"], "REDIRECT")
        self.assertEqual(result["destination_verification_status"], "UNVERIFIED")

    def test_parked(self):
        row = self.row()
        domain = self.domain('<p>This domain is for sale.</p>')
        domain.parked = True
        self.assertEqual(self.decide(row, domain)["verification_status"], "INACTIVE")

    def test_normalization_retains_path(self):
        value = normalize_domain('https://www.acme.com/about?lang=en')
        self.assertEqual(value.normalized_domain, 'acme.com')
        self.assertEqual(value.requested_url, 'https://www.acme.com/about?lang=en')

if __name__ == '__main__':
    unittest.main()
