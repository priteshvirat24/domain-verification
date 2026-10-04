import csv
import tempfile
import unittest
from pathlib import Path

from verifier.audit import score_audit


class AuditTests(unittest.TestCase):
    def test_unlabeled_rows_do_not_count_as_accuracy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audit.csv'
            with path.open('w', newline='', encoding='utf-8') as file:
                writer = csv.DictWriter(file, fieldnames=['input_row_id', 'decision', 'verification_status', 'proof_level', 'human_decision'])
                writer.writeheader()
                writer.writerows([
                    {'input_row_id': 2, 'decision': 'ACCEPT', 'verification_status': 'VERIFIED_EXACT', 'proof_level': 'STRONG', 'human_decision': 'ACCEPT'},
                    {'input_row_id': 3, 'decision': 'ACCEPT', 'verification_status': 'VERIFIED_EXACT', 'proof_level': 'STRONG', 'human_decision': 'REJECT'},
                    {'input_row_id': 4, 'decision': 'NEEDS_REVIEW', 'verification_status': 'UNVERIFIED', 'proof_level': 'NONE', 'human_decision': ''},
                ])
            result = score_audit(path)
            self.assertEqual(result['labeled_rows'], 2)
            self.assertEqual(result['unlabeled_rows'], 1)
            self.assertEqual(result['accuracy'], 0.5)
            self.assertFalse(result['by_rule']['proof:STRONG']['meets_95_percent'])
