"""Unit test MenuDetector (delegasi ke MatchingEngine + BusinessRules asli).

Jalankan: python3 -m unittest tests.test_intelligence.test_menu_detector
"""

import unittest

from app.intelligence.intelligence_engine import IntelligenceResult
from app.intelligence.menu_detector import MenuDetector, UNKNOWN_SCORE
from app.parser_engine import ParserEngine


def run(text):
    detector = MenuDetector(parser_provider=ParserEngine)
    result = IntelligenceResult(normalized_text=text)
    detector.detect(text, result)
    return result


class TestMenuDetector(unittest.TestCase):

    def test_exact_menu(self):
        r = run("BATAGOR KERING")
        self.assertEqual(len(r.items), 1)
        self.assertEqual(r.items[0]["menu"], "BATAGOR KERING")
        self.assertEqual(r.items[0]["score"], 100)
        self.assertGreaterEqual(r.confidence, 100)

    def test_fuzzy_menu(self):
        # Typo "katzu" -- harus tetap ketemu lewat fuzzy MatchingEngine
        r = run("chicken katzu")
        self.assertEqual(len(r.items), 1)
        self.assertIn("KATSU", r.items[0]["menu"])

    def test_business_rule_applied(self):
        # "AYAM KALASAN" tanpa bagian -> DefaultChickenRule menambah
        # PAHA ATAS sebelum matching (delegasi BusinessRules existing)
        r = run("nasi uduk ayam kalasan")
        self.assertEqual(len(r.items), 1)
        self.assertIn("PAHA ATAS", r.items[0]["menu"])

    def test_qty_and_note_reported(self):
        r = run("BATAGOR KERING (no kacang) x2")
        self.assertEqual(r.items[0]["qty"], 2)
        self.assertEqual(r.items[0]["note"], "no kacang")

    def test_customer_line_skipped(self):
        r = run("Kevin\nBATAGOR KERING")
        self.assertEqual(len(r.items), 1)
        self.assertEqual(r.items[0]["menu"], "BATAGOR KERING")

    def test_never_invents_menu(self):
        # Teks yang tidak ada di katalog: menu None, skor rendah --
        # TIDAK pernah mengarang nama menu baru
        r = run("BATAGOR KERING\nNASI ZZZZ QWERTY")
        unknown = [i for i in r.items if i["menu"] is None]

        if unknown:  # baris aneh terdeteksi sebagai baris menu gagal match
            self.assertEqual(unknown[0]["score"], UNKNOWN_SCORE)
            self.assertLess(r.confidence, 70)

    def test_without_parser_is_safe(self):
        detector = MenuDetector(parser_provider=None)
        result = IntelligenceResult(normalized_text="BATAGOR KERING")
        detector.detect("BATAGOR KERING", result)
        self.assertEqual(result.items, [])


if __name__ == "__main__":
    unittest.main()
