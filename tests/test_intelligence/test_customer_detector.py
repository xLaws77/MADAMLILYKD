"""Unit test CustomerDetector (delegasi ke ParserEngine asli).

Jalankan: python3 -m unittest tests.test_intelligence.test_customer_detector
"""

import unittest

from app.intelligence.intelligence_engine import IntelligenceResult
from app.intelligence.customer_detector import CustomerDetector
from app.parser_engine import ParserEngine


def run(text):
    detector = CustomerDetector(parser_provider=ParserEngine)
    result = IntelligenceResult(normalized_text=text)
    detector.detect(text, result)
    return result


class TestCustomerDetector(unittest.TestCase):

    def test_name_above_menu(self):
        r = run("Kevin\nChicken Katsu")
        self.assertEqual(r.customer, "KEVIN")

    def test_name_below_menu(self):
        r = run("Chicken Katsu\nKevin")
        self.assertEqual(r.customer, "KEVIN")

    def test_name_in_parens(self):
        r = run("Chicken Katsu (Kevin)")
        self.assertEqual(r.customer, "KEVIN")

    def test_any_name(self):
        for name in ("Captain", "Fransen", "Lukman"):
            r = run(f"{name}\nChicken Katsu")
            self.assertEqual(r.customer, name.upper(), name)

    def test_no_customer(self):
        r = run("BATAGOR KERING: 12.000R ( 1 )")
        self.assertEqual(r.customer, "")

    def test_note_paren_not_customer(self):
        r = run("Chicken Katsu (no pedas)")
        self.assertEqual(r.customer, "")

    def test_text_never_rewritten(self):
        r = run("Kevin\nChicken Katsu")
        self.assertFalse(r.rewritten)
        self.assertEqual(r.normalized_text, "Kevin\nChicken Katsu")

    def test_without_parser_is_safe(self):
        detector = CustomerDetector(parser_provider=None)
        result = IntelligenceResult(normalized_text="Kevin\nChicken Katsu")
        detector.detect("Kevin\nChicken Katsu", result)
        self.assertEqual(result.customer, "")


if __name__ == "__main__":
    unittest.main()
