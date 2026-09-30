"""Unit tests for the Elimination-First Architecture."""
import unittest

from verifier.elimination_engine import evaluate_elimination_decision
from verifier.extractor import extract_page
from verifier.models import DomainRecord
from verifier.normalization import normalize_domain, registered_domain


class EliminationEngineTests(unittest.TestCase):
    def make_row(self, org: str, domain: str, country: str = "US", territory: str = ""):
        return {
            "Organization Name": org,
            "Domain Name": domain,
            "Country": country,
            "Sales Territory Name": territory,
            "Organization ID": 1,
        }

    def make_domain(self, domain: str, html: str, status: int = 200, final_url: str = ""):
        furl = final_url or f"https://{domain}/"
        page = extract_page(furl, html, status)
        reg_domain = registered_domain(domain)
        final_reg = registered_domain(furl.split("/")[2] if "//" in furl else domain)
        return DomainRecord(
            domain=domain,
            registered_domain=reg_domain,
            requested_url=f"https://{domain}/",
            final_url=furl,
            final_registered_domain=final_reg,
            http_status=status,
            dns_status="RESOLVED",
            domain_active=True,
            pages=[page],
        )

    def test_direct_valid_entity(self):
        """Website title/h1 directly matches normalized organization."""
        row = self.make_row("Carinity Education - Rockhampton", "carinity.qld.edu.au", "AU")
        html = "<title>Carinity Education | Rockhampton Campus</title><h1>Welcome to Carinity Education</h1>"
        dom = self.make_domain("carinity.qld.edu.au", html)
        res = evaluate_elimination_decision(row, normalize_domain(row["Domain Name"]), dom)
        self.assertEqual(res["classification"], "VALID")
        self.assertIn(res["confidence"], ("HIGH", "MEDIUM"))
        self.assertFalse(res["contradiction_found"])

    def test_corporate_group_valid(self):
        """Corporate group matches via territory name or parent brand without requiring legal name in footer."""
        row = self.make_row("PHILIP MORRIS (PAKISTAN) LIMITED", "pmi.com", "PK", territory="PMI - PK")
        html = "<title>Philip Morris International | Delivering a Smoke-Free Future</title><p>Our operations across the globe including Pakistan.</p>"
        dom = self.make_domain("pmi.com", html)
        res = evaluate_elimination_decision(row, normalize_domain(row["Domain Name"]), dom)
        self.assertEqual(res["classification"], "VALID_GROUP")
        self.assertEqual(res["confidence"], "HIGH")
        self.assertIn("corporate group", res["decision_reason"].lower())

    def test_government_portal_mismatch(self):
        """Private commercial organization mapped to unrelated government domain is eliminated as MISMATCH."""
        row = self.make_row("NATIONAL KIDNEY AND TRANSPLANT INSTITUTE", "michigan.gov", "PH")
        html = "<title>State of Michigan Official Website</title><h1>Welcome to Michigan.gov</h1>"
        dom = self.make_domain("michigan.gov", html)
        res = evaluate_elimination_decision(row, normalize_domain(row["Domain Name"]), dom)
        self.assertEqual(res["classification"], "MISMATCH")
        self.assertEqual(res["contradiction_type"], "GOVERNMENT_PORTAL")
        self.assertTrue(res["contradiction_found"])

    def test_unrelated_operator_mismatch(self):
        """Website explicitly identifies an unrelated operator with no brand/territory connection."""
        row = self.make_row("Acme Singapore Pte Ltd", "sunflowerbakery.com", "SG")
        html = "<title>Sunflower Bakery - Best Bread in Town</title><h1>Sunflower Bakery</h1><p>Operated by Sunflower Bakery LLC</p>"
        dom = self.make_domain("sunflowerbakery.com", html)
        res = evaluate_elimination_decision(row, normalize_domain(row["Domain Name"]), dom)
        self.assertEqual(res["classification"], "MISMATCH")
        self.assertEqual(res["contradiction_type"], "UNRELATED_ENTITY")

    def test_absence_is_not_negative(self):
        """Absence of exact legal footer or JSON-LD is not negative; candidate mapping with matching brand is VALID."""
        row = self.make_row("SCG CERAMICS PUBLIC COMPANY LIMITED", "scgceramics.com", "TH")
        # Simple active page with brand in domain and compatible context, but no legalName in footer
        html = "<title>SCG Ceramics Products and Innovation</title><p>Ceramic tiles and surfaces.</p>"
        dom = self.make_domain("scgceramics.com", html)
        res = evaluate_elimination_decision(row, normalize_domain(row["Domain Name"]), dom)
        self.assertEqual(res["classification"], "VALID")
        self.assertFalse(res["contradiction_found"])

    def test_inactive_dns_failure(self):
        """Confirmed DNS failure classifies as INACTIVE."""
        row = self.make_row("Unknown Dead Entity", "nonexistentdomainxyz12345.com")
        dom = DomainRecord(
            domain="nonexistentdomainxyz12345.com",
            registered_domain="nonexistentdomainxyz12345.com",
            requested_url="https://nonexistentdomainxyz12345.com/",
            dns_status="FAILED",
            http_status=0,
            domain_active=False,
        )
        res = evaluate_elimination_decision(row, normalize_domain(row["Domain Name"]), dom)
        self.assertEqual(res["classification"], "INACTIVE")
        self.assertTrue(res["contradiction_found"])


if __name__ == "__main__":
    unittest.main()
