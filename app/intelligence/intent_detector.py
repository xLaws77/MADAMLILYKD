"""
intent_detector.py

Mengklasifikasi MAKSUD pesan: ORDER (default), CANCEL, EDIT,
REPEAT_ORDER, ASK_MENU, ASK_PRICE.

Prinsip kehati-hatian:
- Pesan yang mengandung TOKEN HARGA ("12.000R") selalu ORDER --
  apapun kata lain di dalamnya. Order sungguhan tidak boleh nyasar.
- Intent lain hanya di-set kalau polanya eksplisit (kata tanya /
  kata batal yang jelas). Ragu = ORDER, biar pipeline lama yang
  menangani seperti biasa.

Detector ini metadata-only: TIDAK mengubah teks. Keputusan membalas
pertanyaan (ASK_MENU/ASK_PRICE) ada di TelegramAdapter.
"""

import re

try:
    from ..logger import info
except ImportError:
    try:
        from app.logger import info
    except ImportError:
        def info(msg): print(f"INFO: {msg}")


_PRICE_TOKEN_RE = re.compile(r"\d[\d.,]*\s*[Rr]\b")

# Urutan = prioritas. Pola pertama yang kena, itu intent-nya.
_INTENT_PATTERNS = [
    (
        "CANCEL",
        re.compile(
            r"\b(batal(kan)?|cancel|ga jadi|gak jadi|nggak jadi|tidak jadi)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "REPEAT_ORDER",
        re.compile(
            r"\b(pesan lagi|order lagi|repeat( order)?|ulangi( order)?|"
            r"sama (seperti|kayak) (kemarin|biasa)|seperti biasa)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "EDIT",
        re.compile(
            r"\b(ganti|ubah|tukar|edit)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ASK_PRICE",
        re.compile(
            r"\b(berapa+n?\s*(harga|duit)?|harga(nya)?\s*berapa+|"
            r"how much)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ASK_MENU",
        re.compile(
            r"\b(menu apa( aja| saja)?|ada (menu|apa( aja| saja)?)|"
            r"daftar menu|list menu|lihat menu|jual apa|menu hari ini|"
            r"menu ?nya)\b",
            re.IGNORECASE,
        ),
    ),
]


class IntentDetector:
    """Klasifikasi intent pesan. Satu tanggung jawab: INTENT."""

    def detect(self, text, result):
        result.intent = self._classify(text)

        if result.intent != "ORDER":
            info(f"[INTELLIGENCE] Detected Intent: {result.intent}")

    # ------------------------------------------------------------------
    @staticmethod
    def _classify(text):
        stripped = (text or "").strip()

        if not stripped:
            return "ORDER"

        # Ada harga -> pasti order, kata apapun di sekitarnya.
        if _PRICE_TOKEN_RE.search(stripped):
            return "ORDER"

        # Pesan panjang (banyak baris) hampir pasti order, bukan
        # pertanyaan/batal -- jangan salah klasifikasi.
        if stripped.count("\n") >= 3:
            return "ORDER"

        for intent, pattern in _INTENT_PATTERNS:
            if pattern.search(stripped):
                return intent

        return "ORDER"
