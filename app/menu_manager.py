"""
menu_manager.py

CRUD operasi pada database (data/orders.db) untuk manajemen menu lewat Telegram.
Menu sekarang disimpan di SQLite, bukan Excel, supaya tidak hilang saat redeploy
di Render (yang punya ephemeral file system).

Admin bisa tambah, hapus, dan update menu tanpa perlu restart bot.
"""

from typing import Any, Dict, List, Optional, Tuple

try:
    from .menu_store import MenuStore
except ImportError:
    from menu_store import MenuStore


class MenuManager:
    def __init__(self):
        self.store = MenuStore()

    def find_menu(self, name: str) -> Optional[Dict[str, Any]]:
        return self.store.find_menu(name)

    def add_menu(
        self,
        name: str,
        price: int,
        category: str = "",
        resto: str = "",
        emoji: str = "",
        alias: str = "",
        note: str = "",
    ) -> Tuple[bool, str]:
        return self.store.add_menu(
            name=name,
            price=price,
            category=category,
            resto=resto,
            emoji=emoji,
            alias=alias,
            note=note,
        )

    def remove_menu(self, name: str) -> Tuple[bool, str]:
        return self.store.remove_menu(name)

    def update_price(self, name: str, new_price: int) -> Tuple[bool, str]:
        return self.store.update_price(name, new_price)

    def list_categories(self) -> List[Tuple[str, int]]:
        return self.store.list_categories()

    def list_menus_by_category(self, category: str) -> List[Dict[str, Any]]:
        return self.store.list_menus_by_category(category)

    def search_menus(self, keyword: str) -> List[Dict[str, Any]]:
        return self.store.search_menus(keyword)
