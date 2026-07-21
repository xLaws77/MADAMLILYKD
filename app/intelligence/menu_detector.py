"""
menu_detector.py

Mendeteksi MENU di tiap baris order dan mengisi metadata
IntelligenceResult.items ({menu, qty, note, score}) + confidence.

ATURAN UTAMA: detector ini TIDAK PERNAH membuat/mengarang menu
sendiri. Semua pencarian didelegasikan ke MatchingEngine.search()
existing, yang urutan prioritasnya sudah persis seperti spec:

    Koreksi hasil belajar -> Exact (nama/alias) -> Prefix -> Fuzzy -> Unknown

Business rule (mis. default PAHA ATAS) diterapkan lewat
BusinessRules.apply() existing SEBELUM pencarian -- sama seperti yang
ParserEngine lakukan di jalur parse sungguhan.

Sumbangan confidence: skor match TERENDAH di antara baris menu.
Baris menu yang tidak ketemu sama sekali (Unknown) menurunkan
confidence ke bawah ambang -> teks asli dipakai, dan gate AI hybrid
existing yang memutuskan langkah berikutnya.
"""

try:
    from ..business_rules import BusinessRules
    from ..logger import info
except ImportError:
    from business_rules import BusinessRules
    try:
        from app.logger import info
    except ImportError:
        def info(msg): print(f"INFO: {msg}")

# Skor yang dilaporkan saat baris menu tidak ketemu di katalog --
# sengaja di bawah ambang INTELLIGENCE_CONFIDENCE supaya hasil
# intelligence tidak dipakai dan jalur lama (+ AI) yang menangani.
UNKNOWN_SCORE = 30


class MenuDetector:
    """Deteksi menu per baris. Satu tanggung jawab: MENU (delegasi
    penuh ke BusinessRules + MatchingEngine)."""

    def __init__(self, parser_provider=None):
        self._parser_provider = parser_provider
        self._parser = None

    # ------------------------------------------------------------------
    def detect(self, text, result):
        parser = self._get_parser()

        if parser is None:
            return

        min_score = None

        for line in text.split("\n"):
            line = line.strip()

            if not line:
                continue

            # Baris nama customer murni bukan urusan detector ini.
            if not parser.is_menu_line(line) and parser.is_customer_line(line):
                continue

            found = self._match_line(parser, line)

            if found is None:
                continue

            menu, score, qty, note = found
            result.items.append(
                {
                    "menu": menu["name"] if menu else None,
                    "price": menu.get("price", 0) if menu else 0,
                    "qty": qty,
                    "note": note,
                    "score": score,
                }
            )

            min_score = score if min_score is None else min(min_score, score)

            info(
                "[INTELLIGENCE] Detected Menu: "
                f"{menu['name'] if menu else 'UNKNOWN'} "
                f"(score={score}, qty={qty})"
            )

        if min_score is not None:
            result.merge_confidence(min_score)

    # ------------------------------------------------------------------
    def _match_line(self, parser, line):
        """Return (menu|None, score, qty, note) atau None kalau baris
        bukan baris menu sama sekali."""

        if not parser.is_menu_line(line):
            return None

        qty = parser.detect_qty(line)
        note = parser.detect_note(line)

        # Urutan SAMA dengan jalur parse asli: bersihkan dulu, lalu
        # business rules, lalu MatchingEngine.
        cleaned = parser.normalizer.clean(line)
        ruled = BusinessRules.apply(cleaned)
        menu, score = parser.matcher.search(ruled)

        if menu is None:
            return None, UNKNOWN_SCORE, qty, note

        return menu, score, qty, note

    # ------------------------------------------------------------------
    def _get_parser(self):
        if self._parser is None and self._parser_provider is not None:
            try:
                self._parser = self._parser_provider()
            except Exception:
                self._parser_provider = None

        return self._parser
