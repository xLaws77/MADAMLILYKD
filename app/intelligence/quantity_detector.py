"""
quantity_detector.py

Menormalkan cara customer menulis JUMLAH ke bentuk "x<N>" yang sudah
dimengerti ParserEngine.detect_qty() (pola "[xX]\\s*(\\d+)").

Bentuk yang dinormalkan:
    "Chicken Katsu 2x"      -> "Chicken Katsu x2"
    "Batagor 5 pcs"         -> "Batagor x5"
    "Chicken Katsu dua"     -> "Chicken Katsu x2"
    "tiga Batagor Kuah"     -> "Batagor Kuah x3"

Bentuk yang SUDAH dimengerti parser ("(2)", "x2", "@2", "=2") TIDAK
disentuh -- biar satu sumber kebenaran tetap di ParserEngine.

Kata bilangan hanya dinormalkan kalau sisa barisnya masih terlihat
seperti baris menu (dicek pakai ParserEngine.is_menu_line() yang sudah
ada) -- mencegah salah ubah nama menu/customer yang kebetulan
mengandung kata itu.
"""

import re

# Angka diikuti x, mis. "2x" / "2 x" -- tapi BUKAN "7 x 2" (x-nya milik
# qty di belakang) dan BUKAN bagian harga ("12.000R").
_NUM_X_RE = re.compile(r"(?<![\d.,])(\d{1,2})\s*[xX]\b(?!\s*\d)")

# "5 pcs" / "5pcs" / "5 porsi" / "5 biji"
_NUM_PCS_RE = re.compile(
    r"(?<![\d.,])(\d{1,3})\s*(?:PCS|PC|PORSI|BIJI)\b", re.IGNORECASE
)

# Kata bilangan Indonesia (berdiri sendiri sebagai kata)
_WORD_NUMBERS = {
    "SATU": 1, "DUA": 2, "TIGA": 3, "EMPAT": 4, "LIMA": 5,
    "ENAM": 6, "TUJUH": 7, "DELAPAN": 8, "SEMBILAN": 9, "SEPULUH": 10,
}

_WORD_NUM_RE = re.compile(
    r"\b(" + "|".join(_WORD_NUMBERS) + r")\b(\s*(?:PCS|PC|PORSI|BIJI))?\b",
    re.IGNORECASE,
)

# Penanda qty yang SUDAH dimengerti ParserEngine.detect_qty() -- kalau
# sudah ada, baris tidak perlu (dan tidak boleh) diubah lagi.
_EXISTING_QTY_RE = re.compile(r"\(\s*\d+\s*\)|[xX]\s*\d|[@#=]\s*\d|:\s*\d+\s*$")


def _mask_parens(line):
    """Salinan baris dengan isi kurung diganti '#' (panjang tetap sama).

    Isi kurung di baris menu biasanya deskripsi -- mis. "(5PCS)+RICE",
    "(EGG ROLL 3PCS+CHICKEN TERIYAKI)", "(no pedas)" -- BUKAN qty. Tanpa
    masking ini, regex "5PCS" ketemu di dalam "(5PCS)" lalu qty jadi 5,
    padahal pesanan cuma 1 porsi. Setelah pencarian pakai versi masked,
    substitusi tetap dilakukan di baris ASLI di posisi yang sama."""

    chars = list(line)
    depth = 0

    for i, c in enumerate(chars):
        if c == "(":
            depth += 1
            chars[i] = "#"
        elif c == ")":
            depth = max(0, depth - 1)
            chars[i] = "#"
        elif depth > 0:
            chars[i] = "#"

    return "".join(chars)


class QuantityDetector:
    """Deteksi & normalisasi qty per baris. Satu tanggung jawab: JUMLAH."""

    # Confidence untuk bentuk angka (deterministik) vs kata bilangan
    CONF_NUMERIC = 95
    CONF_WORD = 85

    def __init__(self, parser_provider=None):
        # parser_provider: callable tanpa argumen -> instance ParserEngine
        # (dipakai untuk is_menu_line; None = lewati normalisasi kata
        # bilangan yang butuh pengecekan itu).
        self._parser_provider = parser_provider
        self._parser = None

    # ------------------------------------------------------------------
    def detect(self, text, result):
        lines = text.split("\n")
        changed = False
        min_conf = 100

        for i, line in enumerate(lines):
            new_line, conf = self._rewrite_line(line)

            if new_line != line:
                lines[i] = new_line
                changed = True
                min_conf = min(min_conf, conf)

        if changed:
            result.normalized_text = "\n".join(lines)
            result.rewritten = True
            result.merge_confidence(min_conf)

    # ------------------------------------------------------------------
    def _rewrite_line(self, line):
        """Return (baris_baru, confidence). Baris tak berubah kalau
        tidak ada bentuk qty non-standar yang aman untuk diubah."""

        if not line or not line.strip():
            return line, 100

        # Sudah ada penanda qty standar -> jangan sentuh.
        if _EXISTING_QTY_RE.search(line):
            return line, 100

        # Cari qty HANYA di luar kurung -- isi kurung biasanya bagian
        # nama menu (mis. "(5PCS)+RICE") atau catatan. Search pakai
        # versi masked, tapi substitusi tetap di posisi yang sama pada
        # baris asli (indeks tidak bergeser karena masking pengganti
        # per karakter).
        masked = _mask_parens(line)

        # 1) "5 pcs" -> "x5"
        m = _NUM_PCS_RE.search(masked)
        if m:
            new = line[: m.start()] + line[m.end():]
            new = new.rstrip()
            return f"{new} x{int(m.group(1))}", self.CONF_NUMERIC

        # 2) "2x" -> "x2"
        m = _NUM_X_RE.search(masked)
        if m:
            new = line[: m.start()] + line[m.end():]
            new = new.rstrip()
            return f"{new} x{int(m.group(1))}", self.CONF_NUMERIC

        # 3) Kata bilangan ("dua", "tiga pcs") -- hanya kalau sisa
        #    barisnya masih terbaca sebagai baris menu oleh parser lama.
        m = _WORD_NUM_RE.search(masked)
        if m:
            qty = _WORD_NUMBERS[m.group(1).upper()]
            remainder = (line[: m.start()] + line[m.end():]).strip()
            remainder = re.sub(r"\s+", " ", remainder)

            if remainder and self._is_menu_line(remainder):
                return f"{remainder} x{qty}", self.CONF_WORD

        return line, 100

    # ------------------------------------------------------------------
    def _is_menu_line(self, text):
        parser = self._get_parser()

        if parser is None:
            return False  # tanpa parser helper: konservatif, jangan ubah

        try:
            return bool(parser.is_menu_line(text))
        except Exception:
            return False

    def _get_parser(self):
        if self._parser is None and self._parser_provider is not None:
            try:
                self._parser = self._parser_provider()
            except Exception:
                self._parser_provider = None  # jangan coba terus-terusan

        return self._parser
