"""
order_store.py

Penyimpanan order ke SQLite (data/orders.db) supaya:
1. Order terakhir per chat TIDAK hilang saat bot restart/redeploy --
   /struk, /invoice, /tambah, /hapus, /ganti tetap jalan untuk order
   yang dikirim sebelum restart.
2. Koreksi hasil belajar dari staf tersimpan permanen: tulisan yang
   pernah dibetulkan tidak salah baca lagi walau bot restart.

Order disimpan satu baris per order: kolom agregat (tanggal, total,
lokasi) plus payload JSON berisi seluruh isi OrderSummary untuk
dibangun ulang persis seperti aslinya.

CATATAN deployment: di hosting dengan filesystem ephemeral (mis. Render
free tier) file .db ikut hilang saat REDEPLOY -- tapi tetap selamat dari
restart proses biasa, dan di PC/VPS biasa permanen sepenuhnya.
"""

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from .models import Invoice, OrderItem
    from .receipt_data import OrderSummary
    from .timezone_utils import now_jakarta
except ImportError:
    from models import Invoice, OrderItem
    from receipt_data import OrderSummary
    from timezone_utils import now_jakarta


DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "orders.db"


class OrderStore:
    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    order_date TEXT NOT NULL,
                    destination TEXT DEFAULT '',
                    orderer_name TEXT DEFAULT '',
                    grand_total_riel INTEGER DEFAULT 0,
                    grand_total_usd REAL DEFAULT 0,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_chat ON orders(chat_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date)"
            )

            # Koreksi hasil belajar: staf membetulkan hasil salah baca
            # bot (lewat /ganti), bot mengingat "tulisan -> menu benar"
            # supaya tulisan yang sama tidak salah lagi ke depannya.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS corrections (
                    pattern TEXT PRIMARY KEY,
                    menu_name TEXT NOT NULL,
                    hits INTEGER DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
                """
            )

            # Pre-order: order yang dijadwalkan untuk hari berikutnya.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS preorders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    order_text TEXT NOT NULL,
                    scheduled_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    processed_at TEXT DEFAULT ''
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_preorders_date "
                "ON preorders(scheduled_date, status)"
            )

            # Ketersediaan menu per hari (ready/tidak ready).
            # is_ready = 1 (siap), 0 (tidak siap).
            # Menu yang tidak ada di tabel ini dianggap tersedia (default ready).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS menu_availability (
                    menu_name TEXT NOT NULL,
                    date TEXT NOT NULL,
                    is_ready INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (menu_name, date)
                )
                """
            )

            # Diskon per menu (nominal Riel atau persen).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    menu_name TEXT UNIQUE NOT NULL,
                    discount_type TEXT NOT NULL,
                    discount_value REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

            # Daftar meja fisik untuk dine-in.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dine_in_tables (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_no TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

            # Sesi dine-in aktif per meja.
            # Satu meja hanya boleh punya satu sesi 'active' dalam satu waktu.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dine_in_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_no TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    grand_total_riel INTEGER DEFAULT 0,
                    started_at TEXT NOT NULL,
                    paid_at TEXT DEFAULT '',
                    status TEXT DEFAULT 'active'
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dinein_active "
                "ON dine_in_sessions(table_no, status)"
            )

            # Meja permanen: IN1-IN4 (dine-in) dan OUT1-OUT4 (take-out).
            # INSERT OR IGNORE: tidak dobel kalau sudah ada.
            _default_tables = [
                "IN1", "IN2", "IN3", "IN4",
                "OUT1", "OUT2", "OUT3", "OUT4",
            ]
            _ts = now_jakarta().isoformat(timespec="seconds")
            for _t in _default_tables:
                conn.execute(
                    "INSERT OR IGNORE INTO dine_in_tables (table_no, created_at) VALUES (?, ?)",
                    (_t, _ts),
                )

    # ------------------------------------------------------------
    # Serialisasi
    # ------------------------------------------------------------
    @staticmethod
    def _summary_to_json(summary: OrderSummary) -> str:
        return json.dumps(asdict(summary), ensure_ascii=False)

    @staticmethod
    def _summary_from_json(payload: str) -> OrderSummary:
        data = json.loads(payload)

        invoices = []

        for inv in data.get("invoices", []):
            items = [OrderItem(**item) for item in inv.pop("items", [])]
            # Ensure string fields are never None (could be null in JSON)
            for key in ("telegram_name", "destination", "delivery_type", "invoice_no"):
                if inv.get(key) is None:
                    inv[key] = ""
            invoices.append(Invoice(items=items, **inv))

        return OrderSummary(
            invoices=invoices,
            grand_total_riel=data.get("grand_total_riel", 0),
            grand_total_usd=data.get("grand_total_usd", 0.0),
            destination=data.get("destination", ""),
            orderer_name=data.get("orderer_name", ""),
        )

    # ------------------------------------------------------------
    # Simpan / update / muat
    # ------------------------------------------------------------
    def save_order(self, chat_id: str, summary: OrderSummary) -> int:
        now = now_jakarta()

        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO orders (
                    chat_id, created_at, order_date, destination,
                    orderer_name, grand_total_riel, grand_total_usd, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(chat_id),
                    now.isoformat(timespec="seconds"),
                    now.strftime("%Y-%m-%d"),
                    summary.destination,
                    summary.orderer_name,
                    summary.grand_total_riel,
                    summary.grand_total_usd,
                    self._summary_to_json(summary),
                ),
            )
            return cursor.lastrowid

    def update_order(self, order_id: int, summary: OrderSummary) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE orders SET
                    destination = ?,
                    orderer_name = ?,
                    grand_total_riel = ?,
                    grand_total_usd = ?,
                    payload = ?
                WHERE id = ?
                """,
                (
                    summary.destination,
                    summary.orderer_name,
                    summary.grand_total_riel,
                    summary.grand_total_usd,
                    self._summary_to_json(summary),
                    order_id,
                ),
            )

    def load_last_order(self, chat_id: str) -> Optional[Tuple[int, OrderSummary]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, payload FROM orders WHERE chat_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (str(chat_id),),
            ).fetchone()

        if row is None:
            return None

        return row["id"], self._summary_from_json(row["payload"])

    # ------------------------------------------------------------
    # Koreksi hasil belajar
    # ------------------------------------------------------------
    def load_corrections(self) -> Dict[str, str]:
        """Muat semua koreksi: {pattern -> menu_name}."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT pattern, menu_name FROM corrections"
            ).fetchall()

        return {row["pattern"]: row["menu_name"] for row in rows}

    def orders_by_date(self, date_str: str) -> List[dict]:
        """Ambil semua order untuk tanggal tertentu (YYYY-MM-DD)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, chat_id, created_at, order_date, destination,
                       orderer_name, grand_total_riel, grand_total_usd, payload
                FROM orders WHERE order_date = ?
                ORDER BY id ASC
                """,
                (date_str,),
            ).fetchall()
        return [dict(row) for row in rows]

    def daily_summary(self, date_str: str) -> dict:
        """Ringkasan total order & pendapatan untuk satu hari."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) as order_count,
                       COALESCE(SUM(grand_total_riel), 0) as total_riel,
                       COALESCE(SUM(grand_total_usd), 0.0) as total_usd
                FROM orders WHERE order_date = ?
                """,
                (date_str,),
            ).fetchone()
        return dict(row) if row else {"order_count": 0, "total_riel": 0, "total_usd": 0.0}

    # ------------------------------------------------------------
    # Pre-order
    # ------------------------------------------------------------
    def save_preorder(self, chat_id: str, order_text: str, scheduled_date: str) -> int:
        """Simpan pre-order untuk tanggal tertentu. Return ID baris baru."""
        now = now_jakarta().isoformat(timespec="seconds")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO preorders (chat_id, order_text, scheduled_date, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(chat_id), order_text, scheduled_date, now),
            )
            return cursor.lastrowid

    def pending_preorders(self, date_str: str) -> List[dict]:
        """Ambil semua pre-order yang belum diproses untuk tanggal tertentu."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, chat_id, order_text, created_at
                FROM preorders
                WHERE scheduled_date = ? AND status = 'pending'
                ORDER BY id ASC
                """,
                (date_str,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upcoming_preorders(self, from_date: str) -> List[dict]:
        """Ambil pre-order pending dari tanggal tertentu ke depan."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, chat_id, order_text, scheduled_date, created_at
                FROM preorders
                WHERE scheduled_date >= ? AND status = 'pending'
                ORDER BY scheduled_date ASC, id ASC
                """,
                (from_date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_preorder_done(self, preorder_id: int) -> None:
        now = now_jakarta().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                "UPDATE preorders SET status = 'processed', processed_at = ? WHERE id = ?",
                (now, preorder_id),
            )

    def cancel_preorder(self, preorder_id: int, chat_id: str) -> bool:
        """Batalkan pre-order. Return True kalau berhasil ditemukan & dibatalkan."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE preorders SET status = 'cancelled' "
                "WHERE id = ? AND chat_id = ? AND status = 'pending'",
                (preorder_id, str(chat_id)),
            )
            return cursor.rowcount > 0

    def save_correction(self, pattern: str, menu_name: str) -> None:
        """Simpan/perbarui satu koreksi. Kalau pattern sudah ada,
        menu_name di-update ke yang terbaru dan hits ditambah."""
        now = now_jakarta().isoformat(timespec="seconds")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO corrections (pattern, menu_name, hits, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(pattern) DO UPDATE SET
                    menu_name = excluded.menu_name,
                    hits = corrections.hits + 1,
                    updated_at = excluded.updated_at
                """,
                (pattern, menu_name, now),
            )

    # ------------------------------------------------------------
    # Diskon per menu
    # ------------------------------------------------------------
    def set_discount(self, menu_name: str, discount_type: str, value: float) -> None:
        """Simpan/update diskon untuk menu (discount_type: 'persen' atau 'nominal')."""
        now = now_jakarta().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO discounts (menu_name, discount_type, discount_value, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(menu_name) DO UPDATE SET
                    discount_type = excluded.discount_type,
                    discount_value = excluded.discount_value,
                    created_at = excluded.created_at
                """,
                (menu_name.upper(), discount_type, value, now),
            )

    def remove_discount(self, menu_name: str) -> bool:
        """Hapus diskon untuk menu. Return True kalau ada yang dihapus."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM discounts WHERE menu_name = ?",
                (menu_name.upper(),),
            )
            return cursor.rowcount > 0

    def get_all_discounts(self) -> list:
        """Ambil semua diskon aktif."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT menu_name, discount_type, discount_value, created_at "
                "FROM discounts ORDER BY menu_name ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_discount_map(self) -> dict:
        """Return {MENU_NAME_UPPER: {"type": str, "value": float}}."""
        rows = self.get_all_discounts()
        return {r["menu_name"]: {"type": r["discount_type"], "value": r["discount_value"]} for r in rows}

    # ------------------------------------------------------------
    # Menu availability (ready / tidak ready hari ini)
    # ------------------------------------------------------------
    def set_menu_ready(self, menu_name: str, is_ready: bool, date_str: str) -> None:
        """Tandai menu siap atau tidak siap untuk tanggal tertentu."""
        now = now_jakarta().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO menu_availability (menu_name, date, is_ready, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(menu_name, date) DO UPDATE SET
                    is_ready = excluded.is_ready,
                    updated_at = excluded.updated_at
                """,
                (menu_name.strip().upper(), date_str, 1 if is_ready else 0, now),
            )

    def get_menu_availability(self, date_str: str) -> Dict[str, bool]:
        """Kembalikan dict {nama_menu_upper -> is_ready} untuk tanggal ini.
        Menu yang tidak terdaftar dianggap READY (default)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT menu_name, is_ready FROM menu_availability WHERE date = ?",
                (date_str,),
            ).fetchall()
        return {row["menu_name"]: bool(row["is_ready"]) for row in rows}

    def reset_menu_availability(self, date_str: str) -> int:
        """Hapus semua data ketersediaan menu untuk tanggal ini (reset ke ready semua).
        Return jumlah baris yang dihapus."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM menu_availability WHERE date = ?",
                (date_str,),
            )
            return cursor.rowcount

    # ------------------------------------------------------------
    # Dine-in: meja
    # ------------------------------------------------------------
    def create_dine_in_table(self, table_no: str) -> bool:
        """Buat meja baru. Return True kalau berhasil, False kalau sudah ada."""
        now = now_jakarta().isoformat(timespec="seconds")
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO dine_in_tables (table_no, created_at) VALUES (?, ?)",
                (table_no.upper().strip(), now),
            )
            return cursor.rowcount > 0

    def delete_dine_in_table(self, table_no: str) -> Tuple[bool, str]:
        """Hapus meja. Return (True, '') kalau berhasil, (False, alasan) kalau gagal."""
        table_no = table_no.upper().strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM dine_in_sessions WHERE table_no = ? AND status = 'active'",
                (table_no,),
            ).fetchone()
            if row:
                return False, "Meja masih ada sesi aktif. Selesaikan dulu dengan /bayar."
            cursor = conn.execute(
                "DELETE FROM dine_in_tables WHERE table_no = ?",
                (table_no,),
            )
            if cursor.rowcount == 0:
                return False, f"Meja {table_no} tidak ditemukan."
            return True, ""

    def get_dine_in_tables(self) -> List[dict]:
        """Ambil semua meja beserta status aktif/tidaknya."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT table_no, created_at FROM dine_in_tables ORDER BY table_no ASC"
            ).fetchall()
            active_rows = conn.execute(
                "SELECT table_no FROM dine_in_sessions WHERE status = 'active'"
            ).fetchall()
        active_set = {r["table_no"] for r in active_rows}
        return [
            {
                "table_no": r["table_no"],
                "created_at": r["created_at"],
                "active": r["table_no"] in active_set,
            }
            for r in rows
        ]

    # ------------------------------------------------------------
    # Dine-in: sesi order per meja
    # ------------------------------------------------------------
    def start_dine_in_session(self, table_no: str, summary: "OrderSummary") -> int:
        """Buat sesi dine-in baru untuk meja. Return session_id."""
        now = now_jakarta().isoformat(timespec="seconds")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO dine_in_sessions (table_no, payload, grand_total_riel, started_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    table_no.upper().strip(),
                    self._summary_to_json(summary),
                    summary.grand_total_riel,
                    now,
                ),
            )
            return cursor.lastrowid

    def get_active_dine_in_session(self, table_no: str) -> Optional[dict]:
        """Ambil sesi aktif untuk meja, atau None kalau tidak ada."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, payload, grand_total_riel, started_at "
                "FROM dine_in_sessions "
                "WHERE table_no = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                (table_no.upper().strip(),),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "grand_total_riel": row["grand_total_riel"],
            "started_at": row["started_at"],
            "summary": self._summary_from_json(row["payload"]),
        }

    def update_dine_in_session(self, session_id: int, summary: "OrderSummary") -> None:
        """Perbarui payload sesi dine-in setelah ada order baru."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE dine_in_sessions SET payload = ?, grand_total_riel = ? WHERE id = ?",
                (self._summary_to_json(summary), summary.grand_total_riel, session_id),
            )

    def close_dine_in_session(self, session_id: int) -> None:
        """Tutup sesi dine-in (sudah bayar)."""
        now = now_jakarta().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                "UPDATE dine_in_sessions SET status = 'paid', paid_at = ? WHERE id = ?",
                (now, session_id),
            )
