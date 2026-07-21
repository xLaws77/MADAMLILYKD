"""Test integrasi Smart Order Intelligence end-to-end.

Memakai ParserEngine + MatchingEngine + menu database SUNGGUHAN
(bukan mock) -- memastikan lapisan intelijen dan pipeline lama
bekerja sama tanpa mengubah perilaku format order yang sudah ada.

Jalankan: python3 -m unittest tests.test_intelligence.test_integration
"""

import unittest

from app.config import INTELLIGENCE_CONFIDENCE
from app.intelligence import IntelligenceEngine
from app.parser_engine import ParserEngine


def analyze(text):
    engine = IntelligenceEngine(parser_provider=ParserEngine)
    return engine.analyze(text)


class TestIntegration(unittest.TestCase):

    # ------------------------------------------------------------------
    # Format LAMA tidak boleh berubah (backward compatibility)
    # ------------------------------------------------------------------
    def test_standard_format_passthrough(self):
        text = (
            "BATAGOR KERING: 12.000R ( 1 )\n"
            "NASI UDUK AYAM GORENG: 15.000R ( 2 )\n"
            "ES TEH MANIS: 5.000R ( 3 )"
        )
        r = analyze(text)
        # Teks standar tidak butuh rewrite -- parser lama membacanya
        self.assertEqual(r.normalized_text, text)
        self.assertFalse(r.rewritten)

    def test_standard_with_customer_passthrough(self):
        text = "🍱CHICKEN KATSU+RICE : 12.000R (Rafly)"
        r = analyze(text)
        self.assertEqual(r.normalized_text, text)

    # ------------------------------------------------------------------
    # Order natural: dinormalkan DAN hasil parse-nya benar
    # ------------------------------------------------------------------
    def test_natural_order_high_confidence(self):
        r = analyze("Kevin\nChicken Katsu 2x no pedas")

        self.assertTrue(r.rewritten)
        self.assertGreaterEqual(r.confidence, INTELLIGENCE_CONFIDENCE)
        self.assertEqual(r.customer, "KEVIN")
        self.assertEqual(r.intent, "ORDER")

        # Teks hasil normalisasi harus terbaca benar oleh parser LAMA
        parser = ParserEngine()
        parser.parse(r.normalized_text)
        groups = parser.group_by_customer()

        self.assertIn("KEVIN", groups)
        item = groups["KEVIN"][0]
        self.assertEqual(item.menu, "CHICKEN KATSU+RICE")
        self.assertEqual(item.qty, 2)
        self.assertEqual(item.note, "no pedas")

    # ------------------------------------------------------------------
    # Menu tak dikenal -> confidence < threshold -> teks asli dipakai
    # (jalur lama + gate AI existing yang menangani)
    # ------------------------------------------------------------------
    def test_unknown_menu_low_confidence(self):
        r = analyze("Nasi Zzzz Qwerty 2x")

        if r.rewritten:
            self.assertLess(r.confidence, INTELLIGENCE_CONFIDENCE)

    # ------------------------------------------------------------------
    # Intent non-order terdeteksi tanpa merusak teks
    # ------------------------------------------------------------------
    def test_ask_menu_intent(self):
        r = analyze("menu apa aja?")
        self.assertEqual(r.intent, "ASK_MENU")

    def test_ask_price_intent_with_item(self):
        r = analyze("berapa harga chicken katsu")
        self.assertEqual(r.intent, "ASK_PRICE")
        matched = [i for i in r.items if i.get("menu")]
        self.assertTrue(matched)
        self.assertGreater(matched[0]["price"], 0)

    # ------------------------------------------------------------------
    # Fail-open: error internal tidak boleh mematahkan pipeline
    # ------------------------------------------------------------------
    def test_fail_open(self):
        engine = IntelligenceEngine(parser_provider=ParserEngine)

        class Broken:
            def detect(self, text, result):
                raise RuntimeError("boom")

        engine.detectors.insert(0, Broken())
        r = engine.analyze("BATAGOR KERING: 12.000R ( 1 )")

        self.assertEqual(r.normalized_text, "BATAGOR KERING: 12.000R ( 1 )")
        self.assertEqual(r.confidence, 0)

    # ------------------------------------------------------------------
    # Contoh order asli yang dulu pernah error (regresi NoneType)
    # ------------------------------------------------------------------
    def test_historic_order_regression(self):
        text = (
            "BATAGOR KERING: 12.000R ( 1 )\n"
            "NASI UDUK AYAM GORENG: 15.000R ( 2 )\n"
            "ES TEH MANIS: 5.000R ( 3 )"
        )
        r = analyze(text)

        parser = ParserEngine()
        parser.parse(r.normalized_text)
        groups = parser.group_by_customer()

        total_items = sum(len(v) for v in groups.values())
        self.assertEqual(total_items, 3)


if __name__ == "__main__":
    unittest.main()
