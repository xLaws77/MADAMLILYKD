"""Unit test QuantityDetector.

Jalankan: python3 -m unittest tests.test_intelligence.test_quantity_detector
"""

import unittest

from app.intelligence.intelligence_engine import IntelligenceResult
from app.intelligence.quantity_detector import QuantityDetector


class _FakeParser:
    """Pengganti ParserEngine untuk is_menu_line -- tanpa akses DB."""

    MENU_WORDS = {"CHICKEN", "KATSU", "BATAGOR", "KUAH", "NASI", "UDUK"}

    def is_menu_line(self, text):
        words = set(text.upper().replace("+", " ").split())
        return bool(words & self.MENU_WORDS)


def run(text, parser=_FakeParser()):
    detector = QuantityDetector(parser_provider=(lambda: parser) if parser else None)
    result = IntelligenceResult(normalized_text=text)
    detector.detect(text, result)
    return result


class TestQuantityDetector(unittest.TestCase):

    # ---------- bentuk "2x" ----------
    def test_prefix_x(self):
        r = run("Chicken Katsu 2x")
        self.assertEqual(r.normalized_text, "Chicken Katsu x2")
        self.assertTrue(r.rewritten)
        self.assertGreaterEqual(r.confidence, 90)

    def test_prefix_x_with_space(self):
        r = run("Chicken Katsu 2 x")
        self.assertEqual(r.normalized_text, "Chicken Katsu x2")

    # ---------- bentuk "5 pcs" ----------
    def test_pcs(self):
        r = run("Batagor Kuah 5 pcs")
        self.assertEqual(r.normalized_text, "Batagor Kuah x5")

    def test_pcs_glued(self):
        r = run("Batagor Kuah 5pcs")
        self.assertEqual(r.normalized_text, "Batagor Kuah x5")

    # ---------- kata bilangan ----------
    def test_word_number_suffix(self):
        r = run("Chicken Katsu dua")
        self.assertEqual(r.normalized_text, "Chicken Katsu x2")
        self.assertGreaterEqual(r.confidence, 70)

    def test_word_number_prefix(self):
        r = run("tiga Batagor Kuah")
        self.assertEqual(r.normalized_text, "Batagor Kuah x3")

    def test_word_number_needs_parser(self):
        # Tanpa parser helper: konservatif, tidak diubah
        r = run("Chicken Katsu dua", parser=None)
        self.assertFalse(r.rewritten)

    def test_word_number_not_menu_line(self):
        # Sisa baris bukan menu -> jangan diubah (bisa jadi nama orang)
        r = run("Dua Lipa")
        self.assertFalse(r.rewritten)

    # ---------- bentuk standar TIDAK disentuh ----------
    def test_existing_paren_qty_untouched(self):
        r = run("BATAGOR KERING: 12.000R ( 1 )")
        self.assertFalse(r.rewritten)

    def test_existing_x_qty_untouched(self):
        r = run("Chicken Katsu x2")
        self.assertFalse(r.rewritten)

    def test_price_line_untouched(self):
        r = run("CHICKEN KATSU+RICE : 12.000R = 2")
        self.assertFalse(r.rewritten)

    def test_menu_with_number_and_qty_after(self):
        # "HOKI 7 x 2": x milik qty 2, angka 7 bagian nama menu
        r = run("HOKI 7 x 2")
        self.assertFalse(r.rewritten)  # sudah bentuk standar (x 2)

    # ---------- multi-baris ----------
    def test_multiline_mixed(self):
        r = run("Chicken Katsu 2x\nBATAGOR KUAH: 12.000R ( 1 )")
        self.assertEqual(
            r.normalized_text,
            "Chicken Katsu x2\nBATAGOR KUAH: 12.000R ( 1 )",
        )
        self.assertTrue(r.rewritten)

    def test_empty_line_safe(self):
        r = run("")
        self.assertFalse(r.rewritten)

    # ---------- regresi: kualifikasi di dalam kurung BUKAN qty ----------
    def test_pcs_inside_paren_is_menu_description(self):
        # "(5PCS)" bagian nama menu ("EGG CHICKEN ROLL (5PCS)+RICE"),
        # bukan qty. Pernah bikin qty jadi 5.
        r = run("EGG CHICKEN ROLL (5PCS)+RICE : 12.000R")
        self.assertFalse(r.rewritten)
        self.assertEqual(r.normalized_text, "EGG CHICKEN ROLL (5PCS)+RICE : 12.000R")

    def test_pcs_inside_nested_desc_is_menu_description(self):
        # "3PCS" ada di dalam kurung deskripsi, bukan qty.
        r = run("HOKI 1 (EGG ROLL 3PCS+CHICKEN TERIYAKI): 16.000R")
        self.assertFalse(r.rewritten)

    def test_pcs_outside_paren_still_qty(self):
        # Di luar kurung, "5 pcs" tetap dianggap qty (perilaku lama).
        r = run("BATAGOR KUAH 5 pcs")
        self.assertEqual(r.normalized_text, "BATAGOR KUAH x5")


if __name__ == "__main__":
    unittest.main()
