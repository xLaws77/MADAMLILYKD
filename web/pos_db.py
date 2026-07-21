"""
pos_db.py

Layer database untuk POS web. Semua operasi menggunakan orders.db
yang sama dengan bot Telegram supaya order dari POS dan Telegram
tersinkron di satu tempat.

Tabel baru:
  pos_queue  -- antrian dapur (dari POS, QR, dan Telegram)
  tables     -- manajemen meja dine-in
"""

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "orders.db"


class PosDB:
    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pos_queue (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    source      TEXT    DEFAULT 'pos',
                    table_no    TEXT    DEFAULT '',
                    customer_name TEXT  DEFAULT '',
                    items_json  TEXT    NOT NULL,
                    grand_total_riel INTEGER DEFAULT 0,
                    status      TEXT    DEFAULT 'pending',
                    note        TEXT    DEFAULT '',
                    created_at  TEXT    NOT NULL,
                    updated_at  TEXT    NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_posqueue_status "
                "ON pos_queue(status, created_at)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tables (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_no TEXT    NOT NULL UNIQUE,
                    name     TEXT    DEFAULT '',
                    capacity INTEGER DEFAULT 4,
                    active   INTEGER DEFAULT 1
                )
            """)

    # ----------------------------------------------------------
    # MENU
    # ----------------------------------------------------------
    def get_menu(self) -> List[Dict[str, Any]]:
        """Baca menu dari Excel via app.menu_loader."""
        try:
            from app.menu_loader import load_menu_from_excel, DEFAULT_MENU_PATH
        except ImportError:
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from app.menu_loader import load_menu_from_excel, DEFAULT_MENU_PATH

        try:
            return load_menu_from_excel(str(DEFAULT_MENU_PATH))
        except Exception:
            return []

    def get_categories(self) -> List[str]:
        menus = self.get_menu()
        seen = []
        for m in menus:
            cat = m.get("category", "")
            if cat and cat not in seen:
                seen.append(cat)
        return seen

    # ----------------------------------------------------------
    # POS QUEUE
    # ----------------------------------------------------------
    def _now(self) -> str:
        try:
            from app.timezone_utils import now_jakarta
            return now_jakarta().isoformat(timespec="seconds")
        except ImportError:
            from datetime import datetime
            return datetime.now().isoformat(timespec="seconds")

    def create_order(
        self,
        items: List[Dict],
        customer_name: str,
        table_no: str = "",
        source: str = "pos",
        note: str = "",
    ) -> int:
        grand_total = sum(
            int(item.get("price", 0)) * int(item.get("qty", 1))
            for item in items
        )
        now = self._now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO pos_queue
                  (source, table_no, customer_name, items_json,
                   grand_total_riel, status, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (source, table_no, customer_name,
                 json.dumps(items, ensure_ascii=False),
                 grand_total, note, now, now),
            )
            return cur.lastrowid

    def get_queue(self, statuses: Optional[List[str]] = None) -> List[Dict]:
        if statuses is None:
            statuses = ["pending", "cooking"]
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, source, table_no, customer_name,
                       items_json, grand_total_riel, status, note,
                       created_at, updated_at
                FROM pos_queue
                WHERE status IN ({placeholders})
                ORDER BY created_at ASC
                """,
                statuses,
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["items"] = json.loads(d.pop("items_json", "[]"))
            result.append(d)
        return result

    def update_status(self, order_id: int, new_status: str) -> bool:
        now = self._now()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE pos_queue SET status=?, updated_at=? WHERE id=?",
                (new_status, now, order_id),
            )
            return cur.rowcount > 0

    def get_done_orders(self, date_str: Optional[str] = None) -> List[Dict]:
        try:
            from app.timezone_utils import now_jakarta
            today = date_str or now_jakarta().strftime("%Y-%m-%d")
        except ImportError:
            from datetime import date
            today = date_str or date.today().isoformat()

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source, table_no, customer_name,
                       grand_total_riel, status, created_at
                FROM pos_queue
                WHERE DATE(created_at) = ?
                ORDER BY created_at DESC
                """,
                (today,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------
    # DASHBOARD STATS
    # ----------------------------------------------------------
    def stats_today(self, date_str: Optional[str] = None) -> Dict[str, Any]:
        try:
            from app.timezone_utils import now_jakarta
            today = date_str or now_jakarta().strftime("%Y-%m-%d")
        except ImportError:
            from datetime import date
            today = date_str or date.today().isoformat()

        with self._connect() as conn:
            # POS orders hari ini
            pos_row = conn.execute(
                """
                SELECT COUNT(*) as cnt,
                       COALESCE(SUM(grand_total_riel),0) as total
                FROM pos_queue
                WHERE DATE(created_at) = ? AND status != 'cancelled'
                """,
                (today,),
            ).fetchone()

            # Telegram orders hari ini (dari tabel orders)
            tg_row = conn.execute(
                """
                SELECT COUNT(*) as cnt,
                       COALESCE(SUM(grand_total_riel),0) as total
                FROM orders
                WHERE order_date = ?
                """,
                (today,),
            ).fetchone() if self._table_exists(conn, "orders") else None

            # Antrian aktif
            queue_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM pos_queue "
                "WHERE status IN ('pending','cooking')"
            ).fetchone()

            # Perjam (POS + Telegram)
            hourly = conn.execute(
                """
                SELECT SUBSTR(created_at, 12, 2) as hour,
                       COUNT(*) as cnt,
                       COALESCE(SUM(grand_total_riel),0) as total
                FROM pos_queue
                WHERE DATE(created_at) = ? AND status != 'cancelled'
                GROUP BY hour ORDER BY hour
                """,
                (today,),
            ).fetchall()

        pos_count = pos_row["cnt"] if pos_row else 0
        pos_total = pos_row["total"] if pos_row else 0
        tg_count = tg_row["cnt"] if tg_row else 0
        tg_total = tg_row["total"] if tg_row else 0

        return {
            "date": today,
            "pos_orders": pos_count,
            "telegram_orders": tg_count,
            "total_orders": pos_count + tg_count,
            "pos_revenue": pos_total,
            "telegram_revenue": tg_total,
            "total_revenue": pos_total + tg_total,
            "queue_active": queue_row["cnt"] if queue_row else 0,
            "hourly": [dict(r) for r in hourly],
        }

    @staticmethod
    def _table_exists(conn, table_name: str) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None

    # ----------------------------------------------------------
    # TABLES (MEJA)
    # ----------------------------------------------------------
    def get_tables(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, table_no, name, capacity, active "
                "FROM tables ORDER BY table_no"
            ).fetchall()
        return [dict(r) for r in rows]

    def add_table(self, table_no: str, name: str = "", capacity: int = 4) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO tables (table_no, name, capacity) VALUES (?,?,?)",
                    (table_no.upper().strip(), name.strip(), capacity),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_table(self, table_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM tables WHERE id=?", (table_id,))
            return cur.rowcount > 0

    def toggle_table(self, table_id: int) -> bool:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tables SET active = CASE WHEN active=1 THEN 0 ELSE 1 END "
                "WHERE id=?",
                (table_id,),
            )
        return True
