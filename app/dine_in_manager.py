"""
dine_in_manager.py

Manajemen sesi dine-in per meja: lacak meja mana yang sedang aktif,
sesi order yang sedang berjalan, dan chat mana yang sedang bertugas
melayani meja tertentu.
"""

from typing import Dict, List, Optional, Tuple

try:
    from .bill import BillGenerator
    from .receipt_data import OrderSummary
    from .order_store import OrderStore
except ImportError:
    from bill import BillGenerator
    from receipt_data import OrderSummary
    from order_store import OrderStore

_bill_gen = BillGenerator()


class DineInManager:
    """
    Koordinasi meja dine-in + sesi order.

    Mapping chat->meja disimpan in-memory supaya tidak "nyangkut" kalau
    staf berganti shift atau bot restart. Meja (tabel dine_in_tables) dan
    sesi order (dine_in_sessions) disimpan permanen ke DB lewat OrderStore.
    """

    def __init__(self, store: OrderStore):
        self._store = store
        # chat_id -> table_no yang sedang dilayani chat ini
        self._chat_to_table: Dict[str, str] = {}

    # ----------------------------------------------------------------
    # Manajemen meja (CRUD)
    # ----------------------------------------------------------------

    def create_table(self, table_no: str) -> Tuple[bool, str]:
        table_no = table_no.upper().strip()
        if not table_no:
            return False, "Nomor meja tidak boleh kosong."
        ok = self._store.create_dine_in_table(table_no)
        if ok:
            return True, f"✅ Meja {table_no} berhasil dibuat."
        return False, f"Meja {table_no} sudah ada."

    def delete_table(self, table_no: str) -> Tuple[bool, str]:
        table_no = table_no.upper().strip()
        ok, reason = self._store.delete_dine_in_table(table_no)
        if ok:
            for c in [c for c, t in self._chat_to_table.items() if t == table_no]:
                del self._chat_to_table[c]
            return True, f"✅ Meja {table_no} berhasil dihapus."
        return False, reason

    def list_tables(self) -> List[dict]:
        return self._store.get_dine_in_tables()

    # ----------------------------------------------------------------
    # Meja aktif per chat (sticky mode)
    # ----------------------------------------------------------------

    def set_active_table(self, chat_id: str, table_no: str) -> Tuple[bool, str]:
        """Set meja aktif untuk chat ini. Return (ok, table_no_or_error)."""
        table_no = table_no.upper().strip()
        tables = {t["table_no"] for t in self._store.get_dine_in_tables()}
        if table_no not in tables:
            return False, (
                f"Meja {table_no} tidak ditemukan.\n"
                "Gunakan /daftarmeja untuk melihat daftar meja."
            )
        self._chat_to_table[str(chat_id)] = table_no
        return True, table_no

    def get_active_table(self, chat_id: str) -> Optional[str]:
        return self._chat_to_table.get(str(chat_id))

    def clear_active_table(self, chat_id: str) -> None:
        self._chat_to_table.pop(str(chat_id), None)

    # ----------------------------------------------------------------
    # Sesi order per meja
    # ----------------------------------------------------------------

    def get_or_create_session(self, table_no: str) -> Tuple[int, OrderSummary]:
        """Ambil sesi aktif meja, atau buat sesi baru kalau belum ada."""
        table_no = table_no.upper().strip()
        row = self._store.get_active_dine_in_session(table_no)
        if row:
            return row["id"], row["summary"]
        empty = OrderSummary(invoices=[], grand_total_riel=0, grand_total_usd=0.0)
        session_id = self._store.start_dine_in_session(table_no, empty)
        return session_id, empty

    def get_session(self, table_no: str) -> Optional[Tuple[int, OrderSummary]]:
        """Ambil sesi aktif meja, atau None kalau tidak ada."""
        table_no = table_no.upper().strip()
        row = self._store.get_active_dine_in_session(table_no)
        if row is None:
            return None
        return row["id"], row["summary"]

    def update_session(self, session_id: int, summary: OrderSummary) -> None:
        self._store.update_dine_in_session(session_id, summary)

    def close_session(self, table_no: str) -> Optional[OrderSummary]:
        """Tutup sesi aktif meja (sudah bayar). Return summary terakhir, atau None."""
        table_no = table_no.upper().strip()
        row = self._store.get_active_dine_in_session(table_no)
        if row is None:
            return None
        self._store.close_dine_in_session(row["id"])
        for c in [c for c, t in self._chat_to_table.items() if t == table_no]:
            del self._chat_to_table[c]
        return row["summary"]

    # ----------------------------------------------------------------
    # Manajemen item dalam sesi
    # ----------------------------------------------------------------

    def get_flat_items(self, table_no: str) -> List[dict]:
        """Kembalikan semua item dari sesi aktif sebagai flat list.

        Setiap entry: {flat_idx, inv_idx, item_idx, name, qty, price, customer}
        flat_idx dipakai sebagai ID unik untuk callback data hapus-item.
        """
        result = self.get_session(table_no)
        if result is None:
            return []
        _, summary = result
        flat: List[dict] = []
        for inv_idx, inv in enumerate(summary.invoices):
            for item_idx, item in enumerate(inv.items):
                flat.append({
                    "flat_idx": len(flat),
                    "inv_idx": inv_idx,
                    "item_idx": item_idx,
                    "name": item.menu,
                    "qty": item.qty,
                    "price": item.price,
                    "customer": inv.telegram_name or "",
                })
        return flat

    def remove_item_from_session(
        self, table_no: str, flat_idx: int
    ) -> Tuple[bool, Optional[OrderSummary], str]:
        """Hapus satu baris item (berdasarkan flat_idx) dari sesi aktif meja.

        Return (ok, updated_summary_or_None, pesan_error).
        """
        table_no = table_no.upper().strip()
        row = self._store.get_active_dine_in_session(table_no)
        if row is None:
            return False, None, "Tidak ada sesi aktif di meja ini."

        session_id = row["id"]
        summary = row["summary"]

        # Bangun peta flat_idx → (inv_idx, item_idx)
        flat_map: List[Tuple[int, int]] = []
        for inv_idx, inv in enumerate(summary.invoices):
            for item_idx in range(len(inv.items)):
                flat_map.append((inv_idx, item_idx))

        if flat_idx >= len(flat_map):
            return False, None, "Item tidak ditemukan (indeks tidak valid)."

        inv_idx, item_idx = flat_map[flat_idx]

        # Hapus item dari invoice
        invoice = summary.invoices[inv_idx]
        removed_item = invoice.items[item_idx]
        invoice.items = [
            item for i, item in enumerate(invoice.items) if i != item_idx
        ]

        # Recalculate total invoice
        if invoice.items:
            _bill_gen.calculate(invoice)
            summary.invoices[inv_idx] = invoice
        else:
            # Invoice kosong → hapus seluruh invoice
            summary.invoices = [
                inv for i, inv in enumerate(summary.invoices) if i != inv_idx
            ]

        # Recalculate grand total
        summary.grand_total_riel = sum(inv.total_riel for inv in summary.invoices)
        summary.grand_total_usd = round(
            sum(inv.total_usd for inv in summary.invoices), 2
        )

        self._store.update_dine_in_session(session_id, summary)
        return True, summary, removed_item.menu
