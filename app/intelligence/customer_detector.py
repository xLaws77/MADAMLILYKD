"""
customer_detector.py

Mendeteksi NAMA CUSTOMER di teks order dan melaporkannya sebagai
metadata (IntelligenceResult.customer) + logging.

PENTING -- kenapa detector ini TIDAK menulis ulang teks:
ParserEngine lama SUDAH menangani semua pola customer dari spec
(diverifikasi dengan test nyata):

    "Kevin\\nChicken Katsu"     -> customer di ATAS menu     (is_customer_line)
    "Chicken Katsu\\nKevin"     -> customer di BAWAH menu    (pending_items + flush)
    "Chicken Katsu (Kevin)"     -> customer dalam kurung     (detect_inline_customer)

Menulis ulang logika itu = duplikasi yang dilarang. Detector ini
murni DELEGASI: memakai is_customer_line / detect_inline_customer /
_normalize_customer_name milik ParserEngine untuk mengisi metadata,
teks dibiarkan apa adanya untuk diproses parser seperti biasa.
"""

try:
    from ..logger import info
except ImportError:
    try:
        from app.logger import info
    except ImportError:
        def info(msg): print(f"INFO: {msg}")


class CustomerDetector:
    """Deteksi customer (read-only, delegasi penuh ke ParserEngine)."""

    def __init__(self, parser_provider=None):
        self._parser_provider = parser_provider
        self._parser = None

    # ------------------------------------------------------------------
    def detect(self, text, result):
        parser = self._get_parser()

        if parser is None:
            return  # tanpa parser helper tidak ada yang bisa dilaporkan

        customers = []

        for line in text.split("\n"):
            line = line.strip()

            if not line:
                continue

            name = self._detect_in_line(parser, line)

            if name and name not in customers:
                customers.append(name)

        if customers:
            result.customer = customers[0]
            info(f"[INTELLIGENCE] Detected Customer: {', '.join(customers)}")

    # ------------------------------------------------------------------
    def _detect_in_line(self, parser, line):
        # 1) Nama nempel di baris menu: "(kevin)", "> kevin", "- kevin",
        #    "...12.000R kevin" -- logika existing parser.
        inline = parser.detect_inline_customer(line)

        if inline:
            return parser._normalize_customer_name(inline)

        # 2) Baris nama berdiri sendiri ("Kevin" di atas/bawah menu) --
        #    kriteria yang sama dengan yang parser pakai saat parse.
        if parser.is_customer_line(line) and not parser.is_price_or_qty_text(line):
            candidate = line.split(":", 1)[-1].strip() if ":" in line else line

            if parser._looks_like_name(candidate):
                return parser._normalize_customer_name(candidate)

        return None

    # ------------------------------------------------------------------
    def _get_parser(self):
        if self._parser is None and self._parser_provider is not None:
            try:
                self._parser = self._parser_provider()
            except Exception:
                self._parser_provider = None

        return self._parser
