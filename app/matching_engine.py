"""
matching_engine.py

PERUBAHAN dari versi sebelumnya:
1. Menu sekarang di-load dari data/menu.xlsx (lewat menu_loader), bukan
   data/menu.json. Satu sumber data, gampang di-update lewat Excel.
2. FIX BUG: exact-match alias sebelumnya cuma mengecek alias dari menu
   TERAKHIR di list (looping-nya ada di luar for-loop menu), jadi alias
   menu lain tidak pernah ke-cek. Sekarang exact-match alias dicek untuk
   SEMUA menu.
3. Threshold fuzzy match sekarang konsisten pakai FUZZY_MATCH_SCORE dari
   config.py (sebelumnya hardcode 85, beda dengan config yang isinya 80).
"""

import re

from rapidfuzz import fuzz

try:
    from .menu_loader import load_menu_from_excel
    from .config import FUZZY_MATCH_SCORE
except ImportError:
    from menu_loader import load_menu_from_excel
    from config import FUZZY_MATCH_SCORE


def _tokenize(text):
    """
    Pecah teks jadi kata, huruf dan angka dipisah SEKALIPUN nempel
    tanpa spasi (typo umum, mis. "HOKI7" -> ["hoki", "7"]) supaya
    tokenisasinya sama persis dengan "HOKI 7" yang pakai spasi.
    """

    return re.findall(r"[a-z]+|[0-9]+", text.lower())


def correction_key(text):
    """Kunci normalisasi untuk peta "koreksi belajar": tokenizer yang
    sama dipakai baik saat mencatat koreksi maupun saat mencocokkan,
    supaya "NASI biasa  KOMPLIT PERKEDEL" dan "nasi biasa komplit
    perkedel" jadi kunci yang sama persis."""

    return " ".join(_tokenize(text))


class MatchingEngine:

    def __init__(self, menu_path: str = None, corrections=None):
        self.menus = load_menu_from_excel(menu_path)

        # Peta koreksi hasil belajar dari staf: correction_key(teks) ->
        # nama menu yang benar. Dicek PALING AWAL di search() supaya
        # tulisan yang sama yang dulu pernah dikoreksi langsung benar.
        # {} kalau fitur belajar tidak dipakai (mis. dipanggil di luar
        # konteks bot).
        self._corrections = {}
        self._by_name_upper = {m["name"].strip().upper(): m for m in self.menus}
        self.set_corrections(corrections or {})

    def set_corrections(self, corrections):
        """corrections: dict {correction_key -> nama menu}. Hanya entri
        yang nama menunya benar-benar ada di katalog yang dipakai."""

        cleaned = {}

        for key, menu_name in (corrections or {}).items():
            menu = self._by_name_upper.get(str(menu_name).strip().upper())

            if menu is not None:
                cleaned[key] = menu

        self._corrections = cleaned

    def calculate_score(self, text, menu):

        # Nama menu kadang mengandung simbol, mis. "(JUMBO)", "+", yang
        # TIDAK ADA lagi di teks customer setelah dibersihkan Normalizer
        # (simbol diganti spasi). Kalau dibandingkan apa adanya, rapidfuzz
        # menganggap "(jumbo)" beda dari "jumbo", bikin skor varian yang
        # sebenarnya lebih cocok jadi turun drastis. Normalisasi dulu
        # pakai tokenizer yang sama (huruf/angka dipisah, simbol dibuang)
        # supaya perbandingannya apple-to-apple.
        name = " ".join(_tokenize(menu["name"]))

        scores = [
            fuzz.token_sort_ratio(text, name),
            fuzz.partial_ratio(text, name),
            fuzz.token_set_ratio(text, name),
        ]

        for alias in menu.get("aliases", []):
            alias_norm = " ".join(_tokenize(alias))

            # Sengaja HANYA token_sort_ratio untuk alias (bukan
            # partial_ratio/token_set_ratio): keduanya terlalu longgar
            # untuk alias pendek -- begitu semua kata aliasnya muncul di
            # teks input, skornya otomatis ~100 walau teks input itu
            # sebenarnya menu LAIN yang lebih spesifik (mis. alias
            # "KALASAN DADA" utk "NASI+AYAM KALASAN DADA" ikut match
            # sempurna ke input "NASI UDUK+AYAM KALASAN DADA", padahal
            # itu barang lain dengan harga berbeda).
            scores.append(fuzz.token_sort_ratio(text, alias_norm))

        return max(scores)

    def find_prefix_match(self, words):
        """
        Cari menu yang KATA AWALnya cocok persis dengan `words` (list kata
        huruf/angka, sudah lowercase). Kalau ada beberapa yang cocok,
        ambil yang prefix-nya PALING PANJANG (biar "HOKI 10" tidak
        ketiban "HOKI 1"). Dipakai baik oleh search() maupun oleh
        ParserEngine untuk mengecek apakah angka di akhir baris adalah
        qty (bukan bagian dari nama menu).

        SENGAJA cuma cocokkan ke nama menu RESMI, bukan alias. Alias
        biasanya pendek/umum (mis. "AYAM KALASAN" utk "NASI+AYAM
        KALASAN DADA"), dan customer sering tidak menulis kata "NASI"
        di depan menu (mis. cuma "Ayam Kalasan paha atas") -- itu bikin
        nama resmi varian PAHA ATAS jadi lebih panjang dari input dan
        gugur duluan, sementara alias pendek varian DADA menang cuma
        gara-gara kebetulan jadi prefix yang lebih pendek, padahal
        varian yang diminta beda. Kalau tidak ada nama resmi yang
        cocok sebagai prefix, biar fuzzy match (yang sudah bisa
        membedakan varian lewat overlap kata) yang memutuskan.
        """

        best_menu = None
        best_len = 0

        for menu in self.menus:
            # Nama menu pakai "+" tanpa spasi (mis. "NASI+AYAM ..."), jadi
            # split berbasis huruf/angka -- bukan .split() biasa -- supaya
            # kata-katanya sejajar dengan teks input yang sudah dibersihkan
            # Normalizer (di mana "+" jadi spasi).
            cand_words = _tokenize(menu["name"])

            if not cand_words or len(cand_words) > len(words):
                continue

            if words[: len(cand_words)] == cand_words and len(cand_words) > best_len:
                best_len = len(cand_words)
                best_menu = menu

        return best_menu

    def search(self, text):

        text = text.lower().strip()

        # ======================================================
        # KOREKSI HASIL BELAJAR (paling diprioritaskan)
        #
        # Kalau tulisan ini persis sama dengan sesuatu yang dulu pernah
        # dikoreksi staf (mis. "nasi biasa komplit perkedel" yang salah
        # kebaca PERKEDEL, lalu dibetulkan jadi NASI UDUK KOMPLIT
        # PERKEDEL lewat /ganti), langsung pakai menu yang benar itu.
        # ======================================================
        if self._corrections:
            menu = self._corrections.get(correction_key(text))

            if menu is not None:
                return menu, 100

        # ======================================================
        # EXACT MATCH (nama ATAU alias, dicek untuk SEMUA menu)
        # ======================================================
        for menu in self.menus:

            if text == menu["name"].lower():
                return menu, 100

            for alias in menu.get("aliases", []):
                if text == alias.lower():
                    return menu, 100

        # ======================================================
        # PREFIX MATCH (berbasis kata, ambil prefix TERPANJANG)
        #
        # Order sering ditulis "NAMA MENU (deskripsi tambahan) : harga",
        # mis. customer copy-paste keterangan menu apa adanya:
        # "HOKI 7 (EGG ROLL 3PCS+SHRIMP ROLL 2PCS)". Kalau dicocokkan ke
        # SELURUH teks pakai fuzzy score biasa, kata-kata deskripsi itu
        # jadi noise yang bisa membuat menu lain (mis. "HOKI 1") dapat
        # skor sama/lebih tinggi lewat partial_ratio/token_set_ratio.
        # Prefix match memastikan hanya menu yang KATA AWALnya cocok
        # persis dengan teks yang menang, dan yang prefix-nya paling
        # panjang yang dipilih (biar "HOKI 10" tidak ketiban "HOKI 1").
        # ======================================================
        words = _tokenize(text)
        best_prefix_menu = self.find_prefix_match(words)

        if best_prefix_menu is not None:
            return best_prefix_menu, 100

        # ======================================================
        # FUZZY MATCH
        # ======================================================
        scored = [(self.calculate_score(text, menu), menu) for menu in self.menus]

        if not scored:
            return None, 0

        best_score = max(score for score, _ in scored)

        # ======================================================
        # THRESHOLD (konsisten dengan config.FUZZY_MATCH_SCORE)
        # ======================================================
        if best_score < FUZZY_MATCH_SCORE:
            return None, best_score

        # ======================================================
        # TIE-BREAK antar kandidat yang skornya berdekatan
        #
        # partial_ratio/token_set_ratio kadang kasih skor lebih tinggi
        # ke nama yang lebih PENDEK/kurang spesifik dibanding varian
        # yang lebih lengkap, terutama kalau teks inputnya ada typo
        # (mis. "BAKMI BABI PANGGANG JUMBO" -- typo "BAKMI" tanpa "E"
        # -- skornya malah lebih tinggi ke "BAKMIE BABI PANGGANG" biasa
        # dibanding "BAKMIE BABI PANGGANG (JUMBO)" yang sebenarnya lebih
        # cocok). Di antara kandidat yang skornya dekat (selisih <= 5):
        #
        # 1. Menangkan yang OVERLAP kata-nya paling banyak dengan input
        #    (varian yang paling banyak "dijelaskan" oleh teks input).
        # 2. Kalau overlap-nya SERI (mis. customer tulis "Ayam Kalasan
        #    paha atas" tanpa "Nasi" -- itu cocok jadi substring semua
        #    varian "NASI (UDUK) (KOMPLIT)+AYAM KALASAN PAHA ATAS"),
        #    menangkan yang NAMA-nya PALING PENDEK/sederhana -- asumsi
        #    default kalau customer tidak sebut "uduk"/"komplit" dkk,
        #    mereka maksud varian paling dasar, bukan yang paling mahal.
        # ======================================================
        input_words = set(_tokenize(text))
        TIE_MARGIN = 5

        def overlap(menu):
            return len(set(_tokenize(menu["name"])) & input_words)

        near_best = [menu for score, menu in scored if score >= best_score - TIE_MARGIN]
        max_overlap = max(overlap(menu) for menu in near_best)

        if max_overlap == 0:
            # Tidak ada satu pun kandidat yang kata-katanya beneran cocok
            # sama teks input (mis. "katzu" -- typo total, bukan cuma
            # kurang satu kata) -- tie-break berbasis overlap kata tidak
            # relevan di sini, jadi pakai skor fuzzy tertinggi apa adanya.
            best_menu = max(scored, key=lambda pair: pair[0])[1]
            return best_menu, best_score

        tied = [menu for menu in near_best if overlap(menu) == max_overlap]
        best_menu = min(tied, key=lambda menu: len(_tokenize(menu["name"])))

        return best_menu, best_score


if __name__ == "__main__":

    engine = MatchingEngine()

    tests = [
        "katzu",
        "katsu curry",
        "shrimp roll",
        "ayam kalasan dada",
        "uduk ayam dada",
        "beef teriyaki",
        "batagor",
        "perkedel",
    ]

    for t in tests:
        menu, score = engine.search(t)
        print("=" * 50)
        print("INPUT :", t)

        if menu:
            print("MATCH :", menu["name"])
        else:
            print("MATCH : (tidak ditemukan)")

        print("SCORE :", score)
