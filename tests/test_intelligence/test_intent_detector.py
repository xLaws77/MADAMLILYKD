"""Unit test IntentDetector.

Jalankan: python3 -m unittest tests.test_intelligence.test_intent_detector
"""

import unittest

from app.intelligence.intelligence_engine import IntelligenceResult
from app.intelligence.intent_detector import IntentDetector


def run(text):
    detector = IntentDetector()
    result = IntelligenceResult(normalized_text=text)
    detector.detect(text, result)
    return result


class TestIntentDetector(unittest.TestCase):

    # ---------- ORDER (default) ----------
    def test_plain_order(self):
        r = run("BATAGOR KERING: 12.000R ( 1 )")
        self.assertEqual(r.intent, "ORDER")

    def test_natural_order(self):
        r = run("Kevin\nChicken Katsu dua")
        self.assertEqual(r.intent, "ORDER")

    def test_price_token_always_order(self):
        # Ada harga -> order, walau ada kata "ganti"
        r = run("ganti nasi: 12.000R (2)")
        self.assertEqual(r.intent, "ORDER")

    def test_long_message_always_order(self):
        r = run("a\nb\nc\nd\ne")
        self.assertEqual(r.intent, "ORDER")

    # ---------- CANCEL ----------
    def test_cancel(self):
        for t in ("batal", "batalkan ordernya", "cancel", "ga jadi pesan"):
            self.assertEqual(run(t).intent, "CANCEL", t)

    # ---------- REPEAT ----------
    def test_repeat(self):
        for t in ("pesan lagi", "order lagi dong", "seperti biasa"):
            self.assertEqual(run(t).intent, "REPEAT_ORDER", t)

    # ---------- EDIT ----------
    def test_edit(self):
        for t in ("ganti batagor jadi katsu", "ubah pesanan"):
            self.assertEqual(run(t).intent, "EDIT", t)

    # ---------- ASK PRICE ----------
    def test_ask_price(self):
        for t in ("berapa harga chicken katsu", "chicken katsu berapa", "harganya berapa"):
            self.assertEqual(run(t).intent, "ASK_PRICE", t)

    # ---------- ASK MENU ----------
    def test_ask_menu(self):
        for t in ("menu apa aja", "lihat menu dong", "jual apa", "ada menu apa"):
            self.assertEqual(run(t).intent, "ASK_MENU", t)

    def test_empty(self):
        self.assertEqual(run("").intent, "ORDER")


if __name__ == "__main__":
    unittest.main()
