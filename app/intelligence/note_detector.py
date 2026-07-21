"""
note_detector.py

Mendeteksi CATATAN pesanan yang ditulis bebas tanpa kurung, lalu
membungkusnya jadi "(...)" -- format yang ParserEngine.detect_note()
sudah mengerti.

    "Chicken Katsu no pedas"          -> "Chicken Katsu (no pedas)"
    "Batagor tidak pedas"             -> "Batagor (no pedas)"
    "Katsu tambah sambal : 12.000R"   -> "Katsu (extra sambal) : 12.000R"

Dua keputusan desain penting (karena parser_engine TIDAK boleh diubah):

1. Konektor dinormalkan ke kata yang SUDAH ada di
   ParserEngine.NOTE_PREFIXES ("NO ", "GA ", "EXTRA ", "PAKAI ", dst.)
   supaya isi kurung tidak pernah salah dikira nama customer:
       TIDAK/NGGAK -> NO,  TAMBAH -> EXTRA,  PAKE -> PAKAI
2. Kurung note ditaruh DI POSISI frasa aslinya (bukan di akhir baris)
   supaya di baris berformat "MENU ... : HARGA (QTY)" kurung note tetap
   berada SEBELUM kurung qty -- detect_note() mengambil kurung pertama
   yang bukan nama/angka.
"""

import re

# Konektor -> bentuk normal yang dikenali NOTE_PREFIXES parser lama
_NEGATIONS = {
    "NO": "NO", "TIDAK": "NO", "TAK": "NO", "GA": "GA",
    "GAK": "GAK", "NGGAK": "GA", "TANPA": "TANPA", "JANGAN": "JANGAN",
}
_ADDITIONS = {
    "TAMBAH": "EXTRA", "EXTRA": "EXTRA", "PAKAI": "PAKAI", "PAKE": "PAKAI",
}

# Objek catatan yang umum di kantin (closed set -- sengaja bukan kata
# bebas supaya nama menu tidak pernah ikut termakan).
_INGREDIENTS = (
    "ES BATU", "PEDAS", "SAMBAL", "BAWANG", "SAYUR", "KACANG",
    "NASI", "CABE", "SAUS", "KECAP", "MAYO", "TELUR", "KUAH", "ES",
)

_CONNECTOR_RE = "|".join(list(_NEGATIONS) + list(_ADDITIONS))
_INGREDIENT_RE = "|".join(_INGREDIENTS)

# "no pedas", "tambah es batu", dst.
_PHRASE_RE = re.compile(
    rf"\b({_CONNECTOR_RE})\s+({_INGREDIENT_RE})\b", re.IGNORECASE
)

# "pedas" berdiri sendiri (minta pedas) -- dinormalkan ke "EXTRA PEDAS"
# supaya pasti dikenali sebagai note, bukan nama customer.
_LONE_PEDAS_RE = re.compile(r"\bPEDAS\b", re.IGNORECASE)


def _mask_parens(line):
    """Salinan baris dengan isi kurung diganti '#' (panjang sama) --
    dipakai supaya pencarian frasa TIDAK menyentuh isi kurung yang
    sudah ada (itu wilayah detect_note/detect_qty parser lama)."""

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


class NoteDetector:
    """Deteksi & normalisasi catatan bebas per baris. Satu tanggung
    jawab: NOTE."""

    CONF = 90

    def __init__(self, parser_provider=None):
        # parser_provider: callable -> ParserEngine. Dipakai untuk
        # PENGAMAN tabrakan nama menu: katalog punya menu yang namanya
        # mengandung frasa mirip note (mis. "LEMPER PEDAS", "BAKUT
        # SAYUR ASIN TANPA NASI") -- baris seperti itu TIDAK boleh
        # diubah. Dicek lewat MatchingEngine existing (reuse).
        self._parser_provider = parser_provider
        self._parser = None

    def detect(self, text, result):
        lines = text.split("\n")
        changed = False

        for i, line in enumerate(lines):
            new_line = self._rewrite_line(line)

            if new_line != line:
                lines[i] = new_line
                changed = True

        if changed:
            result.normalized_text = "\n".join(lines)
            result.rewritten = True
            result.merge_confidence(self.CONF)

    # ------------------------------------------------------------------
    def _rewrite_line(self, line):
        if not line or "(" in line and ")" not in line:
            return line

        masked = _mask_parens(line)
        spans = []   # (start, end)
        notes = []   # teks note yang sudah dinormalkan

        for m in _PHRASE_RE.finditer(masked):
            connector = m.group(1).upper()
            ingredient = m.group(2).upper()
            normal = _NEGATIONS.get(connector) or _ADDITIONS[connector]
            spans.append((m.start(), m.end()))
            notes.append(f"{normal} {ingredient}")

        # "pedas" berdiri sendiri: hanya kalau tidak sudah termasuk
        # frasa berkonektor di atas.
        for m in _LONE_PEDAS_RE.finditer(masked):
            if any(s <= m.start() < e for s, e in spans):
                continue
            spans.append((m.start(), m.end()))
            notes.append("EXTRA PEDAS")

        if not spans:
            return line

        # PENGAMAN: kalau baris ini cocok ke menu katalog yang namanya
        # memang mengandung frasa tsb (mis. "LEMPER PEDAS"), itu bagian
        # NAMA MENU, bukan note -- jangan diubah.
        if self._phrase_is_part_of_menu_name(line, notes):
            return line

        # Kalau setelah frasa dibuang barisnya kosong (baris CUMA berisi
        # note, tanpa menu), jangan diubah -- di luar tanggung jawab
        # detector ini (note tanpa item induk).
        remainder = line
        for s, e in sorted(spans, reverse=True):
            remainder = remainder[:s] + remainder[e:]

        if not re.sub(r"[\s:;,+\-=.]+", "", remainder):
            return line

        # Ganti frasa PERTAMA dengan "(note1, note2)" di posisinya,
        # frasa lain dibuang.
        spans_sorted = sorted(spans)
        first_start = spans_sorted[0][0]
        combined = "(" + ", ".join(notes).lower() + ")"

        for s, e in sorted(spans, reverse=True):
            line = line[:s] + line[e:]

        new_line = line[:first_start].rstrip()
        rest = line[first_start:].lstrip()
        new_line = f"{new_line} {combined}" + (f" {rest}" if rest else "")

        return re.sub(r"\s+", " ", new_line).strip()

    # ------------------------------------------------------------------
    def _phrase_is_part_of_menu_name(self, line, notes):
        parser = self._get_parser()

        if parser is None:
            return False

        try:
            cleaned = parser.normalizer.clean(line)
            menu, _score = parser.matcher.search(cleaned)
        except Exception:
            return False

        if menu is None:
            return False

        name = (menu.get("name") or "").upper()

        # Bandingkan per KATA OBJEK note (PEDAS, NASI, ...) -- konektor
        # sudah dinormalkan jadi bisa beda dari teks menu.
        for note in notes:
            ingredient = note.split(" ", 1)[-1]

            if ingredient in name:
                return True

        return False

    def _get_parser(self):
        if self._parser is None and self._parser_provider is not None:
            try:
                self._parser = self._parser_provider()
            except Exception:
                self._parser_provider = None

        return self._parser
