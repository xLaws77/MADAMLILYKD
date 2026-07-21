import re

from app.normalizer import Normalizer
from app.matching_engine import MatchingEngine, _tokenize
from app.models import OrderItem

try:
    from .business_rules import BusinessRules
    from .config import DEFAULT_SPLIT_COMBO, ENABLE_DEBUG, ENABLE_LOG, FUZZY_MATCH_SCORE
except ImportError:
    from business_rules import BusinessRules
    from config import DEFAULT_SPLIT_COMBO, ENABLE_DEBUG, ENABLE_LOG, FUZZY_MATCH_SCORE

# Angka polos di akhir baris menu di atas batas ini dianggap HARGA, bukan
# qty (harga termurah di menu ada di kisaran ribuan, qty asli tidak
# pernah setinggi itu). Lihat create_item().
QTY_TRAILING_NUMBER_MAX = 1000

# Staf kadang pakai ";" bukan ":" buat pisah nama & menu (mis. "vivi;
# -NASI PUTIH: 3.000R" atau "it ;NASI+SOTO BETAWI: 15.000R") -- kedua
# simbol ini diperlakukan sama sebagai pemisah nama/menu.
CUSTOMER_MARKER_RE = re.compile(r"[:;]")

# Nama kadang ditulis nempel LANGSUNG ke emoji menu tanpa spasi/simbol
# apapun (mis. "al🐟BATAGOR KERING: 12.000R") -- nama pendek (1-4 huruf)
# yang diikuti langsung emoji dianggap customer, sisanya menu.
GLUED_NAME_EMOJI_RE = re.compile(r"^([A-Za-z]{1,4})([\U0001F300-\U0001FAFF☀-➿].*)$")

# Dipakai buat bedain "1 nama menu yang kebetulan pakai +" dari "beberapa
# item beda disambung + oleh staf" -- lihat process_menu().
PRICE_TOKEN_RE = re.compile(r"\d[\d.,]*\s*[Rr]")


class ParserEngine:
    def __init__(self, corrections=None):
        self.normalizer = Normalizer()
        self.matcher = MatchingEngine(corrections=corrections)
        self.menu_keywords = self._load_menu_keywords()
        self.reset()

    # ==========================================================
    # MENU KEYWORDS
    # Diambil otomatis dari nama menu, kategori, dan alias di
    # data/menu.xlsx supaya is_menu_line() selalu sinkron dengan menu
    # yang sebenarnya -- tidak perlu update daftar kata manual setiap
    # kali menu baru ditambahkan di Excel.
    # ==========================================================
    def _load_menu_keywords(self):
        keywords = set()

        for menu in self.matcher.menus:
            sources = [menu.get("name", ""), menu.get("category", "")]
            sources.extend(menu.get("aliases", []))

            for source in sources:
                for word in re.findall(r"[A-Za-z]+", source):
                    if len(word) >= 3:
                        keywords.add(word.upper())

        return keywords

    # ==========================================================
    # RESET
    # ==========================================================
    def reset(self):
        # Customer aktif (hanya jika nama berada di atas menu
        # atau customer inline)
        self.current_customer = ""

        # Menu yang belum memiliki customer
        self.pending_items = []

        # Hasil parser
        self.items = []

        # Warning parser
        self.warnings = []

        # Menu yang tidak dikenali
        self.unknown = []

        # Line yang formatnya belum dikenali
        self.unknown_lines = []

        # Log parser
        self.logs = []

        # Statistik parser
        self.stats = {
            "total_line": 0,
            "menu_found": 0,
            "customer_found": 0,
            "pending_item": 0,
            "unknown_line": 0,
            "unknown_menu": 0,
        }

        # Menjaga urutan customer pertama kali muncul
        self.customer_order = []

    # ==========================================================
    # LOG
    # ==========================================================
    def log(self, level, message):
        if not ENABLE_LOG:
            return

        entry = f"{level}: {message}"
        self.logs.append(entry)

        if ENABLE_DEBUG:
            print(entry)

    def print_logs(self):
        print("\n".join(self.logs))

    def debug(self, message):
        if ENABLE_DEBUG:
            line_number = getattr(self, "current_line_number", None)

            if line_number is None:
                print(message)
            else:
                print(f"LINE {line_number}: {message}")

    # ==========================================================
    # NORMALIZE CUSTOMER NAME
    #
    # Supaya "Asa", "ASA", "asa" dianggap customer YANG SAMA (item-nya
    # digabung jadi satu invoice), bukan tiga customer berbeda cuma
    # karena beda huruf besar/kecil.
    # ==========================================================
    def _normalize_customer_name(self, customer):
        if not customer:
            return ""
        return customer.strip().upper()

    # ==========================================================
    # REGISTER CUSTOMER
    # ==========================================================
    def register_customer(self, customer):
        if not customer:
            return

        customer = customer.strip()

        if customer not in self.customer_order:
            self.customer_order.append(customer)
            self.stats["customer_found"] += 1
            self.log("INFO", f"Customer ditemukan: {customer}")

    # ==========================================================
    # LOOKS LIKE NAME
    #
    # Dipakai untuk membedakan isi kurung/marker yang KEMUNGKINAN nama
    # customer (pendek, tanpa angka, bukan kosakata menu) dari yang
    # kemungkinan NOTE pesanan (lebih panjang/deskriptif), mis.:
    #   "(dante)"                          -> nama (1 kata)
    #   "(sambal banyak jeruk nipis)"       -> note (4 kata)
    #   "(EGG ROLL 3PCS+SHRIMP ROLL 2PCS)"  -> note (ada angka)
    # ==========================================================
    NOTE_PREFIXES = ("NO ", "TANPA ", "TAK ", "GA ", "GAK ", "JANGAN ", "EXTRA ", "PAKAI ")

    def _looks_like_name(self, value):
        if not value:
            return False

        if re.search(r"\d", value):
            return False

        if len(value.split()) > 2:
            return False

        if self.is_menu_line(value):
            return False

        # Kata negasi/permintaan umum ("no kacang", "tanpa sambal",
        # "extra pedas") menandakan ini catatan pesanan, bukan nama
        # customer, walau pendek dan tanpa angka.
        upper = value.strip().upper() + " "

        if upper.startswith(self.NOTE_PREFIXES):
            return False

        return True

    # ==========================================================
    # DETECT INLINE CUSTOMER
    # ==========================================================
    def detect_inline_customer(self, text):
        # Kurung (satu atau dua), mis. "NASI KALASAN (dante)",
        # "(LUKMAN) (PYMNT SCAN ABA)", atau "BUBUR AYAM (no kacang)
        # (agit)" -- coba tiap kurung SATU PER SATU dari kiri ke kanan,
        # pakai yang PERTAMA "kelihatan seperti nama" (lihat
        # _looks_like_name). Ini menangani baik urutan "(nama) (note)"
        # maupun "(note) (nama)".
        paren_groups = re.findall(r"\((.*?)\)", text)

        for group in paren_groups:
            value = group.strip()

            if self._looks_like_name(value):
                return value

        patterns = [
            r"\[(.*?)\]",
            r">\s*(.+)$",
            r"=\s*(.+)$",
            r"-\s*([A-Za-z0-9 _]+)$",
            # Nama nempel di belakang harga TANPA simbol pemisah apapun,
            # cuma spasi, mis. "...12.000R tommy" atau "...14.000R rt" --
            # harus muncul PALING TERAKHIR karena paling umum/longgar,
            # supaya pola yang lebih spesifik di atas dicoba duluan.
            r"\d[\d.,]*\s*[Rr]?\s+([A-Za-z][A-Za-z0-9 _]*)$",
        ]

        for pattern in patterns:
            m = re.search(pattern, text)

            if not m:
                continue

            value = m.group(1).strip()

            if self._looks_like_name(value):
                return value

        return None

    # ==========================================================
    # DETECT NOTE
    # Contoh:
    # (LUKMAN) (PYMNT SCAN ABA)          -> customer=LUKMAN, note=PYMNT SCAN ABA
    # (no kacang) (agit)                 -> customer=agit, note=no kacang
    # NASI KALASAN (garing)              -> note=garing (kurung tunggal)
    # NASI KALASAN (dante)               -> customer=dante, TIDAK ada note
    # ==========================================================
    def detect_note(self, text):
        data = [g.strip() for g in re.findall(r"\((.*?)\)", text)]

        if not data:
            return ""

        # Kurung yang dipakai sebagai nama customer (kalau ada) BUKAN
        # note -- ambil kurung PERTAMA lain yang tersisa sebagai note,
        # apapun urutannya ("(nama) (note)" atau "(note) (nama)").
        customer_value = next((g for g in data if self._looks_like_name(g)), None)
        remaining = [g for g in data if g != customer_value] if customer_value else data
        candidate = remaining[0] if remaining else ""

        # Kurung isi angka murni, mis. "Nasi Campur ( 17000 ) = 1", itu
        # harga yang ditulis manual oleh customer -- bukan note. Harga
        # asli tetap dari data menu, jadi angka ini dibuang supaya tidak
        # tampil dobel di struk/invoice.
        if candidate and not re.search(r"[A-Za-z]", candidate):
            return ""

        # Kurung isi harga dengan suffix R/Riel, mis. "(10.000R)",
        # "(12,000 Riel)" -- huruf di sini cuma kode mata uang, bukan
        # catatan pesanan. Tanpa filter ini harga lolos cek huruf di
        # atas dan tampil sebagai note di invoice.
        if candidate and re.match(
            r"^\s*[\d.,]+\s*[Rr](iel)?\s*$", candidate
        ):
            return ""

        return candidate

    # ==========================================================
    # DETECT QTY
    # ==========================================================
    def detect_qty(self, text):
        patterns = [
            r"\((\d+)\)",
            r"[xX]\s*(\d+)",
            r"@\s*(\d+)",
            r"#\s*(\d+)",
            r"=\s*(\d+)",
            r":\s*(\d+)\s*$",
        ]

        for pattern in patterns:
            m = re.search(pattern, text)

            if m:
                return int(m.group(1))

        return 1

    # ==========================================================
    # HARGA / QTY ONLY
    # Contoh: "12.000R = 1", "14.000R : 1" -- ini bukan nama
    # customer, cuma keterangan harga & qty yang menempel di baris
    # menu (format "MENU: HARGA = QTY").
    # ==========================================================
    def is_price_or_qty_text(self, text):
        t = text.strip()

        if not t:
            return False

        return bool(re.fullmatch(r"[Rr\d.,\s:=xX@#()]+", t))

    # ==========================================================
    # SPLIT ON CUSTOMER MARKER
    # ":" atau ";" -- staf kadang pakai titik koma. Split di kemunculan
    # PERTAMA dari salah satu simbol itu (mana pun yang lebih dulu).
    # ==========================================================
    def _split_on_customer_marker(self, line):
        m = CUSTOMER_MARKER_RE.search(line)

        if not m:
            return None

        return line[: m.start()], line[m.start() + 1 :]

    # ==========================================================
    # SPLIT GLUED NAME + MENU
    # Nama nempel langsung ke emoji tanpa spasi/simbol apapun, mis.
    # "al🐟BATAGOR KERING: 12.000R" -- return (nama, sisa_menu) kalau
    # cocok, None kalau tidak.
    # ==========================================================
    def _split_glued_name_menu(self, line):
        m = GLUED_NAME_EMOJI_RE.match(line.strip())

        if not m:
            return None

        name, rest = m.group(1), m.group(2)

        if not self._looks_like_name(name):
            return None

        if not self.is_menu_line(rest):
            return None

        return name, rest

    # ==========================================================
    # CUSTOMER LINE
    # ==========================================================
    def is_customer_line(self, text):
        t = text.strip()

        if not t:
            return False

        upper = t.upper()

        if upper.startswith("NAME"):
            return True

        if self.is_price_or_qty_text(t):
            return False

        if self.is_menu_line(t):
            return False

        if CUSTOMER_MARKER_RE.search(t):
            left = self._split_on_customer_marker(t)[0].strip()

            reserved = [
                "LOCATION",
                "LOKASI",
                "ADDRESS",
                "ALAMAT",
                "DELIVERY",
                "TEMPAT",
                "NOTE",
            ]

            if (left or "").upper() in reserved:
                return False

            if self.is_menu_line(left):
                return False

            return True

        return True

    # ==========================================================
    # MENU LINE
    # ==========================================================
    def is_menu_line(self, text):
        if not text:
            return False
        upper = text.upper()

        keywords = [
            "NASI",
            "AYAM",
            "KATSU",
            "RICE",
            "CURRY",
            "TERIYAKI",
            "BATAGOR",
            "HOKI",
            "TEMPE",
            "TAHU",
            "TELUR",
            "EMPING",
            "ES ",
        ]

        if any(word in upper for word in keywords):
            return True

        words_in_text = set(re.findall(r"[A-Za-z]+", upper))
        return bool(words_in_text & self.menu_keywords)

    # ==========================================================
    # PARSE COLON LINE
    #
    # Mendukung:
    #
    # Kevin: Chicken Katsu
    # Chicken Katsu: Kevin
    #
    # Return:
    # (customer, menu)
    # Jika bukan format yang dikenali:
    # (None, None)
    # ==========================================================
    def parse_colon_line(self, line):
        split_result = self._split_on_customer_marker(line)

        if split_result is None:
            return None, None

        left, right = split_result

        left = left.strip()
        right = right.strip()

        # CUSTOMER : MENU
        if self.is_customer_line(left) and self.is_menu_line(right):
            return left, right

        # MENU : CUSTOMER
        if self.is_menu_line(left) and self.is_customer_line(right):
            # Jangan langsung anggap SELURUH sisi kanan sebagai nama
            # customer -- bisa jadi ini harga+customer yang nempel
            # tanpa pemisah jelas, mis. "MENU: 12.000R (dante)" atau
            # "MENU: 10.000R -kijek". Kalau begitu, biarkan
            # process_menu() yang urus lewat detect_inline_customer()
            # (yang sudah bisa memisahkan harga dari nama customer di
            # akhir baris dengan benar).
            if self._has_price_with_inline_customer(right):
                return None, None

            return right, left

        return None, None

    # ==========================================================
    # HAS PRICE WITH INLINE CUSTOMER
    #
    # Contoh: "12.000R (dante)" atau "10.000R -kijek" -- harga di depan,
    # nama customer nempel di belakang tanpa pemisah yang jelas. Return
    # True kalau sisa teks SETELAH marker customer dibuang cuma harga/
    # qty (atau kosong), supaya parse_colon_line() tahu ini bukan
    # "customer line" murni.
    # ==========================================================
    def _has_price_with_inline_customer(self, text):
        patterns = [
            r"^(?P<rest>.*)\((?P<name>[^()]+)\)\s*$",
            r"^(?P<rest>.*)\[(?P<name>[^\[\]]+)\]\s*$",
            r"^(?P<rest>.*)-\s*(?P<name>[A-Za-z][A-Za-z0-9 _]*)\s*$",
            r"^(?P<rest>.*)>\s*(?P<name>[A-Za-z][A-Za-z0-9 _]*)\s*$",
            r"^(?P<rest>.*)=\s*(?P<name>[A-Za-z][A-Za-z0-9 _]*)\s*$",
            # Nama nempel di belakang harga tanpa simbol apapun, cuma
            # spasi (mis. "12.000R tommy") -- generik & dicek paling
            # akhir, tapi tetap aman karena "rest" tetap harus lolos
            # is_price_or_qty_text di bawah.
            r"^(?P<rest>.*?)\s+(?P<name>[A-Za-z][A-Za-z0-9 _]*)\s*$",
        ]

        for pattern in patterns:
            m = re.match(pattern, text)

            if not m:
                continue

            name = m.group("name").strip()
            rest = m.group("rest").strip()

            if self._looks_like_name(name) and (not rest or self.is_price_or_qty_text(rest)):
                return True

        return False

    # ==========================================================
    # BUSINESS RULES
    # ==========================================================
    def apply_business_rules(self, text):
        return BusinessRules.apply(text, parser=self)

    # ==========================================================
    # SPLIT COMBO
    # ==========================================================
    def split_combo(self, text):
        text = text.strip()

        separator = DEFAULT_SPLIT_COMBO

        if not re.search(separator, text):
            return [text]

        parts = [
            x.strip()
            for x in re.split(separator, text)
            if x.strip()
        ]

        return parts

    # ==========================================================
    # SPLIT MENU ITEMS
    # Logika yang lebih cerdas untuk memisahkan item-item.
    # Ada 2 macam pemisah: "-" (item baru dengan dash prefix) dan "+"
    # (bisa bagian nama menu atau pemisah item).
    #
    # Strategi:
    # 1. Digit sebelum "+" → "+" adalah pemisah item (bukan nama combo)
    #    mis. "BUBUR AYAM 1 + TELUR ASIN 2" → 2 item terpisah
    #    vs   "BUBUR AYAM + TELUR ASIN 1"   → 1 item combo (no digit before +)
    # 2. Lebih dari 1 price token (dengan suffix R) → coba split per harga
    # ==========================================================
    def _split_by_qty_plus(self, raw):
        """Split 'ITEM QTY + ITEM QTY' format.

        Digit sebelum '+' menandakan '+' adalah pemisah item, bukan
        penghubung nama combo. Tanpa digit sebelum '+', raw dikembalikan
        utuh (kemungkinan nama combo seperti 'BUBUR AYAM + TELUR ASIN').
        """
        if not re.search(r'\d\s*\+', raw):
            return [raw]
        parts = re.split(r'(?<=\d)\s*\+\s*', raw)
        parts = [p.strip() for p in parts if p.strip()]
        return parts if len(parts) > 1 else [raw]

    def _split_menu_items(self, raw):
        raw = raw.strip()

        # Strategi 1: format 'ITEM QTY + ITEM QTY' tanpa suffix R
        # mis. "BUBUR AYAM 1 + TELUR ASIN 2 +SATE USUS 1"
        qty_split = self._split_by_qty_plus(raw)
        if len(qty_split) > 1:
            return qty_split

        # Strategi 2: cek price token berformat angkaR (mis. 12.000R)
        price_count = len(PRICE_TOKEN_RE.findall(raw))
        if price_count <= 1:
            return [raw]

        # Pertama, coba split by "-" yang memisahkan item baru
        # Pola: setelah harga ada " -menu" (spasi-dash-menu)
        # atau di awal "-menu" (dash di awal item)
        dash_items = self._split_by_dash_separator(raw)
        if len(dash_items) > 1:
            # Ada multiple items terpisah oleh "-"
            # Proses setiap item untuk internal "+" split
            result = []
            for item in dash_items:
                result.extend(self._split_by_plus_in_item(item))
            return result

        # Kalau tidak ada "-" separator, coba split pada "+"
        potential_split = self.split_combo(raw)
        if len(potential_split) <= 1:
            return [raw]

        # Cek apakah SETIAP segment punya price token sendiri
        segments_with_prices = [
            len(PRICE_TOKEN_RE.findall(segment)) > 0
            for segment in potential_split
        ]

        # Kalau semua segment punya price, split aman
        if all(segments_with_prices):
            return potential_split

        return [raw]

    def _split_by_dash_separator(self, raw):
        """Pisahkan item-item yang ditandai dash, mis.
        'NASI: 10k -TEMPE: 2k' -> ['NASI: 10k', '-TEMPE: 2k']
        Hanya pisahkan jika pattern clear: " -MENU" setelah price atau
        di awal baris."""
        # Pattern: spasi + dash + menu, atau di awal baris
        # Contoh matches: " -TEMPE BACEM", di awal "-NASI"
        parts = re.split(r'(?=\s-[A-Z]|-[A-Z])', raw)
        parts = [p.strip() for p in parts if p.strip()]
        return parts if len(parts) > 1 else [raw]

    def _split_by_plus_in_item(self, item):
        """Untuk item tunggal, cek apakah "+" dalam item perlu di-split.
        Hanya split jika setiap segment punya price sendiri."""
        item = item.strip()
        potential_split = self.split_combo(item)

        if len(potential_split) <= 1:
            return [item]

        segments_with_prices = [
            len(PRICE_TOKEN_RE.findall(segment)) > 0
            for segment in potential_split
        ]

        if all(segments_with_prices):
            return potential_split

        return [item]

    # ==========================================================
    # CREATE ORDER ITEM
    # ==========================================================
    def create_item(self, menu_text, qty, customer, raw):
        note = self.detect_note(raw)

        menu_text = self.apply_business_rules(menu_text)

        # Qty kadang ditulis sebagai angka polos di akhir baris tanpa
        # simbol apapun, mis. "Nasi soto betawi (sambal banyak jeruk
        # nipis) 4" -- detect_qty() tidak menangkap ini (perlu simbol
        # seperti x/@/#/=/:). Kalau qty masih default 1 dan kata
        # TERAKHIR di menu_text angka murni, coba lepas angka itu dan
        # cek apakah sisa teksnya MASIH cocok ke sebuah menu -- kalau
        # masih cocok, angka itu qty, bukan bagian nama menu (beda
        # dengan "HOKI 10" di mana "10" memang bagian dari nama menu).
        if qty == 1:
            words = menu_text.split()

            if len(words) > 1 and words[-1].isdigit():
                trailing_number = int(words[-1])

                # Angka besar (>= harga termurah di menu, ~1000) hampir
                # pasti HARGA yang ditulis customer nempel di nama menu
                # (mis. "Nasi babi panggang 23000"), bukan qty -- qty
                # asli jarang setinggi itu. Tanpa batas ini, "23000"
                # kepakai jadi qty lalu dikalikan lagi dengan harga menu
                # sehingga totalnya meledak (23000 x Rp23.000).
                if trailing_number < QTY_TRAILING_NUMBER_MAX:
                    remainder = " ".join(words[:-1])
                    remainder_words = _tokenize(remainder)

                    if self.matcher.find_prefix_match(remainder_words) is not None:
                        qty = trailing_number
                        menu_text = remainder

        menu, score = self.matcher.search(menu_text)

        if menu is None:
            self.unknown.append(raw)
            self.stats["unknown_menu"] += 1
            self.log("WARNING", f"Unknown menu: {raw}")
            return None

        if score < FUZZY_MATCH_SCORE:
            self.warnings.append(
                f"Fuzzy Match ({score}%) : {raw}"
            )
            self.log("WARNING", f"Fuzzy Match ({score}%) : {raw}")

        # Buang note yang cuma mengulang teks yang sudah ada di nama
        # menu itu sendiri (mis. "SHRIMP ROLL (4PCS) ALA CARTE" + note
        # "4PCS") atau di catatan menu (mis. HOKI 7 yang catatannya
        # sendiri sudah "Egg Roll 3pcs + Shrimp Roll 2pcs") -- supaya
        # tidak tampil dobel di struk/invoice.
        if note and self._is_redundant_note(note, menu):
            note = ""

        item = OrderItem(
            customer=customer or "",
            menu=menu["name"] or "",
            qty=qty,
            price=menu["price"],
            category=menu["category"],
            raw=raw,
            note=note,
            resto=menu.get("resto", ""),
            emoji=menu.get("emoji", ""),
            # Teks yang benar-benar dicocokkan ke katalog -- dipakai
            # fitur "belajar dari koreksi" (lihat models.OrderItem).
            search_text=menu_text,
        )

        return item

    # ==========================================================
    # IS REDUNDANT NOTE
    # ==========================================================
    def _is_redundant_note(self, note, menu):
        normalized_note = re.sub(r"[^A-Z0-9]", "", (note or "").upper())

        if not normalized_note:
            return True

        normalized_name = re.sub(r"[^A-Z0-9]", "", (menu.get("name") or "").upper())

        if normalized_note in normalized_name:
            return True

        normalized_catatan = re.sub(r"[^A-Z0-9]", "", (menu.get("note") or "").upper())

        if normalized_catatan and normalized_note == normalized_catatan:
            return True

        return False

    # ==========================================================
    # ADD ITEM
    # ==========================================================
    def add_item(self, item):
        if item is None:
            return

        if item.customer:
            self.register_customer(item.customer)
            self.items.append(item)
        else:
            self.pending_items.append(item)
            self.stats["pending_item"] += 1
            self.log("INFO", f"Pending customer: {item.raw}")
            self.debug("Pending Customer")

    # ==========================================================
    # FLUSH PENDING
    # Customer yang muncul di bawah menu
    # HANYA menyelesaikan pending item
    # Tidak mengubah current_customer
    # ==========================================================
    def flush_pending(self, customer):
        if not customer:
            return

        customer = self._normalize_customer_name(customer)

        self.register_customer(customer)

        for item in self.pending_items:
            item.customer = customer
            self.items.append(item)

        self.pending_items.clear()

    # ==========================================================
    # PROCESS CUSTOMER
    # ==========================================================
    def process_customer(self, line):
        if not line:
            return
        customer = line.strip()

        if customer.upper().startswith("NAME"):
            customer = customer.split(":", 1)[-1].strip()
        elif customer.endswith(":"):
            customer = customer[:-1].strip()

        # Baris nama berdiri sendiri kadang ditulis pakai bullet/dash di
        # depan (mis. "-ercan"), bukan sebagai penanda inline seperti
        # "MENU: harga -nama" -- buang supaya "ercan" dan "-ercan" tetap
        # dianggap customer yang sama.
        customer = re.sub(r"^[-•*>]\s*", "", customer).strip()

        customer = self._normalize_customer_name(customer)

        # Jika ada pending,
        # customer ini hanya dipakai untuk pending.
        if self.pending_items:
            self.log("INFO", f"Pending customer diselesaikan: {customer}")
            self.debug("Pending Customer")
            self.flush_pending(customer)
            return

        # Customer di atas menu menjadi customer aktif
        self.current_customer = customer
        self.register_customer(customer)
        self.debug("Customer Detected")

    # ==========================================================
    # PROCESS MENU
    # ==========================================================
    def process_menu(self, raw):
        qty = self.detect_qty(raw)

        inline_customer = self.detect_inline_customer(raw)

        # "+" itu ambigu: banyak nama menu resmi MEMANG pakai "+" (mis.
        # "NASI+AYAM KALASAN DADA" -- SATU item), tapi staf kadang juga
        # pakai "+" buat menyambung BEBERAPA item berbeda dalam 1 baris,
        # masing-masing dengan harga sendiri (mis. "-NASI PUTIH: 3.000R
        # +-TEMPE BACEM: 2.000R"). Bedanya: baris genuine combo staf
        # selalu punya harga LEBIH DARI SATU KALI. Cuma displit per "+"
        # kalau ada >1 harga -- kalau cuma 1 harga, itu SATU nama menu
        # yang kebetulan mengandung "+", jangan dipecah.
        #
        # Split (kalau perlu) dilakukan SEBELUM dibersihkan, karena
        # normalizer.clean() membuang karakter "+" jadi spasi -- kalau
        # dibalik urutannya, split_combo() tidak akan nemu "+" apa pun
        # lagi buat dipisah.
        #
        # PERBAIKAN: split hanya terjadi jika SETIAP segment hasil split
        # punya price token sendiri (bukan hanya total price count > 1).
        # Ini menangani kasus: "NASI+AYAM: 18.000R -TEMPE: 2.000R (nama)"
        # dimana "+" ada dalam satu item (18k) dan "-" memisahkan item lain (2k).
        raw_menus = self._split_menu_items(raw)

        menus = [self.normalizer.clean(m) for m in raw_menus]

        # Customer ditulis nempel di baris menu ini SAJA -- cuma berlaku
        # untuk item-item di baris ini, current_customer dikembalikan
        # lagi sesudahnya. Kalau tidak di-revert, baris menu BERIKUTNYA
        # yang tidak sebut nama ikut kepakai nama ini (bukan balik jadi
        # "pending" menanti nama berdiri sendiri di bawahnya), sehingga
        # customer baru yang menyusul (mis. "rt" setelah item lain yang
        # nempel "tommy") malah tidak pernah kepakai.
        previous_customer = self.current_customer

        if inline_customer:
            inline_customer = self._normalize_customer_name(inline_customer)
            self.current_customer = inline_customer
            self.register_customer(inline_customer)
            self.debug("Customer Detected")

        customer = self.current_customer

        for menu in menus:
            self.stats["menu_found"] += 1
            self.log("INFO", f"Menu ditemukan: {menu}")
            self.debug("Menu Detected")

            item = self.create_item(
                menu_text=menu,
                qty=qty,
                customer=customer,
                raw=raw,
            )

            self.add_item(item)

        if inline_customer:
            self.current_customer = previous_customer

            if item is not None:
                self.debug("Create Item")

    # ==========================================================
    # PROCESS LINE
    # ==========================================================
    def process_line(self, line):
        self.stats["total_line"] += 1
        line = line.strip()

        if line == "":
            return

        glued = self._split_glued_name_menu(line)

        if glued:
            name, menu_part = glued
            name = self._normalize_customer_name(name)
            self.current_customer = name
            self.register_customer(name)
            self.debug("Customer Detected (nempel tanpa pemisah)")
            self.process_menu(menu_part)
            return

        if CUSTOMER_MARKER_RE.search(line):
            customer, menu = self.parse_colon_line(line)

            if customer and menu:
                customer = self._normalize_customer_name(customer)
                self.current_customer = customer
                self.register_customer(customer)
                self.debug("Customer Detected")
                self.process_menu(menu)
                return

        if self.is_customer_line(line):
            self.process_customer(line)
            return

        if self.is_menu_line(line):
            self.process_menu(line)
            return

        self.unknown_lines.append(line)
        self.stats["unknown_line"] += 1
        self.log("WARNING", f"Unknown format: {line}")

    # ==========================================================
    # PARSE
    # ==========================================================
    def parse(self, message):
        self.reset()

        for line_number, line in enumerate(message.splitlines(), start=1):
            self.current_line_number = line_number
            self.process_line(line)

        # Pending item yang tidak pernah mendapat customer
        if self.pending_items:
            self.warnings.append(
                f"{len(self.pending_items)} item tidak memiliki customer"
            )
            self.log("WARNING", f"{len(self.pending_items)} item tidak memiliki customer")

            for item in self.pending_items:
                item.customer = "UNKNOWN"
                self.items.append(item)

            self.pending_items.clear()

        return self.items

    # ==========================================================
    # GROUP BY CUSTOMER
    # ==========================================================
    def group_by_customer(self):
        result = {}

        for customer in self.customer_order:
            key = customer or "UNKNOWN"
            result[key] = []

        if "UNKNOWN" not in result:
            result["UNKNOWN"] = []

        for item in self.items:
            key = item.customer or "UNKNOWN"
            if key not in result:
                result[key] = []

            result[key].append(item)

        if len(result["UNKNOWN"]) == 0:
            del result["UNKNOWN"]

        return result

    # ==========================================================
    # BUILD PREVIEW
    # ==========================================================
    def build_preview(self):
        groups = self.group_by_customer()

        lines = []

        total_item = 0

        total_customer = 0

        lines.append("=" * 45)

        for customer, items in groups.items():
            total_customer += 1

            lines.append("")
            lines.append((customer or "UNKNOWN").upper())
            lines.append("-" * 30)

            for item in items:
                total_item += item.qty

                lines.append(
                    f"- {item.menu} x{item.qty}"
                )

        lines.append("")
        lines.append("=" * 45)

        lines.append(f"Customer : {total_customer}")
        lines.append(f"Item     : {total_item}")
        lines.append(f"Warning  : {len(self.warnings)}")
        lines.append(f"Unknown  : {len(self.unknown)}")

        if self.warnings:
            lines.append("")
            lines.append("WARNING")

            for w in self.warnings:
                lines.append(f"- {w}")

        if self.unknown:
            lines.append("")
            lines.append("UNKNOWN")

            for u in self.unknown:
                lines.append(f"- {u}")

        return "\n".join(lines)

    # ==========================================================
    # SUMMARY
    # ==========================================================
    def summary(self):
        return {
            "customer": len(self.group_by_customer()),
            "item": sum(i.qty for i in self.items),
            "warning": len(self.warnings),
            "unknown": len(self.unknown),
            "total_line": self.stats["total_line"],
            "menu_found": self.stats["menu_found"],
            "customer_found": self.stats["customer_found"],
            "pending_item": self.stats["pending_item"],
            "unknown_line": self.stats["unknown_line"],
            "unknown_menu": self.stats["unknown_menu"],
            "warnings": len(self.warnings),
        }


# ==========================================================
# TEST
# ==========================================================
if __name__ == "__main__":
    parser = ParserEngine()

    sample = """
Kevin

CHICKEN KATSU+RICE

ES TEH

NASI+AYAM KALASAN

Alex

NASI+AYAM KREMES DADA

-TEMPE BACEM

KATSU CURRY RICE (Lux)
"""

    parser.parse(sample)

    print(parser.build_preview())

    print()

    print(parser.summary())
