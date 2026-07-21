"""
telegram_adapter.py

PERUBAHAN dari versi sebelumnya:
1. BillGenerator SEKARANG benar-benar dipakai -- reply teks bill sudah
   menampilkan harga per item + subtotal per customer + grand total
   (sebelumnya cuma daftar nama menu & qty, tanpa harga sama sekali).
2. Menyimpan hasil parsing terakhir per chat_id (self.last_orders) supaya
   command /struk dan /invoice di bot.py bisa generate struk/PDF tanpa
   customer perlu kirim ulang teks order-nya.
3. print() debug diganti pakai app.logger (info/warning/error) supaya
   log-nya konsisten dan bisa diarahkan ke file/monitoring, bukan cuma
   nongol di terminal.
"""

import os
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from .receipt_data import ReceiptBuilder, OrderSummary
    from .logger import info, warning, error
    from .constants import HOME_RESTO
    from .config import USD_RATE, ENABLE_INTELLIGENCE, INTELLIGENCE_CONFIDENCE
    from .order_store import OrderStore
    from .ai_parser import AIParser
    from .models import Invoice, OrderItem
    from .bill import BillGenerator
    from .matching_engine import correction_key, _tokenize
    from .intelligence import IntelligenceEngine
except ImportError:
    from receipt_data import ReceiptBuilder, OrderSummary
    from constants import HOME_RESTO
    from config import USD_RATE, ENABLE_INTELLIGENCE, INTELLIGENCE_CONFIDENCE
    from order_store import OrderStore
    from ai_parser import AIParser
    from models import Invoice, OrderItem
    from bill import BillGenerator
    from matching_engine import correction_key, _tokenize
    from intelligence import IntelligenceEngine
    try:
        from app.logger import info, warning, error
    except ImportError:
        # fallback kalau app.logger belum ada / dipanggil di luar konteks bot
        def info(msg): print(f"INFO: {msg}")
        def warning(msg): print(f"WARNING: {msg}")
        def error(msg): print(f"ERROR: {msg}")


# Regex pendeteksi prefix pre-order. Contoh valid:
#   "BESOK: NASI GORENG..."
#   "BESOK\nNASI GORENG..."
#   "BESOK - NASI GORENG..."
_PREORDER_PREFIX_RE = re.compile(
    r"^BESOK\s*[:\-]?\s*", re.IGNORECASE
)

BOT_VERSION = "1.1.0"
PARSER_VERSION = "1.0.0"
BUSINESS_RULE_VERSION = "1.0.0"

# Ambang "order penting" untuk cross-check AI: order dengan customer
# banyak / item banyak / nominal besar tetap dibaca ulang oleh AI walau
# parser regex terlihat yakin (order besar paling mahal kalau salah).
# Bisa disetel lewat env var kalau perlu.
CROSSCHECK_MIN_CUSTOMERS = int(os.getenv("AI_CROSSCHECK_MIN_CUSTOMERS", "4"))
CROSSCHECK_MIN_ITEMS = int(os.getenv("AI_CROSSCHECK_MIN_ITEMS", "6"))
CROSSCHECK_MIN_TOTAL = int(os.getenv("AI_CROSSCHECK_MIN_TOTAL", "60000"))


class OrderBuilder:
    def build(self, parser: Any, items: Iterable[Any]) -> Dict[str, Any]:
        item_list = list(items)

        if hasattr(parser, "group_by_customer"):
            groups = parser.group_by_customer()
        else:
            groups = self._group_by_customer(item_list)

        summary = parser.summary() if hasattr(parser, "summary") else {}

        return {
            "items": item_list,
            "groups": groups,
            "summary": summary,
            "warnings": getattr(parser, "warnings", []),
            "unknown": getattr(parser, "unknown", []),
            "unknown_lines": getattr(parser, "unknown_lines", []),
        }

    def _group_by_customer(self, items: Iterable[Any]) -> Dict[str, List[Any]]:
        groups: Dict[str, List[Any]] = {}

        for item in items:
            customer = getattr(item, "customer", "") or "UNKNOWN"

            if customer not in groups:
                groups[customer] = []

            groups[customer].append(item)

        return groups


class ReplyFormatter:
    separator = "-" * 24

    def format(self, order: Dict[str, Any], summary: Optional[OrderSummary] = None) -> str:
        if summary is not None and summary.invoices:
            return self._format_with_price(summary, order)

        return self._format_without_price(order)

    # ------------------------------------------------------------
    # Blok siap-copy-paste untuk item yang bukan resto tuan rumah
    # (mis. pesanan Madam Lily yang nyelip di order Madam Lily)
    # ------------------------------------------------------------
    def format_foreign_resto_block(
        self, order: Dict[str, Any], home_resto: str = HOME_RESTO
    ) -> str:
        home_resto_upper = home_resto.strip().upper()
        foreign: Dict[str, Dict[str, List[Any]]] = {}

        for customer, items in order["groups"].items():
            for item in items:
                resto = (getattr(item, "resto", "") or "").strip().upper()

                if not resto or resto == home_resto_upper:
                    continue

                foreign.setdefault(resto, {}).setdefault(customer, []).append(item)

        if not foreign:
            return ""

        blocks: List[str] = []

        for resto, customers in foreign.items():
            lines = [f"📤 UNTUK {resto} (copy-paste ke bot {resto}):", self.separator]

            for customer, items in customers.items():
                lines.append(str(customer or "").upper())

                for item in items:
                    lines.append(
                        f"- {item.menu}: {item.price:,.0f}R = {item.qty}"
                    )

                lines.append("")

            lines.append(self.separator)
            blocks.append("\n".join(lines).strip())

        return "\n\n".join(blocks)

    # ------------------------------------------------------------
    # Format lama (fallback, kalau BillGenerator gagal / tidak ada harga)
    # ------------------------------------------------------------
    def _format_without_price(self, order: Dict[str, Any]) -> str:
        lines: List[str] = []
        total_customer = 0
        total_item = 0

        for customer, items in order["groups"].items():
            total_customer += 1
            lines.append(str(customer or "").upper())
            lines.append("")

            for item in items:
                qty = getattr(item, "qty", 1)
                total_item += qty
                lines.append(f"- {getattr(item, 'menu', '')} x{qty}")

            lines.append("")
            lines.append(self.separator)
            lines.append("")

        lines.append(f"TOTAL CUSTOMER : {total_customer}")
        lines.append(f"TOTAL ITEM : {total_item}")

        return "\n".join(lines).strip()

    # ------------------------------------------------------------
    # Format baru dengan harga & total
    # ------------------------------------------------------------
    def _format_with_price(self, summary: OrderSummary, order: Dict[str, Any]) -> str:
        lines: List[str] = []

        for invoice in summary.invoices:
            lines.append((invoice.telegram_name or "").upper())
            lines.append("")

            for item in invoice.items:
                disc = getattr(item, "discount_riel", 0)
                eff_price = max(0, item.price - disc)
                subtotal = eff_price * item.qty
                if disc > 0:
                    lines.append(
                        f"- {item.menu} x{item.qty}  "
                        f"({item.price:,.0f}→{eff_price:,.0f}R, "
                        f"diskon {disc:,.0f}R) = {subtotal:,.0f} Riel"
                    )
                else:
                    lines.append(
                        f"- {item.menu} x{item.qty}  ({subtotal:,.0f} Riel)"
                    )

            lines.append("")
            lines.append(
                f"Subtotal : {invoice.total_riel:,.0f} Riel (${invoice.total_usd:,.2f})"
            )
            lines.append("")
            lines.append(self.separator)
            lines.append("")

        lines.append(f"TOTAL CUSTOMER : {len(summary.invoices)}")
        lines.append(
            f"GRAND TOTAL : {summary.grand_total_riel:,.0f} Riel "
            f"(${summary.grand_total_usd:,.2f})"
        )

        warnings = order.get("warnings") or []
        unknown = order.get("unknown") or []

        if warnings:
            lines.append("")
            lines.append(f"⚠️ {len(warnings)} peringatan (fuzzy match rendah)")

        if unknown:
            lines.append(f"❓ {len(unknown)} menu tidak dikenali")

        return "\n".join(lines).strip()


class TelegramLogger:
    def __init__(self, log_path: str = "logs/telegram.log"):
        self.log_path = Path(log_path)

    def write(self, username: str, chat_id: str, message: str, status: str):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        date = datetime.now().isoformat(timespec="seconds")
        safe_message = message.replace("\r", " ").replace("\n", "\\n")

        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(
                f"{date}\t{username}\t{chat_id}\t{safe_message}\t{status}\n"
            )


class TelegramAdapter:
    def __init__(
        self,
        parser_cls: Optional[Any] = None,
        order_builder: Optional[OrderBuilder] = None,
        formatter: Optional[ReplyFormatter] = None,
        logger: Optional[TelegramLogger] = None,
        receipt_builder: Optional[ReceiptBuilder] = None,
        order_store: Optional[OrderStore] = None,
    ):
        self.parser_cls = parser_cls or self._load_parser()
        self.order_builder = order_builder or OrderBuilder()
        self.formatter = formatter or ReplyFormatter()
        self.logger = logger or TelegramLogger()
        self.receipt_builder = receipt_builder or ReceiptBuilder()

        # Penyimpanan permanen (SQLite) -- order tetap bisa diakses
        # (/struk, /tambah, dst.) walau bot restart. Kalau gagal dibuka
        # (mis. disk read-only), bot tetap jalan tanpa persistence.
        try:
            self.store: Optional[OrderStore] = order_store or OrderStore()
        except Exception as store_error:
            warning(f"OrderStore tidak tersedia: {store_error}")
            self.store = None

        # Koreksi hasil belajar dari staf ({pattern -> nama menu}).
        # Dimuat dari DB saat start, dipakai tiap kali membuat parser
        # supaya tulisan yang pernah dikoreksi tidak salah baca lagi.
        self.corrections: Dict[str, str] = {}

        if self.store is not None:
            try:
                self.corrections = self.store.load_corrections()
            except Exception as corr_error:
                warning(f"Gagal memuat koreksi: {corr_error}")

        # Lapisan AI (Groq atau Gemini, free tier) untuk order yang
        # gagal dibaca parser regex. Nonaktif kalau GROQ_API_KEY dan
        # GEMINI_API_KEY dua-duanya kosong.
        try:
            self.ai_parser: Optional[AIParser] = AIParser(
                self._new_parser().matcher.menus
            )
        except Exception as ai_error:
            warning(f"AIParser tidak tersedia: {ai_error}")
            self.ai_parser = None

        # Cache order terakhir per chat, dipakai command /struk & /invoice
        self.last_orders: Dict[str, OrderSummary] = {}

        # ID baris DB untuk order terakhir per chat (buat update saat
        # /tambah //hapus //ganti / balasan lokasi)
        self.last_order_ids: Dict[str, int] = {}

        # Versi-AI alternatif untuk order terakhir per chat: kalau AI
        # membaca order penting BERBEDA dari parser regex, hasil AI
        # disimpan di sini supaya staf bisa /pakaiai untuk menggantinya.
        self.pending_ai_versions: Dict[str, OrderSummary] = {}

        # Teks mentah order terakhir per chat -- disimpan supaya tombol
        # "Cek AI" bisa membaca ulang order dengan AI kapan saja, tanpa
        # perlu customer kirim ulang teks order-nya.
        self.last_order_texts: Dict[str, str] = {}

        # Blok "untuk resto lain" (mis. Madam Lily) dari order terakhir
        # per chat, dipakai bot.py untuk auto-forward ke chat resto itu
        self.last_foreign_blocks: Dict[str, str] = {}

        # Smart Order Intelligence: lapisan pemahaman bahasa natural
        # SEBELUM ParserEngine. Fail-open -- kalau gagal dibuat atau
        # gagal menganalisa, teks asli diproses jalur lama seperti biasa.
        self.intelligence: Optional[IntelligenceEngine] = None

        if ENABLE_INTELLIGENCE:
            try:
                self.intelligence = IntelligenceEngine(
                    parser_provider=self._new_parser
                )
            except Exception as intel_error:
                warning(f"IntelligenceEngine tidak tersedia: {intel_error}")

    def _new_parser(self):
        """Buat instance parser dengan koreksi hasil belajar terpasang.
        Dipakai di semua tempat yang butuh parsing/matching supaya
        koreksi selalu ikut. Fallback tanpa argumen kalau parser_cls
        yang dipasang (mis. di test) tidak menerima kwarg."""
        try:
            return self.parser_cls(corrections=self.corrections)
        except TypeError:
            return self.parser_cls()

    def reload_menus(self):
        """Reload data menu setelah admin mengubah menu.xlsx.
        AI parser di-rebuild supaya katalog yang dikirim ke AI sinkron."""
        if self.ai_parser is not None:
            try:
                new_menus = self._new_parser().matcher.menus
                self.ai_parser = AIParser(new_menus)
            except Exception as e:
                warning(f"Gagal reload AI parser setelah update menu: {e}")

    def parse_message(
        self,
        text: str,
        username: str = "",
        chat_id: str = "",
    ) -> str:

        if self._is_command(text):
            reply = self.handle_command(text)
            self.logger.write(username, chat_id, text, "COMMAND")
            return reply

        if self._is_preorder(text):
            return self.handle_preorder(text, chat_id, username)

        try:
            info(f"Memproses order dari {username or chat_id} ({len(text)} karakter)")

            # Smart Order Intelligence: kalau lapisan intelijen cukup
            # yakin (confidence >= threshold) dan menghasilkan teks yang
            # dinormalkan, parser membaca versi normal itu. Selain itu
            # (tidak yakin / error / nonaktif) teks ASLI yang dipakai --
            # perilaku bot sama persis seperti sebelum lapisan ini ada.
            parser_text = text

            if self.intelligence is not None:
                intel = self.intelligence.analyze(text)

                # Pesan yang BUKAN order (tanya menu/harga, batal) tidak
                # diproses sebagai order -- dibalas langsung dengan
                # petunjuk. Hanya untuk intent yang eksplisit; ragu
                # sedikit saja = jalur order biasa.
                intent_reply = self._intent_reply(intel)

                if intent_reply:
                    self.logger.write(
                        username, chat_id, text, f"INTENT:{intel.intent}"
                    )
                    return intent_reply

                if intel.rewritten and intel.confidence >= INTELLIGENCE_CONFIDENCE:
                    parser_text = intel.normalized_text
                    info(
                        "[INTELLIGENCE] Teks dinormalkan "
                        f"(confidence={intel.confidence})"
                    )

            parser = self._new_parser()
            items = parser.parse(parser_text)

            info(f"Parser menghasilkan {len(items)} item")

            order = self.order_builder.build(parser, items)

            summary = None
            used_ai = False
            discrepancy_note = ""

            # Hybrid: kalau parser regex gagal membaca sebagian teks
            # (ada baris/menu tak dikenal, atau tidak ada item sama
            # sekali), coba lapisan AI dulu. Kalau AI gagal apa pun
            # sebabnya, hasil regex tetap dipakai seperti biasa.
            # Konteks dari parser regex (customer dikenal, baris gagal)
            # dikirim ke AI sebagai panduan tambahan (Feature #1).
            if self._should_use_ai(order, text):
                ctx = self._build_ai_context(order)
                ai_result = self._parse_with_ai(text, context=ctx)

                if ai_result is not None:
                    order, summary = ai_result
                    used_ai = True
                    info(f"Order diproses lewat lapisan AI ({self.ai_parser.provider})")

            if not used_ai:
                try:
                    summary = self.receipt_builder.build(parser)
                except Exception as bill_error:
                    warning(f"Gagal menghitung bill: {bill_error}")

            # Cross-check: untuk order PENTING (banyak customer / nominal
            # besar) yang parser regex baca "tanpa keraguan", tetap
            # minta AI membaca ulang dan bandingkan. Parser bisa salah
            # dengan percaya diri (mis. "NASI biasa KOMPLIT PERKEDEL"
            # ke-match PERKEDEL skor 100), dan order besar paling mahal
            # kalau salah -- jadi di situ AI dipakai sebagai pemeriksa.
            if (
                not used_ai
                and summary is not None
                and self._should_crosscheck(summary)
            ):
                ai_result = self._parse_with_ai(text)

                if ai_result is not None:
                    _, ai_summary = ai_result

                    if self._summaries_differ(summary, ai_summary):
                        if chat_id:
                            self.pending_ai_versions[chat_id] = ai_summary

                        discrepancy_note = self._format_discrepancy(
                            summary, ai_summary
                        )
                        info("Cross-check AI menemukan perbedaan")
                    elif chat_id:
                        self.pending_ai_versions.pop(chat_id, None)
            elif chat_id:
                self.pending_ai_versions.pop(chat_id, None)

            if summary is not None:
                self._apply_discounts_to_summary(summary)

            if chat_id and summary is not None:
                self.last_orders[chat_id] = summary
                self.last_order_texts[chat_id] = text

                if self.store is not None:
                    try:
                        self.last_order_ids[chat_id] = self.store.save_order(
                            chat_id, summary
                        )
                    except Exception as db_error:
                        warning(f"Gagal simpan order ke DB: {db_error}")

            reply = self.formatter.format(order, summary)

            if used_ai:
                reply = (
                    "🤖 Format order tidak standar -- dibaca dengan bantuan "
                    "AI, mohon dicek sekilas.\n\n" + reply
                )

            if discrepancy_note:
                reply += "\n\n" + discrepancy_note

            foreign_block = self.formatter.format_foreign_resto_block(order)

            if chat_id:
                self.last_foreign_blocks[chat_id] = foreign_block

            if foreign_block:
                reply += f"\n\n{foreign_block}"

            if chat_id and summary is not None:
                reply += (
                    "\n\n📍 Lokasi pengantaran & nama pemesan?\n"
                    "Balas dengan format: LOKASI/NAMA\n"
                    "Contoh: KD/NICOLAS"
                )

            self.logger.write(username, chat_id, text, "SUCCESS")

            return reply

        except Exception as e:
            error(f"TelegramAdapter error: {e!r}")

            self.logger.write(
                username,
                chat_id,
                text,
                f"ERROR: {repr(e)}",
            )

            return (
                "Terjadi kesalahan saat memproses order.\n\n"
                f"{type(e).__name__}\n"
                f"{e}"
            )

    @staticmethod
    def _is_unknown_name(name: str) -> bool:
        """Order polos tanpa nama customer per-item disimpan dengan nama
        internal 'UNKNOWN' -- jangan tampilkan literal itu ke user."""
        return (name or "").strip().upper() == "UNKNOWN"

    # ==========================================================
    # BALASAN INTENT (Smart Order Intelligence)
    # Hanya untuk intent NON-order yang eksplisit. Return None =
    # proses sebagai order biasa (jalur lama).
    # ==========================================================
    @staticmethod
    def _intent_reply(intel) -> Optional[str]:
        if intel is None:
            return None

        if intel.intent == "ASK_MENU":
            return (
                "📋 Mau lihat menu? Ketik /daftarmenu untuk daftar menu "
                "lengkap beserta harganya."
            )

        if intel.intent == "ASK_PRICE":
            matched = [i for i in intel.items if i.get("menu")]

            if matched:
                lines = ["💰 Harga menu:"]

                for item in matched:
                    price = f"{item.get('price', 0):,.0f}".replace(",", ".")
                    lines.append(f"- {item['menu']}: {price}R")

                lines.append("")
                lines.append("Ketik /daftarmenu untuk daftar lengkap.")
                return "\n".join(lines)

            return (
                "💰 Mau tanya harga? Sebut nama menunya, atau ketik "
                "/daftarmenu untuk daftar menu beserta harganya."
            )

        # Batal murni (tanpa menyebut menu apa pun di teks): kasih
        # petunjuk command. Kalau ada menu tersebut, biarkan jalur
        # order biasa yang menangani (bisa jadi bagian teks order).
        if intel.intent == "CANCEL" and not intel.items:
            return (
                "Untuk mengubah order terakhir:\n"
                "- /hapus <menu> -- hapus item\n"
                "- /ganti <menu lama> > <menu baru> -- ganti item\n"
                "- /batalpreorder <id> -- batalkan pre-order"
            )

        return None

    # ==========================================================
    # LAPISAN AI (hybrid)
    # ==========================================================
    def _should_use_ai(self, order: Dict[str, Any], text: str = "") -> bool:
        if self.ai_parser is None or not self.ai_parser.available:
            return False

        # Hasil fuzzy match berskor rendah artinya parser cuma menebak.
        # (Peringatan lain, mis. "item tidak memiliki customer", normal
        # untuk order polos dan bukan pemicu AI.)
        low_fuzzy = any(
            "Fuzzy Match" in w for w in order.get("warnings") or []
        )

        if (
            order.get("unknown")
            or order.get("unknown_lines")
            or low_fuzzy
            or not order.get("items")
        ):
            return True

        # Cek cakupan: parser bisa saja "percaya diri" salah -- kalimat
        # panjang penuh basa-basi kadang di-fuzzy-match jadi 1 item
        # tanpa peringatan apa pun. Hitung kata di teks yang TIDAK
        # terwakili di hasil parse (nama menu/customer/catatan) -- kalau
        # banyak yang tidak terwakili, teksnya kemungkinan besar bukan
        # format order standar dan layak dicek AI.
        if text:
            text_words = re.findall(r"[a-z]+", text.lower())

            covered = set()

            for item in order.get("items") or []:
                for source in (item.menu, item.note, item.customer):
                    covered.update(re.findall(r"[a-z]+", str(source).lower()))

            unaccounted = [
                w for w in text_words if len(w) > 1 and w not in covered
            ]

            if len(unaccounted) >= max(5, len(text_words) // 2):
                return True

        return False

    # ==========================================================
    # CROSS-CHECK AI (order penting yang parser "yakin")
    # ==========================================================
    def _should_crosscheck(self, summary: OrderSummary) -> bool:
        if self.ai_parser is None or not self.ai_parser.available:
            return False

        if summary is None or not summary.invoices:
            return False

        total_items = sum(len(inv.items) for inv in summary.invoices)

        return (
            len(summary.invoices) >= CROSSCHECK_MIN_CUSTOMERS
            or total_items >= CROSSCHECK_MIN_ITEMS
            or summary.grand_total_riel >= CROSSCHECK_MIN_TOTAL
        )

    @staticmethod
    def _summary_item_counter(summary: OrderSummary) -> Counter:
        """Ringkas order jadi multiset (nama customer, menu, qty) supaya
        dua hasil bisa dibandingkan tanpa peduli urutan."""
        return Counter(
            (
                (inv.telegram_name or "").strip().upper(),
                (item.menu or "").strip().upper(),
                item.qty,
            )
            for inv in summary.invoices
            for item in inv.items
        )

    def _summaries_differ(self, a: OrderSummary, b: OrderSummary) -> bool:
        return self._summary_item_counter(a) != self._summary_item_counter(b)

    def _format_discrepancy(
        self, regex_summary: OrderSummary, ai_summary: OrderSummary
    ) -> str:
        reg = self._summary_item_counter(regex_summary)
        ai = self._summary_item_counter(ai_summary)

        only_reg = reg - ai
        only_ai = ai - reg

        def fmt(counter):
            lines = []
            for (name, menu, qty), n in counter.items():
                who = "" if self._is_unknown_name(name) else f" ({name})"
                for _ in range(n):
                    lines.append(f"- {menu} x{qty}{who}")
            return lines[:8]

        lines = [
            "⚠️ AI membaca beberapa item BERBEDA dari hasil di atas:",
            "",
            "Versi sekarang (dipakai):",
        ]
        lines += fmt(only_reg) or ["- (tidak ada)"]
        lines += ["", "Versi AI:"]
        lines += fmt(only_ai) or ["- (tidak ada)"]
        lines += [
            "",
            f"Total versi AI: {ai_summary.grand_total_riel:,.0f} Riel.",
            "Kalau versi AI yang benar, balas: /pakaiai",
        ]

        return "\n".join(lines)

    def switch_to_ai_version(self, chat_id: str) -> str:
        """Ganti order terakhir dengan versi hasil pembacaan AI
        (dipicu /pakaiai setelah cross-check menemukan perbedaan)."""
        ai_summary = self.pending_ai_versions.get(chat_id) if chat_id else None

        if ai_summary is None or not ai_summary.invoices:
            return (
                "Tidak ada versi AI yang berbeda untuk order terakhir. "
                "/pakaiai hanya bisa dipakai kalau bot menandai ada "
                "perbedaan pembacaan."
            )

        # Bawa lokasi & nama pemesan dari versi sekarang (kalau sudah
        # dibalas LOKASI/NAMA) supaya tidak hilang saat ditukar.
        current = self.last_orders.get(chat_id)

        if current is not None:
            ai_summary.destination = current.destination
            ai_summary.orderer_name = current.orderer_name

            for inv in ai_summary.invoices:
                inv.destination = current.destination

        # Belajar dari konfirmasi AI: simpan koreksi supaya parser biasa
        # bisa langsung benar di lain waktu tanpa perlu AI.
        self._learn_from_ai_confirmation(current, ai_summary)

        self.last_orders[chat_id] = ai_summary
        self.pending_ai_versions.pop(chat_id, None)
        self._persist_last(chat_id)
        self.logger.write("", chat_id, "/pakaiai", "AI_VERSION")

        order = {"groups": {}, "warnings": [], "unknown": []}
        reply = "✅ Order diganti ke versi AI:\n\n" + self.formatter.format(
            order, ai_summary
        )
        reply += "\n\nGunakan /invoice untuk lihat hasil terbaru (PNG + PDF)."

        return reply

    def _learn_from_ai_confirmation(
        self, parser_summary, ai_summary
    ) -> None:
        """Saat user konfirmasi AI benar (/pakaiai), pelajari perbedaan:
        untuk setiap item yang parser salah baca, simpan koreksi
        search_text -> nama menu AI supaya parser bisa langsung benar
        lain kali tanpa bantuan AI."""

        if parser_summary is None or ai_summary is None:
            return

        # Kumpulkan semua item parser: (customer, menu_upper) -> search_text
        # Kalau ada lebih dari satu item dengan customer+menu sama, pakai
        # yang terakhir (tidak kritis karena search_text kemungkinan sama).
        parser_items: dict = {}
        for inv in parser_summary.invoices:
            for item in inv.items:
                key = (
                    (inv.telegram_name or "").strip().upper(),
                    (item.menu or "").strip().upper(),
                )
                if getattr(item, "search_text", ""):
                    parser_items[key] = (item.menu, item.search_text)

        # Kumpulkan item AI: (customer, menu_upper) -> menu_name
        ai_items: dict = {}
        for inv in ai_summary.invoices:
            for item in inv.items:
                key = (
                    (inv.telegram_name or "").strip().upper(),
                    (item.menu or "").strip().upper(),
                )
                ai_items[key] = item.menu

        # Cari item yang ada di AI tapi TIDAK ada di parser dengan key sama
        # -- artinya parser salah baca menu itu.
        # Strategi: pasangkan berdasarkan customer yang sama.
        parser_by_customer: dict = {}
        for (cust, menu_up), (menu_name, stext) in parser_items.items():
            parser_by_customer.setdefault(cust, []).append((menu_up, menu_name, stext))

        ai_by_customer: dict = {}
        for (cust, menu_up), menu_name in ai_items.items():
            ai_by_customer.setdefault(cust, []).append((menu_up, menu_name))

        learned = 0
        for cust, ai_list in ai_by_customer.items():
            parser_list = parser_by_customer.get(cust, [])

            # Pasangkan posisi per customer (urutan item biasanya sama)
            for idx, (ai_menu_up, ai_menu_name) in enumerate(ai_list):
                if idx >= len(parser_list):
                    break

                p_menu_up, p_menu_name, p_stext = parser_list[idx]

                # Hanya belajar kalau parser dan AI beda menu
                if p_menu_up == ai_menu_up:
                    continue

                self._maybe_learn_correction(p_stext, p_menu_name, ai_menu_name)
                learned += 1

        if learned:
            info(f"AI confirmation: belajar {learned} koreksi baru dari /pakaiai")

    def check_with_ai(self, chat_id: str) -> str:
        """Minta AI membaca ulang order terakhir di chat ini dan
        bandingkan dengan hasil parser. Dipicu tombol 'Cek AI'."""

        if not chat_id:
            return "Chat ID tidak ditemukan."

        if self.ai_parser is None or not self.ai_parser.available:
            return (
                "Lapisan AI tidak aktif. Isi GROQ_API_KEY atau "
                "GEMINI_API_KEY di environment variable untuk mengaktifkan."
            )

        raw_text = self.last_order_texts.get(chat_id)

        if not raw_text:
            return "Tidak ada teks order yang bisa dicek ulang di chat ini."

        current = self.last_orders.get(chat_id)

        if current is None or not current.invoices:
            return "Tidak ada order terakhir yang bisa dicek ulang."

        ai_result = self._parse_with_ai(raw_text)

        if ai_result is None:
            last_err = self.ai_parser.last_error if self.ai_parser else ""
            provider = self.ai_parser.provider if self.ai_parser else "-"
            if last_err and "429" in last_err:
                if "Gemini fallback juga gagal" in last_err:
                    # Groq rate limit + Gemini juga error -- tampilkan
                    # kode HTTP saja (bukan URL/key supaya tidak bocor)
                    code = "401" if "401" in last_err else (
                        "403" if "403" in last_err else "error"
                    )
                    return (
                        "⏳ Groq rate limit tercapai, Gemini cadangan juga gagal.\n\n"
                        f"Error Gemini: {code} Unauthorized\n\n"
                        "Kemungkinan penyebab:\n"
                        "- GEMINI_API_KEY tidak valid atau sudah direvoke\n"
                        "- Buat key baru di https://aistudio.google.com/apikey\n"
                        "  lalu update di .env dan restart bot"
                    )
                else:
                    # Hanya Groq rate limit, tidak ada provider cadangan
                    return (
                        "⏳ Batas request AI (rate limit) tercapai.\n\n"
                        "Tunggu beberapa menit lalu coba lagi.\n"
                        "Atau tambahkan OPENROUTER_API_KEY di .env sebagai "
                        "cadangan gratis (https://openrouter.ai/settings/keys)."
                    )
            detail = f"\n\nDetail: {last_err}" if last_err else ""
            return (
                f"❌ AI gagal membaca ulang order ini "
                f"(provider: {provider}).{detail}"
            )

        _, ai_summary = ai_result

        if not self._summaries_differ(current, ai_summary):
            return "✅ AI membaca order SAMA persis dengan parser -- tidak ada perbedaan."

        # Simpan versi AI supaya /pakaiai bisa dipakai
        self.pending_ai_versions[chat_id] = ai_summary

        return self._format_discrepancy(current, ai_summary)

    # ==========================================================
    # PRE-ORDER (order untuk hari berikutnya)
    # Format: "BESOK: <teks order>" atau "BESOK\n<teks order>"
    # ==========================================================

    @staticmethod
    def _is_preorder(text: str) -> bool:
        return bool(_PREORDER_PREFIX_RE.match(text.strip()))

    @staticmethod
    def _extract_preorder_text(text: str) -> str:
        """Buang prefix BESOK dari teks, kembalikan isi order-nya."""
        lines = text.strip().split("\n")
        first = _PREORDER_PREFIX_RE.sub("", lines[0]).strip()
        if first:
            lines[0] = first
        else:
            lines = lines[1:]
        return "\n".join(lines).strip()

    def handle_preorder(
        self, text: str, chat_id: str, username: str = ""
    ) -> str:
        """Simpan pre-order untuk besok, kembalikan konfirmasi ke user."""
        order_text = self._extract_preorder_text(text)

        if not order_text:
            return (
                "Format pre-order:\n\n"
                "BESOK: NASI GORENG: 12.000R\nBudi\n\n"
                "atau:\n\n"
                "BESOK\nNASI GORENG: 12.000R\nBudi\n\n"
                "Tulis 'BESOK' di awal pesan, lalu isi order seperti biasa."
            )

        # Validasi: coba parse dulu supaya teks tidak sampah
        try:
            parser = self._new_parser()
            items = parser.parse(order_text)
            if not items:
                return (
                    "⚠️ Format order tidak terbaca setelah kata 'BESOK'.\n\n"
                    "Pastikan format order sudah benar, contoh:\n"
                    "BESOK: NASI GORENG: 12.000R\nBudi"
                )
        except Exception:
            pass

        if self.store is None:
            return "❌ Database tidak tersedia, pre-order tidak bisa disimpan."

        try:
            from .timezone_utils import now_jakarta
        except ImportError:
            from timezone_utils import now_jakarta

        tomorrow = (now_jakarta() + timedelta(days=1)).strftime("%Y-%m-%d")

        try:
            self.store.save_preorder(str(chat_id), order_text, tomorrow)
        except Exception as e:
            warning(f"Gagal simpan pre-order: {e}")
            return "❌ Gagal menyimpan pre-order. Coba lagi."

        self.logger.write(username, chat_id, text, "PREORDER")

        return (
            f"📅 Pre-order untuk *{tomorrow}* tersimpan!\n\n"
            f"Isi order:\n{order_text}\n\n"
            "Order akan diproses otomatis besok jam 07:00 pagi dan "
            "hasilnya dikirim ke chat ini.\n\n"
            "Untuk melihat pre-order pending: /lihatpreorder\n"
            "Untuk batalkan: /batalpreorder <nomor>"
        )

    def list_preorders(self, chat_id: str) -> str:
        """Tampilkan pre-order pending milik chat ini."""
        if self.store is None:
            return "Database tidak tersedia."

        try:
            from .timezone_utils import now_jakarta
        except ImportError:
            from timezone_utils import now_jakarta

        today = now_jakarta().strftime("%Y-%m-%d")

        try:
            all_pending = self.store.upcoming_preorders(today)
        except Exception as e:
            return f"Gagal memuat pre-order: {e}"

        mine = [p for p in all_pending if p["chat_id"] == str(chat_id)]

        if not mine:
            return "Tidak ada pre-order pending untuk chat ini."

        lines = [f"📅 Pre-order pending ({len(mine)}):\n"]
        for p in mine:
            preview = p["order_text"][:80].replace("\n", " / ")
            if len(p["order_text"]) > 80:
                preview += "..."
            lines.append(
                f"#{p['id']} • {p['scheduled_date']}\n  {preview}"
            )
        lines.append("\nBatalkan dengan: /batalpreorder <nomor>")
        return "\n".join(lines)

    def cancel_preorder(self, preorder_id: int, chat_id: str) -> str:
        """Batalkan pre-order berdasarkan ID."""
        if self.store is None:
            return "Database tidak tersedia."

        try:
            ok = self.store.cancel_preorder(preorder_id, chat_id)
        except Exception as e:
            return f"Gagal membatalkan pre-order: {e}"

        if ok:
            return f"✅ Pre-order #{preorder_id} berhasil dibatalkan."
        return (
            f"❌ Pre-order #{preorder_id} tidak ditemukan atau "
            "sudah diproses/dibatalkan."
        )

    def _maybe_learn_correction(
        self, source_text: str, old_menu_name: str, new_menu_name: str
    ) -> None:
        """Belajar dari /ganti: kalau tulisan `source_text` dulu SALAH
        kebaca `old_menu_name` dan dibetulkan jadi `new_menu_name`,
        ingat pemetaannya supaya tulisan yang sama tidak salah lagi.

        Hanya belajar kalau menu BARU lebih 'menjelaskan' tulisan asli
        daripada menu lama (overlap kata lebih banyak). Ini membedakan
        KOREKSI salah-baca (mis. "nasi biasa komplit perkedel" yang
        salah jadi PERKEDEL, dibetulkan ke NASI UDUK KOMPLIT PERKEDEL --
        overlap naik) dari PERUBAHAN pesanan biasa (customer ganti
        bubur ke nasi kalasan -- overlap tidak naik), supaya memori
        tidak keracunan.
        """
        if not source_text:
            return

        key = correction_key(source_text)

        if not key:
            return

        source_words = set(_tokenize(source_text))
        old_overlap = len(set(_tokenize(old_menu_name)) & source_words)
        new_overlap = len(set(_tokenize(new_menu_name)) & source_words)

        if new_overlap <= old_overlap:
            return

        self.corrections[key] = new_menu_name

        if self.store is not None:
            try:
                self.store.save_correction(key, new_menu_name)
            except Exception as db_error:
                warning(f"Gagal simpan koreksi: {db_error}")

        info(f"Belajar koreksi: '{key}' -> {new_menu_name}")

    def _build_ai_context(self, order: Dict[str, Any]) -> Dict:
        """Kumpulkan informasi dari hasil parsial parser regex untuk
        dikirim ke AI sebagai konteks (Feature #1)."""
        known_customers = [
            c for c in order.get("groups", {}).keys()
            if c and c.strip().upper() != "UNKNOWN"
        ]
        parsed_items = [
            {
                "customer": getattr(item, "customer", ""),
                "menu": item.menu,
                "qty": item.qty,
            }
            for item in order.get("items", [])
        ]
        # unknown_lines: baris tak terbaca sama sekali
        # unknown: teks yang terbaca sebagai menu tapi tak ada di katalog
        unknown_lines = list(order.get("unknown_lines") or [])
        unknown_menus = list(order.get("unknown") or [])

        return {
            "known_customers": known_customers,
            "parsed_items": parsed_items,
            "unknown_lines": unknown_lines + unknown_menus,
        }

    def _normalize_line_for_key(self, line: str) -> str:
        """Bersihkan baris order mentah untuk dijadikan kunci koreksi:
        hapus harga, simbol, dan angka qty di akhir."""
        text = re.sub(r"\d[\d.,]*\s*[Rr]", " ", line)
        for ch in ":;,|()[]=<>+":
            text = text.replace(ch, " ")
        text = re.sub(r"\s+\d+\s*$", "", text.strip())
        return text.strip()

    def _save_ai_correction(self, key: str, menu_name: str) -> None:
        """Simpan alias hasil inferensi AI ke memori + DB (Feature #3)."""
        self.corrections[key] = menu_name

        if self.store is not None:
            try:
                self.store.save_correction(key, menu_name)
            except Exception as db_error:
                warning(f"Gagal simpan koreksi AI: {db_error}")

        info(f"AI alias: '{key}' -> {menu_name}")

    def _auto_learn_from_ai(
        self,
        problem_lines: List[str],
        ai_items: List[Dict[str, Any]],
    ) -> None:
        """Pasangkan baris yang gagal dibaca parser dengan item AI yang
        paling cocok; simpan sebagai koreksi kalau overlap token cukup
        (Feature #3).  Threshold konservatif supaya tidak over-generalize."""
        if not problem_lines or not ai_items:
            return

        learned = 0

        for line in problem_lines:
            # Lewati baris yang mengandung '+' atau '/' -- kemungkinan
            # multi-item atau combo, bukan sekadar typo nama menu tunggal.
            # Auto-alias untuk kasus multi-item berisiko kehilangan item.
            if re.search(r"[+/]", line):
                continue

            normalized = self._normalize_line_for_key(line)
            if not normalized:
                continue

            key = correction_key(normalized)
            if not key:
                continue

            # Skip kunci terlalu panjang (> 5 token) -- kemungkinan
            # bukan sekadar typo, bisa multi-item yang belum tersplit.
            if len(key.split()) > 5:
                continue

            # Jangan timpa koreksi yang sudah ada (human atau AI lama)
            if key in self.corrections:
                continue

            line_tokens = set(_tokenize(normalized))
            if not line_tokens:
                continue

            best_item = None
            best_overlap = 0

            for ai_item in ai_items:
                menu_tokens = set(_tokenize(ai_item["menu"]))
                overlap = len(menu_tokens & line_tokens)

                if overlap > best_overlap:
                    best_overlap = overlap
                    best_item = ai_item

            # Minimal 2 token nama menu harus cocok dengan baris
            if best_item is not None and best_overlap >= 2:
                self._save_ai_correction(key, best_item["menu"])
                learned += 1

        if learned:
            info(f"Auto-alias AI: {learned} koreksi baru tersimpan")

    def _parse_with_ai(self, text: str, context: Optional[Dict] = None):
        """Kirim teks order ke lapisan AI (Groq/Gemini), bangun
        (order, summary) dari jawabannya. Return None kalau gagal --
        caller fallback ke hasil parser regex.

        context: dict opsional dari _build_ai_context() -- diteruskan
        ke AIParser.parse() sebagai panduan (Feature #1), dan dipakai
        untuk auto-simpan koreksi setelah AI berhasil baca (Feature #3)."""

        ai_items = self.ai_parser.parse(text, context=context)

        # Feature #3: auto-simpan alias AI untuk baris yang parser gagal baca
        if ai_items and context:
            problem_lines = context.get("unknown_lines") or []
            if problem_lines:
                self._auto_learn_from_ai(problem_lines, ai_items)

        if not ai_items:
            return None

        # Peta nama menu -> entri katalog (nama dari AI harusnya persis,
        # tapi tetap difuzzy-kan sebagai jaring pengaman).
        matcher = self._new_parser().matcher
        by_name = {(m["name"] or "").strip().upper(): m for m in matcher.menus}

        items: List[OrderItem] = []

        for entry in ai_items:
            menu = by_name.get((entry.get("menu") or "").strip().upper())

            if menu is None:
                menu, _score = matcher.search(entry.get("menu") or "")

            if menu is None:
                warning(f"AI menyebut menu di luar katalog: {entry.get('menu')}")
                continue

            customer = (entry.get("customer") or "").strip().upper() or "UNKNOWN"

            items.append(
                OrderItem(
                    customer=customer,
                    menu=menu["name"],
                    qty=entry["qty"],
                    price=menu["price"],
                    category=menu["category"],
                    raw=entry["menu"],
                    note=entry["note"],
                    resto=menu.get("resto", ""),
                    emoji=menu.get("emoji", ""),
                )
            )

        if not items:
            return None

        # Susun jadi OrderSummary (grouping per customer, urutan sesuai
        # kemunculan pertama) -- struktur yang sama dengan jalur regex,
        # supaya /struk //invoice //tambah //hapus //ganti semuanya
        # jalan tanpa peduli order dibaca regex atau AI.
        groups: Dict[str, List[OrderItem]] = {}

        for item in items:
            groups.setdefault(item.customer, []).append(item)

        bill = BillGenerator()
        today = datetime.now().strftime("%Y%m%d")
        invoices: List[Invoice] = []
        grand_riel = 0
        grand_usd = 0.0

        for i, (customer, customer_items) in enumerate(groups.items(), start=1):
            invoice = Invoice(
                invoice_no=f"INV-{today}-{i:03d}",
                telegram_name=customer or "UNKNOWN",
                items=customer_items,
            )
            invoice = bill.calculate(invoice)
            invoices.append(invoice)
            grand_riel += invoice.total_riel
            grand_usd += invoice.total_usd

        summary = OrderSummary(
            invoices=invoices,
            grand_total_riel=grand_riel,
            grand_total_usd=round(grand_usd, 2),
        )

        order = {
            "items": items,
            "groups": groups,
            "summary": {},
            "warnings": [],
            "unknown": [],
            "unknown_lines": [],
        }

        return order, summary

    def _apply_discounts_to_summary(self, summary: OrderSummary) -> None:
        """Isi item.discount_riel berdasarkan tabel discounts, lalu
        recalculate total invoice dan grand total."""
        if self.store is None:
            return
        discount_map = self.store.get_discount_map()
        if not discount_map:
            return

        bill = BillGenerator()
        for invoice in summary.invoices:
            changed = False
            for item in invoice.items:
                disc = discount_map.get((item.menu or "").upper())
                if disc is None:
                    item.discount_riel = 0
                    continue
                if disc["type"] == "persen":
                    item.discount_riel = max(0, int(item.price * disc["value"] / 100))
                else:
                    item.discount_riel = max(0, min(item.price, int(disc["value"])))
                changed = True
            if changed:
                bill.calculate(invoice)

        summary.grand_total_riel = sum(inv.total_riel for inv in summary.invoices)
        summary.grand_total_usd = round(
            sum(inv.total_usd for inv in summary.invoices), 2
        )

    def _persist_last(self, chat_id: str) -> None:
        """Tulis ulang order terakhir chat ini ke DB setelah dimodifikasi
        (/tambah, /hapus, /ganti, balasan lokasi). Kegagalan DB tidak
        boleh mengganggu balasan bot -- cukup dicatat di log."""

        if self.store is None or not chat_id:
            return

        summary = self.last_orders.get(chat_id)
        order_id = self.last_order_ids.get(chat_id)

        if summary is None or order_id is None:
            return

        try:
            self.store.update_order(order_id, summary)
        except Exception as db_error:
            warning(f"Gagal update order di DB: {db_error}")

    def get_last_order(self, chat_id: str) -> Optional[OrderSummary]:
        summary = self.last_orders.get(chat_id)

        if summary is not None:
            return summary

        # Tidak ada di memori (mis. bot baru restart) -- coba muat dari
        # DB supaya /struk //invoice //tambah dkk tetap jalan.
        if self.store is not None and chat_id:
            try:
                loaded = self.store.load_last_order(chat_id)
            except Exception as db_error:
                warning(f"Gagal muat order dari DB: {db_error}")
                loaded = None

            if loaded is not None:
                order_id, summary = loaded
                self.last_orders[chat_id] = summary
                self.last_order_ids[chat_id] = order_id
                return summary

        return None

    def get_last_foreign_block(self, chat_id: str) -> str:
        return self.last_foreign_blocks.get(chat_id, "")

    # ==========================================================
    # ADD TO LAST ORDER (dipicu command /tambah)
    # Customer kadang sudah kirim order, lalu nyusul mau nambah menu
    # lagi sebelum staf selesai proses -- daripada bikin order terpisah
    # (yang harus digabung manual), /tambah menggabungkan menu baru ini
    # ke order TERAKHIR di chat ini. Customer dengan nama yang sama
    # nempel jadi 1 invoice (bukan invoice terpisah).
    # ==========================================================
    def add_to_last_order(
        self,
        text: str,
        chat_id: str,
        username: str = "",
    ) -> str:
        existing = self.get_last_order(chat_id) if chat_id else None

        if existing is None or not existing.invoices:
            return (
                "Belum ada order sebelumnya di chat ini yang bisa "
                "ditambah.\nKirim dulu teks order-nya, baru pakai "
                "/tambah kalau mau menambah menu lagi."
            )

        try:
            info(
                f"Memproses tambahan order dari {username or chat_id} "
                f"({len(text)} karakter)"
            )

            parser = self._new_parser()
            items = parser.parse(text)

            if not items:
                return "Tidak ada menu yang terbaca dari teks tambahan ini."

            order = self.order_builder.build(parser, items)
            addition = self.receipt_builder.build(parser)

        except Exception as e:
            error(f"TelegramAdapter add_to_last_order error: {e!r}")
            self.logger.write(username, chat_id, text, f"ERROR: {repr(e)}")

            return (
                "Terjadi kesalahan saat memproses tambahan order.\n\n"
                f"{type(e).__name__}\n"
                f"{e}"
            )

        self._apply_discounts_to_summary(addition)
        self._merge_order_summary(existing, addition)
        self._apply_discounts_to_summary(existing)
        self._persist_last(chat_id)

        reply = "➕ TAMBAHAN ORDER\n\n" + self.formatter.format(order, addition)
        reply += (
            "\n\nGRAND TOTAL SELURUH ORDER (SETELAH DITAMBAH) : "
            f"{existing.grand_total_riel:,.0f} Riel "
            f"(${existing.grand_total_usd:,.2f})"
        )
        reply += "\n\nGunakan /invoice untuk lihat hasil terbaru (PNG + PDF)."

        foreign_block = self.formatter.format_foreign_resto_block(order)

        if foreign_block:
            if chat_id:
                previous_foreign = self.last_foreign_blocks.get(chat_id, "")
                self.last_foreign_blocks[chat_id] = "\n\n".join(
                    block for block in (previous_foreign, foreign_block) if block
                )

            reply += f"\n\n{foreign_block}"

        self.logger.write(username, chat_id, text, "ADDITION")

        return reply

    def _merge_order_summary(self, existing: OrderSummary, addition: OrderSummary) -> None:
        """Gabung invoice dari `addition` ke `existing` IN-PLACE -- customer
        dengan nama yang sama (case-insensitive) nempel jadi 1 invoice,
        customer baru ditambah sebagai invoice baru."""

        by_name = {
            (invoice.telegram_name or "").strip().upper(): invoice
            for invoice in existing.invoices
        }

        for new_invoice in addition.invoices:
            key = (new_invoice.telegram_name or "").strip().upper()
            current = by_name.get(key)

            if current is not None:
                current.items.extend(new_invoice.items)
                current.total_riel += new_invoice.total_riel
                current.total_usd = round(
                    current.total_usd + new_invoice.total_usd, 2
                )
            else:
                if existing.destination:
                    new_invoice.destination = existing.destination

                existing.invoices.append(new_invoice)
                by_name[key] = new_invoice

        existing.grand_total_riel += addition.grand_total_riel
        existing.grand_total_usd = round(
            existing.grand_total_usd + addition.grand_total_usd, 2
        )

    # ==========================================================
    # REMOVE FROM LAST ORDER (dipicu command /hapus)
    # Customer kadang batal salah satu menu setelah order terkirim.
    # /hapus <nama menu> mencari item yang cocok di order terakhir chat
    # ini lalu menghapusnya; total dihitung ulang, dan /struk //invoice
    # berikutnya sudah tanpa item itu. Kalau menu yang sama dipesan
    # beberapa customer, sebutkan namanya: "/hapus katsu curry budi".
    # ==========================================================
    def remove_from_last_order(
        self,
        query: str,
        chat_id: str,
        username: str = "",
    ) -> str:
        summary = self.get_last_order(chat_id) if chat_id else None

        if summary is None or not summary.invoices:
            return (
                "Belum ada order sebelumnya di chat ini yang bisa "
                "dihapus itemnya.\nKirim dulu teks order-nya."
            )

        query = query.strip()

        if not query:
            return (
                "Sebutkan menu yang mau dihapus, contoh:\n"
                "/hapus katsu curry\n"
                "Kalau menu itu dipesan beberapa orang, tambahkan "
                "namanya: /hapus katsu curry budi"
            )

        query_words = [w for w in re.findall(r"[A-Za-z0-9]+", query.lower())]

        # Kata terakhir query bisa jadi nama customer (buat membedakan
        # menu sama yang dipesan beberapa orang) -- coba dua-duanya.
        candidate_filters = [(query_words, None)]

        if len(query_words) > 1:
            maybe_name = query_words[-1].upper()
            names = {
                (inv.telegram_name or "").strip().upper() for inv in summary.invoices
            }

            if maybe_name in names:
                candidate_filters.insert(0, (query_words[:-1], maybe_name))

        matches = []
        used_filter = None

        for words, customer_filter in candidate_filters:
            matches = []

            for invoice in summary.invoices:
                if (
                    customer_filter
                    and (invoice.telegram_name or "").strip().upper() != customer_filter
                ):
                    continue

                for item in invoice.items:
                    item_words = set(
                        re.findall(r"[A-Za-z0-9]+", (item.menu or "").lower())
                    )

                    if all(w in item_words for w in words):
                        matches.append((invoice, item))

            if matches:
                used_filter = customer_filter
                break

        if not matches:
            current = ", ".join(
                item.menu
                if self._is_unknown_name(inv.telegram_name)
                else f"{item.menu} ({inv.telegram_name})"
                for inv in summary.invoices
                for item in inv.items
            )
            return (
                f"Tidak ada item yang cocok dengan '{query}' di order "
                f"terakhir.\n\nItem saat ini: {current}"
            )

        distinct_customers = {inv.telegram_name for inv, _ in matches}

        if len(matches) > 1 and len(distinct_customers) > 1 and not used_filter:
            listing = "\n".join(
                f"- {item.menu} ({inv.telegram_name})" for inv, item in matches
            )
            return (
                f"Menu itu cocok dengan {len(matches)} item milik beberapa "
                f"customer:\n{listing}\n\n"
                "Tambahkan nama customer-nya supaya jelas, contoh:\n"
                f"/hapus {query} {matches[0][0].telegram_name}"
            )

        # Kalau masih >1 dalam customer yang sama (menu duplikat), hapus
        # SATU saja -- yang pertama.
        invoice, item = matches[0]
        invoice.items.remove(item)

        removed_riel = item.price * item.qty
        invoice.total_riel -= removed_riel
        invoice.total_usd = round(invoice.total_riel / USD_RATE, 2)

        removed_invoice_note = ""

        if not invoice.items:
            summary.invoices.remove(invoice)

            if not self._is_unknown_name(invoice.telegram_name):
                removed_invoice_note = (
                    f"\n(Customer {invoice.telegram_name} tidak punya item "
                    "lagi, ikut dihapus dari order.)"
                )

        summary.grand_total_riel -= removed_riel
        summary.grand_total_usd = round(summary.grand_total_riel / USD_RATE, 2)

        self._persist_last(chat_id)
        self.logger.write(username, chat_id, f"/hapus {query}", "REMOVAL")

        owner = (
            ""
            if self._is_unknown_name(invoice.telegram_name)
            else f" milik {invoice.telegram_name}"
        )
        reply = (
            f"🗑️ Dihapus: {item.menu} x{item.qty} "
            f"({removed_riel:,.0f} Riel){owner}."
            f"{removed_invoice_note}\n\n"
            f"GRAND TOTAL SEKARANG : {summary.grand_total_riel:,.0f} Riel "
            f"(${summary.grand_total_usd:,.2f})"
        )

        if summary.invoices:
            reply += "\n\nGunakan /invoice untuk lihat hasil terbaru (PNG + PDF)."
        else:
            reply += "\n\nSemua item sudah terhapus -- order ini kosong."

        return reply

    # ==========================================================
    # REPLACE IN LAST ORDER (dipicu command /ganti)
    # Customer kadang mau tukar menu setelah order terkirim, mis. pesan
    # BUBUR AYAM lalu ganti ke NASI+AYAM KALASAN. Bentuk paling simpel:
    # "/ganti <menu baru> <nama>" -- item milik customer itu langsung
    # diganti (kalau dia cuma punya 1 item). Kalau customer punya
    # beberapa item, atau mau lebih eksplisit, pakai bentuk lengkap:
    # "/ganti <menu lama> jadi <menu baru> <nama>".
    # ==========================================================
    def replace_in_last_order(
        self,
        query: str,
        chat_id: str,
        username: str = "",
    ) -> str:
        summary = self.get_last_order(chat_id) if chat_id else None

        if summary is None or not summary.invoices:
            return (
                "Belum ada order sebelumnya di chat ini yang bisa "
                "diganti itemnya.\nKirim dulu teks order-nya."
            )

        query = query.strip()

        if not query:
            return (
                "Sebutkan menu penggantinya, contoh:\n"
                "/ganti nasi ayam kalasan rama\n"
                "(mengganti pesanan milik RAMA)\n\n"
                "Kalau customer itu punya beberapa item, sebutkan juga "
                "menu lamanya:\n"
                "/ganti bubur ayam jadi nasi ayam kalasan rama"
            )

        # Bentuk lengkap: "<menu lama> jadi <menu baru>" atau
        # "<menu lama> > <menu baru>"
        parts = re.split(r"\s*>\s*|\s+jadi\s+", query, maxsplit=1, flags=re.IGNORECASE)

        if len(parts) == 2:
            old_query, new_part = parts[0].strip(), parts[1].strip()
        else:
            old_query, new_part = None, query

        # Kata terakhir bisa jadi nama customer
        names = {
            (inv.telegram_name or "").strip().upper(): inv for inv in summary.invoices
        }
        new_words = new_part.split()
        customer_filter = None

        if len(new_words) > 1 and new_words[-1].upper() in names:
            customer_filter = new_words[-1].upper()
            new_menu_text = " ".join(new_words[:-1])
        else:
            new_menu_text = new_part

        if not new_menu_text.strip():
            return "Menu penggantinya belum disebutkan."

        # --- cari item lama yang mau diganti ---
        if old_query:
            old_words = [
                w for w in re.findall(r"[A-Za-z0-9]+", old_query.lower())
            ]
            matches = []

            for invoice in summary.invoices:
                if (
                    customer_filter
                    and (invoice.telegram_name or "").strip().upper() != customer_filter
                ):
                    continue

                for item in invoice.items:
                    item_words = set(
                        re.findall(r"[A-Za-z0-9]+", (item.menu or "").lower())
                    )

                    if all(w in item_words for w in old_words):
                        matches.append((invoice, item))

            if not matches:
                return (
                    f"Tidak ada item yang cocok dengan '{old_query}' "
                    "di order terakhir."
                )

            distinct = {inv.telegram_name for inv, _ in matches}

            if len(matches) > 1 and len(distinct) > 1 and not customer_filter:
                listing = "\n".join(
                    f"- {item.menu} ({inv.telegram_name})"
                    for inv, item in matches
                )
                return (
                    f"Menu itu cocok dengan {len(matches)} item milik "
                    f"beberapa customer:\n{listing}\n\n"
                    "Tambahkan nama customer-nya di akhir supaya jelas."
                )

            invoice, item = matches[0]

        elif customer_filter:
            invoice = names[customer_filter]

            if len(invoice.items) != 1:
                listing = "\n".join(f"- {i.menu}" for i in invoice.items)
                return (
                    f"{customer_filter} punya {len(invoice.items)} item:\n"
                    f"{listing}\n\n"
                    "Sebutkan menu lamanya juga, contoh:\n"
                    f"/ganti {invoice.items[0].menu.lower()} jadi "
                    f"{new_menu_text.lower()} {customer_filter}"
                )

            item = invoice.items[0]

        else:
            all_items = [
                (inv, it) for inv in summary.invoices for it in inv.items
            ]

            if len(all_items) != 1:
                # Order polos tanpa nama customer: satu-satunya cara
                # menunjuk item adalah lewat menu lamanya, jadi jangan
                # menyuruh user menyebut nama yang memang tidak ada.
                if all(
                    self._is_unknown_name(inv.telegram_name)
                    for inv in summary.invoices
                ):
                    return (
                        "Order ini punya beberapa item -- sebutkan menu "
                        "lamanya juga, contoh:\n"
                        "/ganti bubur ayam jadi nasi ayam kalasan"
                    )

                return (
                    "Order ini punya beberapa item -- sebutkan nama "
                    "customer-nya (dan menu lamanya kalau perlu), contoh:\n"
                    "/ganti nasi ayam kalasan rama\n"
                    "/ganti bubur ayam jadi nasi ayam kalasan rama"
                )

            invoice, item = all_items[0]

        # --- cari menu baru di katalog ---
        parser = self._new_parser()
        new_menu, score = parser.matcher.search((new_menu_text or "").upper())

        if new_menu is None:
            return (
                f"Menu pengganti '{new_menu_text}' tidak dikenali di "
                "daftar menu."
            )

        old_desc = f"{item.menu} x{item.qty} ({item.price * item.qty:,.0f} Riel)"
        delta = (new_menu["price"] - item.price) * item.qty

        # Rekam SEBELUM item diubah -- dipakai fitur "belajar dari
        # koreksi" (lihat _maybe_learn_correction).
        learn_source = getattr(item, "search_text", "")
        old_menu_name = item.menu

        item.menu = new_menu["name"]
        item.price = new_menu["price"]
        item.category = new_menu["category"]
        item.emoji = new_menu.get("emoji", "")
        item.resto = new_menu.get("resto", "")
        item.note = ""
        item.search_text = new_menu["name"]

        invoice.total_riel += delta
        invoice.total_usd = round(invoice.total_riel / USD_RATE, 2)
        summary.grand_total_riel += delta
        summary.grand_total_usd = round(summary.grand_total_riel / USD_RATE, 2)

        self._persist_last(chat_id)
        self._maybe_learn_correction(learn_source, old_menu_name, new_menu["name"])
        self.logger.write(username, chat_id, f"/ganti {query}", "REPLACEMENT")

        owner = (
            ""
            if self._is_unknown_name(invoice.telegram_name)
            else f" milik {invoice.telegram_name}"
        )

        return (
            f"🔄 Diganti{owner}:\n"
            f"{old_desc}\n→ {item.menu} x{item.qty} "
            f"({item.price * item.qty:,.0f} Riel)\n\n"
            f"GRAND TOTAL SEKARANG : {summary.grand_total_riel:,.0f} Riel "
            f"(${summary.grand_total_usd:,.2f})\n\n"
            "Gunakan /invoice untuk lihat hasil terbaru (PNG + PDF)."
        )

    def handle_location_reply(self, text: str, chat_id: str) -> Optional[str]:
        """
        Cek apakah `text` BERBENTUK balasan lokasi & nama pemesan
        (format "LOKASI/NAMA", mis. "KD/NICOLAS") untuk order terakhir di
        chat ini. Return None kalau bentuknya tidak cocok sama sekali --
        caller lalu memperlakukan `text` sebagai pesan biasa (order baru).

        Sengaja dideteksi dari BENTUK teksnya saja (bukan status "sedang
        menunggu balasan" yang disimpan di memori), karena status di
        memori bisa hilang kalau proses bot restart (redeploy, dsb) di
        antara pesan order dan balasan lokasinya -- kalau itu terjadi,
        balasan lokasi jangan sampai malah diproses sebagai order baru.
        """

        if not self._looks_like_location_reply(text):
            return None

        location, _, name = text.strip().partition("/")
        location = (location or "").strip().upper()
        name = (name or "").strip().upper()

        summary = self.get_last_order(chat_id) if chat_id else None

        if summary is None:
            return (
                "Formatnya kelihatan seperti lokasi & nama pemesan, tapi "
                "belum ada order yang sedang diproses di chat ini.\n"
                "Kirim dulu teks order-nya, baru balas LOKASI/NAMA "
                "setelah itu."
            )

        summary.destination = location
        summary.orderer_name = name

        # Nama pemesan yang dibalas di sini HANYA dicatat sebagai
        # penanggung jawab keseluruhan order (summary.orderer_name,
        # dipakai buat header "Pemesan: ..." & fallback nama di struk).
        # invoice.telegram_name TIDAK ditimpa -- kalau order-nya polos
        # tanpa nama customer per-item, biar tetap "UNKNOWN" di data
        # supaya /invoice bisa membiarkan kolom Nama Pemesan kosong,
        # bukan malah diisi ulang dengan nama ini.
        for invoice in summary.invoices:
            invoice.destination = location

        lines = [
            f"📍 Lokasi pengantaran disimpan: {location}",
            f"👤 Nama pemesan disimpan: {name}",
            "",
        ]
        lines.append("Gunakan /invoice untuk melihat hasilnya (PNG + PDF).")

        self._persist_last(chat_id)
        self.logger.write(username="", chat_id=chat_id, message=text, status="LOCATION_SET")

        return "\n".join(lines)

    def _looks_like_location_reply(self, text: str) -> bool:
        t = text.strip()

        if not t or "\n" in t:
            return False

        if t.count("/") != 1:
            return False

        location, _, name = t.partition("/")
        location = location.strip()
        name = name.strip()

        if not location or not name:
            return False

        # Tolak kalau ada format HARGA (angka ribuan + R, atau angka
        # dengan titik pemisah ribuan) -- itu jelas baris order, bukan
        # lokasi. Tapi angka biasa boleh (mis. "KD2", "BLOK A3", "LT5").
        if re.search(r"\d[\d.]*\s*[Rr]", t):
            return False

        if re.search(r"\d{4,}", t):
            return False

        # Tiap sisi tidak boleh terlalu panjang (lokasi/nama biasanya
        # pendek, 1-3 kata) -- teks order biasanya lebih panjang.
        if len(location.split()) > 4 or len(name.split()) > 4:
            return False

        return True

    def handle_command(self, text: str) -> str:
        command = text.strip().split()[0].lower()

        if command == "/ping":
            return "PONG"

        if command == "/version":
            return (
                f"Bot Version : {BOT_VERSION}\n"
                f"Parser Version : {PARSER_VERSION}\n"
                f"Business Rule Version : {BUSINESS_RULE_VERSION}"
            )

        if command == "/parser":
            return "Parser Ready"

        if command == "/stat":
            return "Parser statistics tersedia melalui Parser.summary()."

        if command == "/help":
            return (
                "/tutor - panduan lengkap cara pakai bot ini\n"
                "/ping\n"
                "/version\n"
                "/parser\n"
                "/stat\n"
                "/invoice - kirim invoice order terakhir (PNG + PDF)\n"
                "/tambah - tambah menu ke order terakhir di chat ini\n"
                "/hapus <menu> - hapus salah satu menu dari order terakhir\n"
                "/ganti <menu baru> <nama> - tukar menu customer itu\n"
                "/pakaiai - pakai versi AI kalau bot menandai perbedaan\n"
                "/lihatpreorder - lihat pre-order pending chat ini\n"
                "/batalpreorder <no> - batalkan pre-order\n"
                "/backup - (admin) kirim backup database order\n"
                "/tambahmenu - (admin) tambah menu baru ke daftar\n"
                "/hapusmenu - (admin) hapus menu dari daftar\n"
                "/updatemenu - (admin) ubah harga menu\n"
                "/daftarmenu - (admin) lihat daftar menu & kategori\n"
                "/exportmenu - (admin) bot kirim menu terkini sebagai Excel\n"
                "/importmenu - (admin) reply file Excel -> import menu\n"
                "/readymenu <menu> - (admin) tandai menu TERSEDIA hari ini\n"
                "/notready <menu> - (admin) tandai menu TIDAK TERSEDIA\n"
                "/lihatready - (admin) lihat status menu hari ini\n"
                "/resetready - (admin) reset semua menu ke tersedia\n"
                "/setdiskon <menu> PERSEN/NOMINAL <nilai> - (admin) set diskon menu\n"
                "/hapusdiskon <menu> - (admin) hapus diskon menu\n"
                "/daftardiskon - (admin) lihat semua diskon aktif\n"
                "\nBroadcast ke banyak grup (admin):\n"
                "/daftargrup - jalankan di grup, daftarkan ke broadcast\n"
                "/keluargrup - keluarkan grup ini dari daftar\n"
                "/listgrup - lihat semua grup terdaftar\n"
                "/broadcast - reply pesan/foto/dokumen -> kirim ke semua grup\n"
                "\nDine-in (Meja IN1-IN4 & OUT1-OUT4):\n"
                "/pesanmeja - tombol pilih meja (tambah/hapus/bayar)\n"
                "/ordermeja <nomor> - input order langsung ke meja\n"
                "/daftarmeja - lihat status semua meja\n"
                "/dinein <nomor> - mode: semua order masuk ke meja ini\n"
                "/selesaidinein - keluar mode dine-in\n"
                "/tagihan [nomor] - lihat tagihan meja\n"
                "/bayar [nomor] - tandai meja sudah bayar\n"
                "/invoicemeja [nomor] - invoice meja (PNG + PDF)\n"
                "/help\n\n"
                "Pre-order: tambahkan 'BESOK:' di awal pesan order\n"
                "Contoh: BESOK: NASI GORENG: 12.000R\nBudi"
            )

        return "Command tidak dikenal."

    def health(self) -> Dict[str, bool]:
        parser_ready = self.parser_cls is not None
        matcher_ready = False
        database_ready = True
        menu_loaded = False

        try:
            parser = self._new_parser()
            matcher = getattr(parser, "matcher", None)
            matcher_ready = matcher is not None
            menu_loaded = bool(getattr(matcher, "menus", None))
        except Exception:
            database_ready = False

        ai_active = bool(self.ai_parser is not None and self.ai_parser.available)

        return {
            "Parser Ready": parser_ready,
            "Matcher Ready": matcher_ready,
            "Database Ready": database_ready,
            "Menu Loaded": menu_loaded,
            "AI Aktif": ai_active,
        }

    def startup_banner(self) -> str:
        loaded_menu = self.health()["Menu Loaded"]

        return (
            "===================================\n"
            "MADAM LILY BOT\n"
            f"Version : {BOT_VERSION}\n"
            f"Parser : {PARSER_VERSION}\n"
            f"Business Rule : {BUSINESS_RULE_VERSION}\n"
            f"Loaded Menu : {loaded_menu}\n"
            "==================================="
        )

    def print_startup_banner(self):
        print(self.startup_banner())

    def _is_command(self, text: str) -> bool:
        return text.strip().startswith("/")

    def _load_parser(self):
        try:
            from .parser_engine import ParserEngine
        except ImportError:
            from parser_engine import ParserEngine

        return ParserEngine
