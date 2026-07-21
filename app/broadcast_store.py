"""
broadcast_store.py

Daftar grup Telegram yang menerima broadcast (menu harian, pengumuman,
promo). Grup didaftarkan admin lewat command /daftargrup di grupnya
sendiri, disimpan permanen di data/orders.db bersebelahan dengan tabel
order & menu (satu database, satu file backup).
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional


DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "orders.db"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class BroadcastStore:
    """CRUD grup broadcast. Skema minimal: chat_id (unik), judul, siapa
    yang mendaftarkan, kapan didaftarkan."""

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
                CREATE TABLE IF NOT EXISTS broadcast_groups (
                    chat_id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '',
                    added_by TEXT DEFAULT '',
                    added_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    # ------------------------------------------------------------------
    def add_group(
        self,
        chat_id: str,
        title: str = "",
        added_by: str = "",
    ) -> bool:
        """Tambah grup. Return True kalau baru, False kalau sudah ada."""

        chat_id = str(chat_id)

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT chat_id FROM broadcast_groups WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()

            if existing:
                # Judul mungkin berubah -- update biar daftar tetap akurat
                conn.execute(
                    "UPDATE broadcast_groups SET title = ? WHERE chat_id = ?",
                    (title or "", chat_id),
                )
                conn.commit()
                return False

            conn.execute(
                """
                INSERT INTO broadcast_groups (chat_id, title, added_by, added_at)
                VALUES (?, ?, ?, ?)
                """,
                (chat_id, title or "", added_by or "", _now_iso()),
            )
            conn.commit()
            return True

    # ------------------------------------------------------------------
    def remove_group(self, chat_id: str) -> bool:
        """Hapus grup. Return True kalau ada yang terhapus."""

        chat_id = str(chat_id)

        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM broadcast_groups WHERE chat_id = ?",
                (chat_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    def has_group(self, chat_id: str) -> bool:
        chat_id = str(chat_id)

        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM broadcast_groups WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()

        return row is not None

    # ------------------------------------------------------------------
    def list_groups(self) -> List[dict]:
        """List semua grup terdaftar, terurut dari yang paling baru."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_id, title, added_by, added_at
                FROM broadcast_groups
                ORDER BY added_at DESC
                """
            ).fetchall()

        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM broadcast_groups"
            ).fetchone()

        return int(row["c"] or 0)
