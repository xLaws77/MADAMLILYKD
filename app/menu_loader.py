"""
menu_loader.py

Load daftar menu dari database (SQLite) via MenuStore.
Database sekarang adalah sumber utama menu, bukan Excel file.

Format output untuk MatchingEngine & ParserEngine.create_item():

    {
        "name": "CHICKEN KATSU+RICE",
        "price": 12000,
        "category": "HOKI-HOKI BENTO",
        "resto": "MADAM LILY",
        "aliases": ["KATSU RICE", "KATSU AYAM"],
        "note": "",
        "emoji": "🍱",
    }

Menu dapat diubah via Telegram command (/tambah, /hapus, etc) tanpa
perlu restart bot. Database ter-persist dan tidak hilang saat redeploy.
"""

from pathlib import Path
from typing import Any, Dict, List


DEFAULT_MENU_PATH = Path(__file__).parent.parent / "data" / "menu.xlsx"


def _split_aliases(raw) -> List[str]:
    if not raw:
        return []
    return [a.strip() for a in str(raw).split(",") if a.strip()]


def load_menu_from_excel(path: str = None) -> List[Dict[str, Any]]:
    """Load menu dari database (MenuStore).

    Parameter 'path' diabaikan (hanya untuk backward compatibility).
    Semua menu sekarang dari SQLite database.

    Import MenuStore di sini (bukan top-level) untuk avoid circular dependency.
    """
    try:
        from .menu_store import MenuStore
    except ImportError:
        from menu_store import MenuStore

    store = MenuStore()
    db_menus = store.get_all_menus()

    result = []
    for menu in db_menus:
        result.append(
            {
                "name": menu["name"],
                "price": menu["price"],
                "category": menu.get("category", ""),
                "resto": menu.get("resto", ""),
                "aliases": _split_aliases(menu.get("alias", "")),
                "note": menu.get("note", ""),
                "emoji": menu.get("emoji", ""),
            }
        )

    return result


if __name__ == "__main__":
    menus = load_menu_from_excel()
    print(f"Berhasil load {len(menus)} menu dari database")
    if menus:
        print(menus[0])
