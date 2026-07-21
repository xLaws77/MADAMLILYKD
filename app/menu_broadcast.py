"""
menu_broadcast.py

Format dan kirim broadcast menu harian ke grup-grup Telegram terdaftar.
Menu yang "ready" dan "tidak ready" bisa diatur via bot commands
(/readymenu, /notready) supaya pelanggan tahu persis apa yang tersedia
hari ini sebelum order.
"""

from typing import Any, Dict, List, Optional


def format_menu_broadcast(
    menus: List[Dict[str, Any]],
    availability: Dict[str, bool],
    date_label: str,
    shop_name: str = "MADAM LILY",
    bot_username: str = "",
) -> str:
    """
    Bangun teks broadcast menu harian.

    menus        : list menu dari MatchingEngine (name, price, emoji, category)
    availability : {NAMA_MENU_UPPER -> is_ready} dari OrderStore
                   Menu yang TIDAK ada di dict ini dianggap ready (default).
    date_label   : string tanggal untuk header (mis. "Sabtu, 12 Juli 2026")
    """
    ready: List[Dict] = []
    not_ready: List[str] = []

    # Deduplikasi nama menu supaya tidak muncul dua kali kalau ada duplikat di xlsx
    seen: set = set()

    for menu in menus:
        name = menu.get("name", "").strip()
        name_upper = name.upper()

        if not name or name_upper in seen:
            continue

        seen.add(name_upper)

        # Default: ready kalau tidak ada di tabel availability
        is_ready = availability.get(name_upper, True)

        if is_ready:
            ready.append(menu)
        else:
            not_ready.append(name)

    lines = [
        f"🍽️ MENU HARI INI - {shop_name}",
        f"📅 {date_label}",
        "",
    ]

    if ready:
        lines.append("✅ TERSEDIA:")
        for m in ready:
            emoji = m.get("emoji", "")
            prefix = f"{emoji} " if emoji else "• "
            price_str = f"{m['price']:,.0f}".replace(",", ".")
            lines.append(f"{prefix}{m['name']} : {price_str} Riel")
    else:
        lines.append("⚠️ Belum ada menu yang ditandai tersedia hari ini.")

    if not_ready:
        lines.append("")
        lines.append("❌ TIDAK TERSEDIA HARI INI:")
        for name in not_ready:
            lines.append(f"- {name}")

    if bot_username:
        lines += ["", f"Order: kirim langsung ke @{bot_username}"]

    return "\n".join(lines)


def format_availability_status(
    menus: List[Dict[str, Any]],
    availability: Dict[str, bool],
    date_label: str,
) -> str:
    """Tampilkan status ready/tidak untuk semua menu (untuk command /lihatready)."""
    ready_names: List[str] = []
    not_ready_names: List[str] = []
    seen: set = set()

    for menu in menus:
        name = menu.get("name", "").strip()
        name_upper = name.upper()

        if not name or name_upper in seen:
            continue

        seen.add(name_upper)
        is_ready = availability.get(name_upper, True)

        if is_ready:
            ready_names.append(name)
        else:
            not_ready_names.append(name)

    lines = [f"📋 STATUS MENU — {date_label}", ""]

    if ready_names:
        lines.append(f"✅ TERSEDIA ({len(ready_names)} menu):")
        for name in ready_names:
            lines.append(f"  • {name}")
    else:
        lines.append("✅ TERSEDIA: (belum ada)")

    if not_ready_names:
        lines.append("")
        lines.append(f"❌ TIDAK TERSEDIA ({len(not_ready_names)} menu):")
        for name in not_ready_names:
            lines.append(f"  • {name}")

    if not not_ready_names:
        lines.append("")
        lines.append("Semua menu ditandai tersedia hari ini.")

    lines += [
        "",
        "Gunakan /readymenu NAMA untuk tandai siap,",
        "/notready NAMA untuk tandai tidak siap,",
        "/resetready untuk reset semua ke siap.",
    ]

    return "\n".join(lines)
