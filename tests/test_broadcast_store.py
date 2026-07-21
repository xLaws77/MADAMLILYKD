"""Unit test BroadcastStore.

Jalankan: python3 -m unittest tests.test_broadcast_store
"""

import os
import tempfile
import unittest

from app.broadcast_store import BroadcastStore


class TestBroadcastStore(unittest.TestCase):

    def setUp(self):
        # DB sementara per test -- isolated, tidak menyentuh data/orders.db
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = BroadcastStore(db_path=self.db_path)

    def tearDown(self):
        os.remove(self.db_path)

    # ---------- add ----------
    def test_add_group_baru(self):
        added = self.store.add_group("-100123", title="Grup A", added_by="42")
        self.assertTrue(added)
        self.assertEqual(self.store.count(), 1)

    def test_add_group_duplikat_return_false(self):
        self.store.add_group("-100123", title="Grup A")
        added = self.store.add_group("-100123", title="Grup A (updated)")
        self.assertFalse(added)
        self.assertEqual(self.store.count(), 1)

    def test_add_group_duplikat_update_title(self):
        self.store.add_group("-100123", title="Judul Lama")
        self.store.add_group("-100123", title="Judul Baru")
        groups = self.store.list_groups()
        self.assertEqual(groups[0]["title"], "Judul Baru")

    def test_add_group_chat_id_selalu_string(self):
        # int juga boleh masuk, disimpan sebagai string
        self.store.add_group(-100999, title="Grup INT")
        self.assertTrue(self.store.has_group("-100999"))
        self.assertTrue(self.store.has_group(-100999))

    # ---------- remove ----------
    def test_remove_group_ada(self):
        self.store.add_group("-100123")
        self.assertTrue(self.store.remove_group("-100123"))
        self.assertFalse(self.store.has_group("-100123"))
        self.assertEqual(self.store.count(), 0)

    def test_remove_group_tidak_ada(self):
        self.assertFalse(self.store.remove_group("-100999"))

    # ---------- list ----------
    def test_list_kosong(self):
        self.assertEqual(self.store.list_groups(), [])

    def test_list_urutan_terbaru_di_atas(self):
        # added_at bersifat ISO string; urutan DESC = kirim urutan
        # penambahan terbalik (yang terakhir ditambah muncul duluan)
        self.store.add_group("-100111", title="A")
        # Beda detik supaya urutan DESC stabil
        import time
        time.sleep(1.1)
        self.store.add_group("-100222", title="B")

        groups = self.store.list_groups()
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["chat_id"], "-100222")
        self.assertEqual(groups[1]["chat_id"], "-100111")

    def test_list_field_lengkap(self):
        self.store.add_group("-100123", title="Grup X", added_by="42")
        g = self.store.list_groups()[0]
        self.assertEqual(g["chat_id"], "-100123")
        self.assertEqual(g["title"], "Grup X")
        self.assertEqual(g["added_by"], "42")
        self.assertTrue(g["added_at"])

    # ---------- has_group ----------
    def test_has_group_true_false(self):
        self.store.add_group("-100777")
        self.assertTrue(self.store.has_group("-100777"))
        self.assertFalse(self.store.has_group("-100999"))


if __name__ == "__main__":
    unittest.main()
