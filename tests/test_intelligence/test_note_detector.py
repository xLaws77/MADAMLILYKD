"""Unit test NoteDetector.

Jalankan: python3 -m unittest tests.test_intelligence.test_note_detector
"""

import unittest

from app.intelligence.intelligence_engine import IntelligenceResult
from app.intelligence.note_detector import NoteDetector


def run(text):
    detector = NoteDetector()
    result = IntelligenceResult(normalized_text=text)
    detector.detect(text, result)
    return result


class TestNoteDetector(unittest.TestCase):

    # ---------- negasi ----------
    def test_no_pedas(self):
        r = run("Chicken Katsu no pedas")
        self.assertEqual(r.normalized_text, "Chicken Katsu (no pedas)")
        self.assertTrue(r.rewritten)

    def test_tidak_pedas_normalized_to_no(self):
        r = run("Chicken Katsu tidak pedas")
        self.assertEqual(r.normalized_text, "Chicken Katsu (no pedas)")

    def test_ga_pedas(self):
        r = run("Batagor ga pedas")
        self.assertEqual(r.normalized_text, "Batagor (ga pedas)")

    def test_no_bawang(self):
        r = run("Nasi Uduk no bawang")
        self.assertEqual(r.normalized_text, "Nasi Uduk (no bawang)")

    def test_no_sayur(self):
        r = run("Bakmie no sayur")
        self.assertEqual(r.normalized_text, "Bakmie (no sayur)")

    # ---------- tambahan ----------
    def test_tambah_sambal_normalized_to_extra(self):
        r = run("Chicken Katsu tambah sambal")
        self.assertEqual(r.normalized_text, "Chicken Katsu (extra sambal)")

    def test_extra_sambal(self):
        r = run("Chicken Katsu extra sambal")
        self.assertEqual(r.normalized_text, "Chicken Katsu (extra sambal)")

    def test_tambah_nasi(self):
        r = run("Ayam Kalasan tambah nasi")
        self.assertEqual(r.normalized_text, "Ayam Kalasan (extra nasi)")

    def test_tambah_es_batu(self):
        r = run("Es Teh tambah es batu")
        self.assertEqual(r.normalized_text, "Es Teh (extra es batu)")

    # ---------- pedas berdiri sendiri ----------
    def test_lone_pedas(self):
        r = run("Chicken Katsu pedas")
        self.assertEqual(r.normalized_text, "Chicken Katsu (extra pedas)")

    # ---------- posisi: sebelum harga & kurung qty ----------
    def test_note_before_price_and_qty(self):
        r = run("CHICKEN KATSU no pedas : 12.000R (2)")
        self.assertEqual(
            r.normalized_text, "CHICKEN KATSU (no pedas) : 12.000R (2)"
        )

    # ---------- gabungan beberapa note ----------
    def test_multiple_notes_combined(self):
        r = run("Chicken Katsu no pedas tambah sambal")
        self.assertEqual(
            r.normalized_text, "Chicken Katsu (no pedas, extra sambal)"
        )

    # ---------- yang TIDAK boleh diubah ----------
    def test_existing_paren_note_untouched(self):
        r = run("Chicken Katsu (no pedas)")
        self.assertFalse(r.rewritten)

    def test_note_only_line_untouched(self):
        # Baris cuma berisi note tanpa menu -- di luar tanggung jawab
        r = run("no pedas")
        self.assertFalse(r.rewritten)

    def test_plain_order_untouched(self):
        r = run("BATAGOR KERING: 12.000R ( 1 )")
        self.assertFalse(r.rewritten)

    def test_menu_word_nasi_untouched(self):
        # "NASI" bagian nama menu, bukan note -- tidak ada konektor
        r = run("NASI UDUK KOMPLIT: 10.000R")
        self.assertFalse(r.rewritten)

    # ---------- tabrakan dengan nama menu di katalog ----------
    def test_menu_name_containing_phrase_untouched(self):
        # "LEMPER PEDAS" adalah NAMA MENU -- "pedas" bukan note
        class _FakeNormalizer:
            def clean(self, t):
                return t.upper()

        class _FakeMatcher:
            def search(self, t):
                return {"name": "LEMPER PEDAS"}, 100

        class _FakeParser:
            normalizer = _FakeNormalizer()
            matcher = _FakeMatcher()

        detector = NoteDetector(parser_provider=lambda: _FakeParser())
        result = IntelligenceResult(normalized_text="Lemper pedas 12.000R")
        detector.detect("Lemper pedas 12.000R", result)
        self.assertFalse(result.rewritten)

    # ---------- multi-baris ----------
    def test_multiline(self):
        r = run("Chicken Katsu no pedas\nBATAGOR KUAH: 12.000R ( 1 )")
        self.assertEqual(
            r.normalized_text,
            "Chicken Katsu (no pedas)\nBATAGOR KUAH: 12.000R ( 1 )",
        )


if __name__ == "__main__":
    unittest.main()
