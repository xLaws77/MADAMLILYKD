"""
Madam Lily Telegram Bot
Production Version 1.2

PERUBAHAN dari versi 1.1:
- Tambah /struk -> kirim struk thermal (PNG) untuk order terakhir di chat ini
- Tambah /invoice -> kirim invoice PDF (A4) untuk order terakhir di chat ini
- /help diperbarui otomatis lewat adapter.handle_command("/help")
"""

import asyncio
import os
import re
import socket
import sys
import threading
from datetime import datetime, time as dt_time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.constants import (
    APP_NAME,
    BOT_VERSION,
)

from app.logger import (
    startup,
    info,
    warning,
    error,
)

from app.broadcast_store import BroadcastStore
from app.telegram_adapter import TelegramAdapter
from app.invoice_image import render_a4_invoice_png_bytes
from app.receipt_pdf import render_a4_invoice_bytes
from app.keepalive import set_order_store
from web.pos_app import start_pos_server
from app.timezone_utils import JAKARTA_TZ, now_jakarta
from app.menu_manager import MenuManager
from app.menu_broadcast import format_availability_status
from app.dine_in_manager import DineInManager
from app.receipt_data import OrderSummary

# ==========================================================
# UTF-8
# ==========================================================

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ==========================================================
# ENV
# ==========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN tidak ditemukan pada file .env")

# Chat ID tempat pesanan resto lain (mis. Madam Lily) diteruskan
# otomatis. Kosongkan/hapus env var ini kalau belum ada chat ID-nya --
# blok untuk resto lain tetap muncul di balasan sebagai fallback
# manual (copy-paste sendiri) selama env var ini belum diisi.
FOREIGN_RESTO_CHAT_ID = os.getenv("FOREIGN_RESTO_CHAT_ID")

# Chat ID admin/pemilik. Kalau diisi: database order (data/orders.db)
# dikirim otomatis ke chat ini tiap malam sebagai backup (penting di
# hosting yang disk-nya ephemeral seperti Render free tier, karena file
# .db ikut hilang saat redeploy), dan command /backup bisa dipakai dari
# chat itu untuk minta backup kapan saja.
#
# Boleh diisi lebih dari satu chat ID, dipisah koma (mis.
# "123456,995518976") -- semua admin di list punya hak sama & sama-sama
# terima backup harian. Spasi di sekitar koma diabaikan.
ADMIN_CHAT_IDS = {
    x.strip() for x in (os.getenv("ADMIN_CHAT_ID") or "").split(",") if x.strip()
}

ORDERS_DB_PATH = Path("data/orders.db")


# ==========================================================
# ADAPTER
# ==========================================================

adapter = TelegramAdapter()
menu_mgr = MenuManager()
dinein_mgr = DineInManager(adapter.store) if adapter.store is not None else None
broadcast_store = BroadcastStore()

# chat_id -> table_no: menunggu teks order setelah user klik tombol meja
_pending_table: dict = {}


# ==========================================================
# HELPER
# ==========================================================

def _chat_id(update: Update) -> str:
    if update.effective_chat:
        return str(update.effective_chat.id)
    return ""


def _order_keyboard() -> InlineKeyboardMarkup:
    """Tombol interaktif yang muncul setelah order diproses."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 Invoice", callback_data="order_invoice"),
            InlineKeyboardButton("🤖 Cek AI", callback_data="order_cekai"),
        ],
        [
            InlineKeyboardButton("➕ Tambah", callback_data="order_tambah"),
            InlineKeyboardButton("🗑️ Hapus", callback_data="order_hapus"),
            InlineKeyboardButton("🔄 Ganti", callback_data="order_ganti"),
        ],
    ])


# ==========================================================
# COMMANDS
# ==========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "👋 Selamat datang di Madam Lily Order Bot!\n\n"
        "Silakan kirim format order seperti biasa.\n"
        "Setelah order diproses, kamu bisa pakai:\n"
        "/invoice - invoice PNG + PDF\n"
        "/tambah - tambah menu ke order yang sama\n"
        "/hapus - hapus salah satu menu dari order\n"
        "/ganti - tukar salah satu menu dengan menu lain"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        adapter.handle_command("/help")
    )


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        adapter.handle_command("/version")
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        adapter.handle_command("/ping")
    )


async def parser(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        adapter.handle_command("/parser")
    )


async def stat(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        adapter.handle_command("/stat")
    )


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):

    result = adapter.health()

    lines = []

    for name, status in result.items():

        icon = "✅" if status else "❌"

        lines.append(f"{icon} {name}")

    await update.message.reply_text(
        "\n".join(lines)
    )


async def _send_invoice_bundle(message, summary, filename_stem: str, reply_markup=None):
    """Kirim invoice sebagai PNG (gambar) + PDF (dokumen) sekaligus.

    Keduanya memakai layout A4 yang sama (tabel Menu/Qty/Harga/Nama
    Pemesan + grand total): PNG untuk dilihat cepat di chat, PDF untuk
    arsip / kirim ke pelanggan.
    """
    png_ok = False
    try:
        photo = render_a4_invoice_png_bytes(summary)
        await message.reply_photo(photo=photo)
        png_ok = True
    except Exception as e:
        error(f"Gagal generate invoice PNG: {e!r}")

    try:
        pdf = render_a4_invoice_bytes(summary)
        filename = f"{filename_stem}_{datetime.now():%Y%m%d_%H%M}.pdf"
        await message.reply_document(
            document=pdf,
            filename=filename,
            reply_markup=reply_markup,
        )
    except Exception as e:
        error(f"Gagal generate invoice PDF: {e!r}")
        if png_ok:
            await message.reply_text("PNG terkirim, tapi PDF gagal dibuat.")
        else:
            await message.reply_text("Gagal generate invoice.")


async def tambah(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message is None:
        return

    text = update.message.text or ""
    text = re.sub(r"^/tambah(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    if not text:
        await update.message.reply_text(
            "Kirim /tambah diikuti menu tambahannya, contoh:\n\n"
            "/tambah\nNASI GORENG: 12.000R\nBudi"
        )
        return

    username = ""

    if update.effective_user:
        username = update.effective_user.username or (
            update.effective_user.full_name
        )

    chat_id = _chat_id(update)

    reply = adapter.add_to_last_order(
        text=text,
        chat_id=chat_id,
        username=username,
    )

    await update.message.reply_text(reply, reply_markup=_order_keyboard())


async def hapus(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message is None:
        return

    text = update.message.text or ""
    query = re.sub(r"^/hapus(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    username = ""

    if update.effective_user:
        username = update.effective_user.username or (
            update.effective_user.full_name
        )

    chat_id = _chat_id(update)

    reply = adapter.remove_from_last_order(
        query=query,
        chat_id=chat_id,
        username=username,
    )

    await update.message.reply_text(reply, reply_markup=_order_keyboard())


async def ganti(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message is None:
        return

    text = update.message.text or ""
    query = re.sub(r"^/ganti(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    username = ""

    if update.effective_user:
        username = update.effective_user.username or (
            update.effective_user.full_name
        )

    chat_id = _chat_id(update)

    reply = adapter.replace_in_last_order(
        query=query,
        chat_id=chat_id,
        username=username,
    )

    await update.message.reply_text(reply, reply_markup=_order_keyboard())


async def _send_backup_file(bot, chat_id: str) -> bool:
    """Kirim data/orders.db ke chat_id. Return False kalau file belum ada."""

    if not ORDERS_DB_PATH.exists():
        return False

    stamp = now_jakarta().strftime("%Y%m%d_%H%M")

    with ORDERS_DB_PATH.open("rb") as db_file:
        await bot.send_document(
            chat_id=chat_id,
            document=db_file,
            filename=f"orders_backup_{stamp}.db",
            caption=(
                f"💾 Backup database order ({stamp} WIB).\n"
                "Simpan file ini -- kalau server di-redeploy dan datanya "
                "hilang, file ini bisa dikembalikan ke data/orders.db."
            ),
        )

    return True


async def proses_preorder_harian(context: ContextTypes.DEFAULT_TYPE):
    """Job harian jam 07:00: proses semua pre-order yang jadwalnya hari ini."""
    if adapter.store is None:
        return

    today = now_jakarta().strftime("%Y-%m-%d")
    pending = adapter.store.pending_preorders(today)

    if not pending:
        info("Pre-order harian: tidak ada pre-order untuk hari ini.")
        return

    info(f"Pre-order harian: memproses {len(pending)} pre-order untuk {today}")

    for po in pending:
        chat_id = po["chat_id"]
        order_text = po["order_text"]
        po_id = po["id"]

        try:
            reply = await asyncio.to_thread(
                adapter.parse_message, order_text, "[pre-order]", chat_id
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📅 PRE-ORDER HARI INI DIPROSES!\n\n{reply}",
                reply_markup=_order_keyboard(),
            )
            adapter.store.mark_preorder_done(po_id)
            info(f"Pre-order #{po_id} berhasil diproses untuk chat {chat_id}")
        except Exception as e:
            error(f"Gagal proses pre-order #{po_id}: {e!r}")
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ Gagal memproses pre-order #{po_id}.\n"
                        "Coba kirim ulang order secara manual."
                    ),
                )
            except Exception:
                pass


async def backup_harian(context: ContextTypes.DEFAULT_TYPE):
    """Job otomatis tiap malam: kirim backup DB ke semua chat admin."""

    if not ADMIN_CHAT_IDS:
        return

    for chat_id in ADMIN_CHAT_IDS:
        try:
            sent = await _send_backup_file(context.bot, chat_id)

            if sent:
                info(f"Backup harian terkirim ke admin {chat_id}.")
            else:
                info("Backup harian dilewati: belum ada database order.")

        except Exception as e:
            error(f"Gagal kirim backup harian ke {chat_id}: {e!r}")


async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message is None:
        return

    chat_id = _chat_id(update)

    if not ADMIN_CHAT_IDS:
        await update.message.reply_text(
            "Backup belum diaktifkan. Isi ADMIN_CHAT_ID di environment "
            "variable dengan chat ID admin, lalu restart bot."
        )
        return

    if chat_id not in ADMIN_CHAT_IDS:
        await update.message.reply_text(
            "Command ini hanya bisa dipakai dari chat admin."
        )
        return

    try:
        sent = await _send_backup_file(context.bot, chat_id)

        if not sent:
            await update.message.reply_text(
                "Belum ada database order yang bisa di-backup."
            )

    except Exception as e:
        error(f"Gagal kirim backup manual: {e!r}")
        await update.message.reply_text("Gagal mengirim backup.")


async def lihatpreorder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    chat_id = _chat_id(update)
    await update.message.reply_text(adapter.list_preorders(chat_id))


async def batalpreorder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    chat_id = _chat_id(update)
    text = update.message.text or ""
    arg = re.sub(r"^/batalpreorder(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()
    if not arg.isdigit():
        await update.message.reply_text(
            "Format: /batalpreorder <nomor>\n\n"
            "Lihat nomor pre-order dengan /lihatpreorder"
        )
        return
    await update.message.reply_text(
        adapter.cancel_preorder(int(arg), chat_id)
    )


async def pakaiai(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message is None:
        return

    chat_id = _chat_id(update)

    await update.message.reply_text(adapter.switch_to_ai_version(chat_id))


# ==========================================================
# MENU MANAGEMENT (admin only)
# ==========================================================

def _is_admin(chat_id: str) -> bool:
    return chat_id in ADMIN_CHAT_IDS


def _user_id(update: Update) -> str:
    """User yang mengirim pesan. Di chat pribadi = chat.id (backward
    compatible). Di grup, chat.id adalah id grup, jadi kita perlu id
    user secara terpisah supaya admin bisa jalankan command dari grup."""

    if update.effective_user:
        return str(update.effective_user.id)
    return _chat_id(update)


def _is_admin_actor(update: Update) -> bool:
    """Admin kalau chat.id ATAU user.id ada di daftar admin. Ini
    mempertahankan pola lama (ADMIN_CHAT_ID diisi chat_id pribadi) DAN
    memungkinkan admin jalankan command di dalam grup (di mana chat.id
    berbeda dari user.id)."""

    return _is_admin(_chat_id(update)) or _is_admin(_user_id(update))


async def _reject_non_admin(update: Update) -> bool:
    """Return True (dan kirim pesan ditolak) kalau bukan admin."""

    if not ADMIN_CHAT_IDS:
        await update.message.reply_text(
            "Fitur manajemen menu belum diaktifkan.\n"
            "Isi ADMIN_CHAT_ID di environment variable, lalu restart bot."
        )
        return True

    if not _is_admin_actor(update):
        await update.message.reply_text(
            "Command ini hanya bisa dipakai dari chat admin."
        )
        return True

    return False


async def tambahmenu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    text = update.message.text or ""
    args = re.sub(r"^/tambahmenu(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    if not args:
        await update.message.reply_text(
            "Format: /tambahmenu NAMA MENU | HARGA | KATEGORI\n\n"
            "Contoh:\n"
            "/tambahmenu SATE AYAM | 15000 | MAKANAN\n"
            "/tambahmenu NASI BAKAR IKAN | 20000 | NASI\n"
            "/tambahmenu ES JERUK | 5000 | MINUMAN | 🥤\n\n"
            "KATEGORI dan EMOJI bersifat opsional."
        )
        return

    parts = [p.strip() for p in args.split("|")]

    if len(parts) < 2:
        await update.message.reply_text(
            "Format salah. Pisahkan dengan tanda | (pipe).\n"
            "Contoh: /tambahmenu SATE AYAM | 15000 | MAKANAN"
        )
        return

    name = parts[0].strip().upper()
    category = parts[2].strip().upper() if len(parts) >= 3 else ""
    emoji = parts[3].strip() if len(parts) >= 4 else ""

    try:
        price = int(parts[1].replace(".", "").replace(",", "").strip())
    except ValueError:
        await update.message.reply_text(
            f"Harga '{parts[1]}' bukan angka yang valid."
        )
        return

    if price <= 0:
        await update.message.reply_text("Harga harus lebih dari 0.")
        return

    ok, msg = menu_mgr.add_menu(
        name=name, price=price, category=category, emoji=emoji,
    )

    if ok:
        adapter.reload_menus()

    await update.message.reply_text(msg)


async def hapusmenu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    text = update.message.text or ""
    name = re.sub(r"^/hapusmenu(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    if not name:
        await update.message.reply_text(
            "Format: /hapusmenu NAMA MENU\n\n"
            "Contoh: /hapusmenu SATE AYAM\n\n"
            "Gunakan /daftarmenu untuk melihat daftar menu."
        )
        return

    ok, msg = menu_mgr.remove_menu(name)

    if ok:
        adapter.reload_menus()

    await update.message.reply_text(msg)


async def updatemenu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    text = update.message.text or ""
    args = re.sub(r"^/updatemenu(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    if not args:
        await update.message.reply_text(
            "Format: /updatemenu NAMA MENU | HARGA BARU\n\n"
            "Contoh: /updatemenu SATE AYAM | 18000\n\n"
            "Gunakan /daftarmenu untuk melihat daftar menu."
        )
        return

    parts = [p.strip() for p in args.split("|")]

    if len(parts) < 2:
        await update.message.reply_text(
            "Format salah. Pisahkan dengan tanda | (pipe).\n"
            "Contoh: /updatemenu SATE AYAM | 18000"
        )
        return

    name = parts[0].strip()

    try:
        new_price = int(parts[1].replace(".", "").replace(",", "").strip())
    except ValueError:
        await update.message.reply_text(
            f"Harga '{parts[1]}' bukan angka yang valid."
        )
        return

    if new_price <= 0:
        await update.message.reply_text("Harga harus lebih dari 0.")
        return

    ok, msg = menu_mgr.update_price(name, new_price)

    if ok:
        adapter.reload_menus()

    await update.message.reply_text(msg)


async def daftarmenu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    text = update.message.text or ""
    args = re.sub(r"^/daftarmenu(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    if not args:
        cats = menu_mgr.list_categories()

        if not cats:
            await update.message.reply_text("Belum ada menu yang terdaftar.")
            return

        lines = ["📋 DAFTAR KATEGORI MENU\n"]

        for cat, count in cats:
            lines.append(f"• {cat} ({count} menu)")

        lines.append(f"\nTotal: {sum(c for _, c in cats)} menu")
        lines.append("\nKetik /daftarmenu NAMA KATEGORI untuk lihat isinya.")
        lines.append("Ketik /daftarmenu cari KATA KUNCI untuk mencari menu.")

        await update.message.reply_text("\n".join(lines))
        return

    if args.lower().startswith("cari "):
        keyword = args[5:].strip()

        if not keyword:
            await update.message.reply_text("Ketik kata kunci setelah 'cari'.")
            return

        results = menu_mgr.search_menus(keyword)

        if not results:
            await update.message.reply_text(
                f"Tidak ada menu yang mengandung '{keyword}'."
            )
            return

        lines = [f"🔍 Hasil pencarian '{keyword}' ({len(results)} menu):\n"]

        for m in results[:30]:
            emoji = m.get("emoji", "")
            prefix = f"{emoji} " if emoji else "• "
            lines.append(f"{prefix}{m['name']}: {m['price']:,} Riel")

        if len(results) > 30:
            lines.append(f"\n... dan {len(results) - 30} menu lainnya.")

        await update.message.reply_text("\n".join(lines))
        return

    menus = menu_mgr.list_menus_by_category(args)

    if not menus:
        await update.message.reply_text(
            f"Tidak ada menu di kategori '{args}'.\n"
            "Ketik /daftarmenu untuk melihat daftar kategori."
        )
        return

    lines = [f"📋 MENU KATEGORI: {args.upper()}\n"]

    for m in menus:
        emoji = m.get("emoji", "")
        prefix = f"{emoji} " if emoji else "• "
        lines.append(f"{prefix}{m['name']}: {m['price']:,} Riel")

    lines.append(f"\nTotal: {len(menus)} menu")

    msg = "\n".join(lines)

    if len(msg) > 4000:
        for i in range(0, len(msg), 4000):
            await update.message.reply_text(msg[i : i + 4000])
    else:
        await update.message.reply_text(msg)


# ==========================================================
# EXPORT / IMPORT MENU (Excel round-trip)
# ==========================================================

async def exportmenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kirim file Excel berisi seluruh menu terkini. Admin bisa edit
    file itu di HP/laptop lalu kirim balik + /importmenu."""

    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    try:
        from app.menu_excel_io import menus_to_workbook_bytes

        menus = menu_mgr.store.get_all_menus()

        if not menus:
            await update.message.reply_text(
                "Belum ada menu di database, tidak ada yang di-export."
            )
            return

        data = menus_to_workbook_bytes(menus)
        stamp = now_jakarta().strftime("%Y%m%d-%H%M")
        filename = f"menu-{stamp}.xlsx"

        await context.bot.send_document(
            chat_id=update.message.chat_id,
            document=data,
            filename=filename,
            caption=(
                f"📤 Menu terkini ({len(menus)} baris).\n\n"
                "Cara update:\n"
                "1. Buka file di Excel/Google Sheets/WPS.\n"
                "2. Edit (tambah baris baru / ubah harga / dst).\n"
                "3. Save sebagai .xlsx, kirim balik ke chat ini.\n"
                "4. Reply file itu dengan /importmenu.\n\n"
                "Header kolom WAJIB: Nama & Harga (kolom lain opsional)."
            ),
        )
    except Exception as e:
        error(f"exportmenu gagal: {e!r}")
        await update.message.reply_text(f"Gagal export menu: {e}")


async def importmenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Import menu dari file Excel yang di-reply. UPSERT by nama:
    tambah baris baru, update yang sudah ada. TIDAK menghapus menu
    yang tidak ada di file (pakai /hapusmenu untuk itu)."""

    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    reply_to = update.message.reply_to_message

    if reply_to is None or reply_to.document is None:
        await update.message.reply_text(
            "Cara pakai:\n"
            "1. Kirim file .xlsx (hasil /exportmenu yang sudah diedit) ke chat ini.\n"
            "2. Reply file itu dengan /importmenu.\n\n"
            "Menu akan ditambah/diupdate berdasarkan nama.\n"
            "TIDAK menghapus menu yang tidak ada di file -- pakai /hapusmenu."
        )
        return

    doc = reply_to.document
    name = (doc.file_name or "").lower()

    if not name.endswith((".xlsx", ".xlsm")):
        await update.message.reply_text(
            f"File harus berformat Excel (.xlsx). Diterima: {doc.file_name}"
        )
        return

    try:
        from app.menu_excel_io import workbook_bytes_to_menus

        tg_file = await context.bot.get_file(doc.file_id)
        data = await tg_file.download_as_bytearray()

        rows = workbook_bytes_to_menus(bytes(data))

        if not rows:
            await update.message.reply_text(
                "File Excel kosong atau tidak ada baris data setelah header."
            )
            return

        added, updated, errors = menu_mgr.store.bulk_upsert(rows)

        # Reload cache menu di adapter (AI parser). Parser regex sendiri
        # baca DB tiap _new_parser() -- otomatis lihat menu terbaru.
        try:
            adapter.reload_menus()
        except Exception:
            pass

        lines = [
            "📥 Import selesai.",
            f"➕ Ditambah: {added}",
            f"🔄 Diupdate: {updated}",
            f"📄 Total baris file: {len(rows)}",
        ]

        if errors:
            lines.append(f"⚠️ Error: {len(errors)}")
            for msg in errors[:5]:
                lines.append(f"  • {msg}")
            if len(errors) > 5:
                lines.append(f"  ... dan {len(errors) - 5} error lain.")

        lines.append("")
        lines.append(
            "Catatan: menu yang tidak ada di file TIDAK dihapus. "
            "Untuk hapus, pakai /hapusmenu."
        )

        await update.message.reply_text("\n".join(lines))

    except ValueError as e:
        await update.message.reply_text(f"Format file salah: {e}")
    except Exception as e:
        error(f"importmenu gagal: {e!r}")
        await update.message.reply_text(f"Gagal import menu: {e}")


# ==========================================================
# BROADCAST MENU HARIAN
# ==========================================================

def _get_menus_from_adapter():
    """Ambil daftar menu aktif dari matcher (singleton adapter)."""
    try:
        return adapter._new_parser().matcher.menus
    except Exception:
        return []


def _today_str() -> str:
    return now_jakarta().strftime("%Y-%m-%d")


def _date_label() -> str:
    HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    BULAN = [
        "", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
        "Juli", "Agustus", "September", "Oktober", "November", "Desember",
    ]
    now = now_jakarta()
    return f"{HARI[now.weekday()]}, {now.day} {BULAN[now.month]} {now.year}"


async def readymenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tandai menu sebagai TERSEDIA hari ini."""
    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    text = update.message.text or ""
    name = re.sub(r"^/readymenu(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    if not name:
        await update.message.reply_text(
            "Format: /readymenu NAMA MENU\n\n"
            "Contoh: /readymenu BUBUR AYAM\n\n"
            "Gunakan /lihatready untuk melihat status menu hari ini."
        )
        return

    if adapter.store is None:
        await update.message.reply_text("Database tidak tersedia.")
        return

    # Fuzzy-cari nama menu di katalog
    parser = adapter._new_parser()
    menu, score = parser.matcher.search(name.upper())

    if menu is None:
        await update.message.reply_text(
            f"Menu '{name}' tidak ditemukan di daftar menu.\n"
            "Gunakan /daftarmenu untuk melihat daftar menu."
        )
        return

    adapter.store.set_menu_ready(menu["name"], True, _today_str())
    await update.message.reply_text(
        f"✅ {menu['name']} ditandai TERSEDIA hari ini."
    )


async def notready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tandai menu sebagai TIDAK TERSEDIA hari ini."""
    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    text = update.message.text or ""
    name = re.sub(r"^/notready(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    if not name:
        await update.message.reply_text(
            "Format: /notready NAMA MENU\n\n"
            "Contoh: /notready NASI UDUK KOMPLIT\n\n"
            "Gunakan /lihatready untuk melihat status menu hari ini."
        )
        return

    if adapter.store is None:
        await update.message.reply_text("Database tidak tersedia.")
        return

    parser = adapter._new_parser()
    menu, score = parser.matcher.search(name.upper())

    if menu is None:
        await update.message.reply_text(
            f"Menu '{name}' tidak ditemukan di daftar menu.\n"
            "Gunakan /daftarmenu untuk melihat daftar menu."
        )
        return

    adapter.store.set_menu_ready(menu["name"], False, _today_str())
    await update.message.reply_text(
        f"❌ {menu['name']} ditandai TIDAK TERSEDIA hari ini."
    )


async def lihatready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan status ready/tidak hari ini (admin only)."""
    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    if adapter.store is None:
        await update.message.reply_text("Database tidak tersedia.")
        return

    menus = _get_menus_from_adapter()
    availability = adapter.store.get_menu_availability(_today_str())
    text = format_availability_status(menus, availability, _date_label())

    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i : i + 4000])
    else:
        await update.message.reply_text(text)


async def resetready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset semua menu ke TERSEDIA untuk hari ini (hapus semua entri hari ini)."""
    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    if adapter.store is None:
        await update.message.reply_text("Database tidak tersedia.")
        return

    n = adapter.store.reset_menu_availability(_today_str())
    if n == 0:
        await update.message.reply_text(
            "Tidak ada data ketersediaan hari ini yang perlu direset.\n"
            "(Semua menu sudah dalam kondisi default: tersedia.)"
        )
    else:
        await update.message.reply_text(
            f"✅ Reset selesai -- {n} status menu dihapus.\n"
            "Semua menu sekarang dianggap TERSEDIA hari ini."
        )


# ==========================================================
# TUTOR / PANDUAN
# ==========================================================

_TUTOR_SECTIONS = [
    (
        "📖 PANDUAN BOT MADAM LILY  (1/7)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛒  CARA ORDER BIASA\n\n"
        "Kirim teks order langsung ke chat ini.\n"
        "Format bebas — bot otomatis membaca nama menu,\n"
        "jumlah, dan nama pelanggan.\n\n"
        "Contoh:\n"
        "─────────────────\n"
        "SOTO AYAM 2\n"
        "NASI GORENG 1\n"
        "BUDI\n\n"
        "TEH MANIS 3\n"
        "SITI\n"
        "─────────────────\n\n"
        "Bot akan membalas dengan rincian harga per pelanggan\n"
        "dan grand total.\n\n"
        "Setelah order diterima, bot meminta lokasi & nama:\n"
        "Balas dengan format  LOKASI/NAMA\n"
        "Contoh:  KD/NICOLAS"
    ),
    (
        "📖 PANDUAN BOT MADAM LILY  (2/7)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✏️  MENGUBAH ORDER\n\n"
        "Setelah order masuk, kamu bisa mengubahnya:\n\n"
        "➕ TAMBAH ITEM\n"
        "/tambah\n"
        "Lalu kirim teks menu tambahan di pesan berikutnya.\n\n"
        "➖ HAPUS ITEM\n"
        "/hapus NAMA MENU\n"
        "Contoh: /hapus SOTO AYAM\n\n"
        "🔄 GANTI MENU PELANGGAN TERTENTU\n"
        "/ganti MENU BARU NAMA_PELANGGAN\n"
        "Contoh: /ganti NASI GORENG BUDI\n"
        "(Menu BUDI diganti jadi NASI GORENG)\n\n"
        "🤖 PAKAI VERSI AI\n"
        "/pakaiai\n"
        "Kalau bot menandai perbedaan hasil baca AI vs parser,\n"
        "pakai command ini untuk memilih versi AI."
    ),
    (
        "📖 PANDUAN BOT MADAM LILY  (3/7)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🧾  INVOICE\n\n"
        "Setelah order diproses:\n\n"
        "📄 INVOICE (PNG + PDF)\n"
        "/invoice\n"
        "Bot kirim 2 file sekaligus:\n"
        "• Gambar PNG — praktis dilihat / print cepat\n"
        "• File PDF (A4) — untuk arsip atau kirim ke pelanggan\n\n"
        "📋 PRE-ORDER (pesan untuk besok)\n"
        "Tambahkan  BESOK:  di awal pesan order.\n\n"
        "Contoh:\n"
        "─────────────────\n"
        "BESOK:\n"
        "NASI GORENG 2\n"
        "SOTO AYAM 1\n"
        "BUDI\n"
        "─────────────────\n\n"
        "Lihat pre-order pending: /lihatpreorder\n"
        "Batalkan pre-order:      /batalpreorder <nomor>"
    ),
    (
        "📖 PANDUAN BOT MADAM LILY  (4/7)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🪑  DINE-IN / MEJA\n\n"
        "Bot mendukung 8 meja fisik: IN1–IN4 dan OUT1–OUT4.\n\n"
        "CARA 1 — Tombol interaktif:\n"
        "/pesanmeja\n"
        "Pilih meja dari tombol. Meja 🔴 = ada pesanan, 🟢 = kosong.\n"
        "Setelah pilih meja, kirim teks order seperti biasa.\n"
        "Tersedia tombol: Tambah | Hapus Item | Lihat Harga |\n"
        "                 Bayar | Invoice\n\n"
        "CARA 2 — Input langsung:\n"
        "/ordermeja IN1\n"
        "Lalu kirim teks order di pesan berikutnya.\n\n"
        "CARA 3 — Mode sticky:\n"
        "/dinein IN1\n"
        "Semua order berikutnya otomatis masuk ke meja IN1.\n"
        "/selesaidinein  → keluar mode sticky\n\n"
        "LIHAT & BAYAR:\n"
        "/tagihan IN1   → lihat rincian tagihan meja\n"
        "/bayar IN1     → tandai meja sudah bayar & tutup sesi\n"
        "/invoicemeja IN1 → invoice meja (PNG + PDF)"
    ),
    (
        "📖 PANDUAN BOT MADAM LILY  (5/7)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🏷️  DISKON & PROMO  (admin)\n\n"
        "Set diskon persen:\n"
        "/setdiskon SOTO AYAM PERSEN 10\n"
        "→ Soto Ayam diskon 10% dari harga asli\n\n"
        "Set diskon nominal (Riel):\n"
        "/setdiskon NASI GORENG NOMINAL 2000\n"
        "→ Nasi Goreng diskon flat 2.000 Riel\n\n"
        "Diskon otomatis berlaku di semua order berikutnya\n"
        "dan tampil di invoice (PNG & PDF).\n\n"
        "Lihat semua diskon aktif:\n"
        "/daftardiskon\n\n"
        "Hapus diskon:\n"
        "/hapusdiskon SOTO AYAM"
    ),
    (
        "📖 PANDUAN BOT MADAM LILY  (6/7)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️  MANAJEMEN MENU  (admin)\n\n"
        "Lihat daftar menu:\n"
        "/daftarmenu\n\n"
        "Tambah menu baru:\n"
        "/tambahmenu\n"
        "Bot akan memandu langkah demi langkah.\n\n"
        "Ubah harga menu:\n"
        "/updatemenu NAMA MENU HARGA_BARU\n"
        "Contoh: /updatemenu SOTO AYAM 9000\n\n"
        "Hapus menu:\n"
        "/hapusmenu NAMA MENU\n\n"
        "Ketersediaan menu hari ini:\n"
        "/readymenu SOTO AYAM   → tandai TERSEDIA\n"
        "/notready SOTO AYAM    → tandai TIDAK TERSEDIA\n"
        "/lihatready             → lihat status semua menu\n"
        "/resetready             → reset semua ke tersedia\n\n"
        "Backup database:\n"
        "/backup  → bot kirim file backup DB ke admin"
    ),
    (
        "📖 PANDUAN BOT MADAM LILY  (7/7)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡  TIPS & PINTASAN\n\n"
        "• Format order bebas — tidak perlu urutan tertentu.\n"
        "  Bot membaca nama menu dengan fuzzy matching.\n\n"
        "• Nama pelanggan ditulis di BAWAH daftar menu-nya.\n\n"
        "• Bisa multi-pelanggan dalam 1 pesan:\n"
        "  SOTO AYAM 2 / BUDI\n"
        "  TEH MANIS 1 / SITI\n\n"
        "• Setelah order masuk, langsung ketik /invoice\n"
        "  untuk cetak tanpa perlu ketik ulang.\n\n"
        "• /daftarmeja → lihat status semua meja sekaligus\n\n"
        "• /help → daftar lengkap semua command\n\n"
        "• /tutor → tampilkan panduan ini lagi\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Butuh bantuan lebih? Hubungi admin."
    ),
]


async def tutor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kirim panduan lengkap penggunaan bot dalam beberapa pesan."""
    if update.message is None:
        return
    for section in _TUTOR_SECTIONS:
        await update.message.reply_text(section)


# ==========================================================
# BROADCAST KE BANYAK GRUP
# ==========================================================

async def daftargrup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Daftarkan grup ini ke daftar broadcast (admin only, di dalam grup)."""

    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    chat = update.effective_chat

    if chat is None or chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "Command ini harus dijalankan DI DALAM grup Telegram yang mau "
            "menerima broadcast.\n\n"
            "Cara pakai:\n"
            "1. Tambahkan bot ke grup.\n"
            "2. Ketik /daftargrup di grup tsb dari akun admin."
        )
        return

    added = broadcast_store.add_group(
        chat_id=str(chat.id),
        title=chat.title or "",
        added_by=_user_id(update),
    )

    total = broadcast_store.count()

    if added:
        await update.message.reply_text(
            f"✅ Grup '{chat.title or chat.id}' terdaftar.\n"
            f"Total grup broadcast: {total}.\n\n"
            "Gunakan /broadcast di chat pribadi (reply pesan/foto/dokumen) "
            "untuk mengirim ke semua grup."
        )
    else:
        await update.message.reply_text(
            f"ℹ️ Grup '{chat.title or chat.id}' sudah terdaftar.\n"
            f"Total grup broadcast: {total}."
        )


async def keluargrup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Keluarkan grup ini dari daftar broadcast (admin only, di grup)."""

    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    chat = update.effective_chat

    if chat is None:
        return

    removed = broadcast_store.remove_group(str(chat.id))

    if removed:
        await update.message.reply_text(
            f"✅ Grup '{chat.title or chat.id}' dikeluarkan dari daftar "
            "broadcast."
        )
    else:
        await update.message.reply_text(
            "Grup ini tidak terdaftar di daftar broadcast."
        )


async def listgrup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lihat daftar grup broadcast (admin only)."""

    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    groups = broadcast_store.list_groups()

    if not groups:
        await update.message.reply_text(
            "Belum ada grup broadcast terdaftar.\n\n"
            "Cara daftar: tambahkan bot ke grup, lalu ketik /daftargrup "
            "di grup tsb."
        )
        return

    lines = [f"📢 GRUP BROADCAST ({len(groups)}):", ""]

    for i, g in enumerate(groups, 1):
        title = g["title"] or "(tanpa judul)"
        lines.append(f"{i}. {title}")
        lines.append(f"   chat_id: {g['chat_id']}")

    await update.message.reply_text("\n".join(lines))


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kirim pesan (teks/foto/dokumen/dsb.) ke semua grup broadcast.

    Cara pakai: reply pesan yang mau di-broadcast dengan /broadcast.
    Pesan asli di-copy (media, caption, format terjaga)."""

    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    reply_to = update.message.reply_to_message

    if reply_to is None:
        await update.message.reply_text(
            "Cara pakai:\n"
            "1. Kirim/paste pesan (teks, foto, dokumen, video, dsb.) di "
            "chat ini.\n"
            "2. Reply pesan itu dengan /broadcast.\n\n"
            "Pesan akan diteruskan (copy) ke semua grup yang terdaftar.\n"
            "Lihat daftar grup: /listgrup"
        )
        return

    groups = broadcast_store.list_groups()

    if not groups:
        await update.message.reply_text(
            "Belum ada grup broadcast terdaftar.\n\n"
            "Cara daftar: tambahkan bot ke grup, lalu /daftargrup di grup tsb."
        )
        return

    source_chat_id = update.message.chat_id
    source_msg_id = reply_to.message_id

    sent = 0
    failed = []

    for g in groups:
        try:
            await context.bot.copy_message(
                chat_id=int(g["chat_id"]),
                from_chat_id=source_chat_id,
                message_id=source_msg_id,
            )
            sent += 1
        except Exception as e:
            failed.append((g.get("title") or g["chat_id"], str(e)))
            warning(
                f"Broadcast ke grup {g['chat_id']} gagal: {e!r}"
            )

    report = [f"📢 Broadcast selesai.", f"✅ Terkirim: {sent}/{len(groups)}"]

    if failed:
        report.append(f"❌ Gagal: {len(failed)}")
        for title, err in failed[:5]:
            short = err.split("\n")[0][:100]
            report.append(f"  • {title}: {short}")
        if len(failed) > 5:
            report.append(f"  ... dan {len(failed) - 5} grup lain.")

    await update.message.reply_text("\n".join(report))


# ==========================================================
# DISKON / PROMO
# ==========================================================

async def setdiskon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set diskon untuk menu tertentu (admin only).

    Format:
      /setdiskon NAMA MENU PERSEN 10      → diskon 10%
      /setdiskon NAMA MENU NOMINAL 5000   → diskon 5000 Riel
    """
    if update.message is None:
        return
    if await _reject_non_admin(update):
        return
    if adapter.store is None:
        await update.message.reply_text("Database tidak tersedia.")
        return

    text = update.message.text or ""
    arg = re.sub(r"^/setdiskon(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    # Pisahkan: semua kecuali 2 token terakhir = nama menu; token[-2] = tipe; token[-1] = nilai
    tokens = arg.split()
    if len(tokens) < 3:
        await update.message.reply_text(
            "Format: /setdiskon NAMA MENU PERSEN 10\n"
            "   atau: /setdiskon NAMA MENU NOMINAL 5000\n\n"
            "Contoh:\n"
            "  /setdiskon SOTO AYAM PERSEN 10\n"
            "  /setdiskon NASI GORENG NOMINAL 2000"
        )
        return

    disc_type_raw = tokens[-2].upper()
    disc_val_raw = tokens[-1]
    menu_query = " ".join(tokens[:-2])

    if disc_type_raw not in ("PERSEN", "NOMINAL"):
        await update.message.reply_text(
            "Tipe diskon harus PERSEN atau NOMINAL.\n"
            "Contoh: /setdiskon SOTO AYAM PERSEN 10"
        )
        return

    try:
        disc_val = float(disc_val_raw.replace(",", "."))
    except ValueError:
        await update.message.reply_text(f"Nilai diskon tidak valid: {disc_val_raw}")
        return

    if disc_val <= 0:
        await update.message.reply_text("Nilai diskon harus lebih dari 0.")
        return
    if disc_type_raw == "PERSEN" and disc_val > 100:
        await update.message.reply_text("Diskon persen tidak boleh melebihi 100%.")
        return

    # Cari menu di katalog (fuzzy)
    parser = adapter._new_parser()
    menu, score = parser.matcher.search(menu_query.upper())
    if menu is None:
        await update.message.reply_text(
            f"Menu '{menu_query}' tidak ditemukan di katalog.\n"
            "Gunakan /daftarmenu untuk melihat daftar menu."
        )
        return

    disc_type = disc_type_raw.lower()
    adapter.store.set_discount(menu["name"], disc_type, disc_val)

    if disc_type == "persen":
        contoh_riel = int(menu["price"] * disc_val / 100)
        desc = f"{disc_val:.0f}% (≈ {contoh_riel:,.0f} Riel dari harga {menu['price']:,.0f} Riel)"
    else:
        desc = f"{disc_val:,.0f} Riel"

    await update.message.reply_text(
        f"✅ Diskon berhasil disimpan.\n\n"
        f"Menu    : {menu['name']}\n"
        f"Diskon  : {desc}\n\n"
        f"Diskon ini akan otomatis diterapkan ke semua order berikutnya."
    )


async def hapusdiskon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hapus diskon untuk menu tertentu (admin only)."""
    if update.message is None:
        return
    if await _reject_non_admin(update):
        return
    if adapter.store is None:
        await update.message.reply_text("Database tidak tersedia.")
        return

    text = update.message.text or ""
    menu_query = re.sub(r"^/hapusdiskon(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    if not menu_query:
        await update.message.reply_text(
            "Format: /hapusdiskon NAMA MENU\n"
            "Contoh: /hapusdiskon SOTO AYAM\n\n"
            "Gunakan /daftardiskon untuk lihat diskon aktif."
        )
        return

    # Cari fuzzy dulu
    parser = adapter._new_parser()
    menu, _ = parser.matcher.search(menu_query.upper())
    menu_name = menu["name"] if menu else menu_query.upper()

    ok = adapter.store.remove_discount(menu_name)
    if ok:
        await update.message.reply_text(f"✅ Diskon untuk {menu_name} berhasil dihapus.")
    else:
        await update.message.reply_text(
            f"Tidak ada diskon aktif untuk '{menu_name}'.\n"
            "Gunakan /daftardiskon untuk melihat daftar diskon."
        )


async def daftardiskon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan semua diskon aktif (admin only)."""
    if update.message is None:
        return
    if await _reject_non_admin(update):
        return
    if adapter.store is None:
        await update.message.reply_text("Database tidak tersedia.")
        return

    discounts = adapter.store.get_all_discounts()
    if not discounts:
        await update.message.reply_text(
            "Belum ada diskon aktif.\n\n"
            "Tambahkan dengan:\n"
            "/setdiskon NAMA MENU PERSEN 10\n"
            "/setdiskon NAMA MENU NOMINAL 5000"
        )
        return

    # Ambil katalog untuk tampilkan harga asli
    parser = adapter._new_parser()
    price_map = {m["name"].upper(): m["price"] for m in parser.matcher.menus}

    lines = [f"🏷️ DISKON AKTIF ({len(discounts)} menu):\n"]
    for d in discounts:
        name = d["menu_name"]
        orig = price_map.get(name, 0)
        if d["discount_type"] == "persen":
            eff = int(orig * (1 - d["discount_value"] / 100))
            desc = f"−{d['discount_value']:.0f}%  ({orig:,.0f}R → {eff:,.0f}R)"
        else:
            eff = max(0, orig - int(d["discount_value"]))
            desc = f"−{d['discount_value']:,.0f}R  ({orig:,.0f}R → {eff:,.0f}R)"
        lines.append(f"• {name}\n  {desc}")

    lines += ["", "Hapus diskon: /hapusdiskon NAMA MENU"]
    await update.message.reply_text("\n".join(lines))


# ==========================================================
# DINE-IN
# ==========================================================

def _table_select_keyboard() -> Optional[InlineKeyboardMarkup]:
    """Keyboard pilih meja — tombol per meja, dikelompokkan IN / OUT / lainnya.
    Meja terisi ditandai 🔴, kosong 🟢. Return None kalau tidak ada meja."""
    if dinein_mgr is None:
        return None

    try:
        tables = dinein_mgr.list_tables()
    except Exception:
        return None

    if not tables:
        return None

    def _btn(t: dict) -> InlineKeyboardButton:
        icon = "🔴" if t["active"] else "🟢"
        return InlineKeyboardButton(
            f"{icon} {t['table_no']}",
            callback_data=f"dinein_table:{t['table_no']}",
        )

    in_tables  = [t for t in tables if t["table_no"].startswith("IN")]
    out_tables = [t for t in tables if t["table_no"].startswith("OUT")]
    other      = [t for t in tables
                  if not t["table_no"].startswith("IN")
                  and not t["table_no"].startswith("OUT")]

    rows: list = []
    if in_tables:
        rows.append([_btn(t) for t in in_tables])
    if out_tables:
        rows.append([_btn(t) for t in out_tables])
    chunk: list = []
    for t in other:
        chunk.append(_btn(t))
        if len(chunk) == 4:
            rows.append(chunk)
            chunk = []
    if chunk:
        rows.append(chunk)

    if not rows:
        return None

    rows.append([InlineKeyboardButton("❌ Batalkan", callback_data="dinein_cancel")])
    return InlineKeyboardMarkup(rows)


def _table_action_keyboard(table_no: str, has_orders: bool) -> InlineKeyboardMarkup:
    """Tombol aksi meja: tampilkan, tambah, hapus item, bayar, invoice."""
    rows = [
        [InlineKeyboardButton("➕ Tambah Order", callback_data=f"dinein_action:{table_no}:add")],
    ]
    if has_orders:
        rows.append([
            InlineKeyboardButton("🗑️ Hapus Item",  callback_data=f"dinein_action:{table_no}:remove"),
            InlineKeyboardButton("📋 Lihat Harga", callback_data=f"dinein_action:{table_no}:view"),
        ])
        rows.append([
            InlineKeyboardButton("💰 Bayar",   callback_data=f"dinein_action:{table_no}:pay"),
            InlineKeyboardButton("📄 Invoice", callback_data=f"dinein_action:{table_no}:invoice"),
        ])
    rows.append([InlineKeyboardButton("⬅️ Pilih Meja Lain", callback_data="dinein_back")])
    return InlineKeyboardMarkup(rows)


def _table_remove_keyboard(table_no: str) -> Optional[InlineKeyboardMarkup]:
    """Keyboard daftar item yang bisa dihapus dari meja."""
    if dinein_mgr is None:
        return None
    items = dinein_mgr.get_flat_items(table_no)
    if not items:
        return None
    rows = []
    for it in items:
        price_str = f"{it['price']:,.0f}".replace(",", ".")
        label = f"🗑️ {it['name']} x{it['qty']} ({price_str}R)"
        if it["customer"]:
            label += f" — {it['customer']}"
        rows.append([
            InlineKeyboardButton(label, callback_data=f"dinein_remove:{table_no}:{it['flat_idx']}")
        ])
    rows.append([InlineKeyboardButton("⬅️ Kembali", callback_data=f"dinein_table:{table_no}")])
    return InlineKeyboardMarkup(rows)


def _merge_summaries(base: OrderSummary, new: OrderSummary) -> OrderSummary:
    """Gabungkan invoice baru ke summary meja yang sudah ada."""
    return OrderSummary(
        invoices=list(base.invoices) + list(new.invoices),
        grand_total_riel=base.grand_total_riel + new.grand_total_riel,
        grand_total_usd=round(base.grand_total_usd + new.grand_total_usd, 2),
        destination=new.destination or base.destination,
        orderer_name=new.orderer_name or base.orderer_name,
    )


def _format_table_bill(table_no: str, summary: OrderSummary, started_at: str = "") -> str:
    """Format teks tagihan meja untuk ditampilkan."""
    lines = [f"🪑 TAGIHAN MEJA {table_no}"]
    if started_at:
        lines.append(f"Mulai: {started_at[:16].replace('T', ' ')}")
    lines.append("")

    for inv in summary.invoices:
        lines.append(f"👤 {inv.telegram_name or '—'}")
        for item in inv.items:
            price_str = f"{item.price:,.0f}".replace(",", ".")
            lines.append(f"  • {item.menu} x{item.qty} = {price_str} R")
        lines.append("")

    total_str = f"{summary.grand_total_riel:,.0f}".replace(",", ".")
    lines.append(f"TOTAL: {total_str} Riel")
    return "\n".join(lines)


def _require_dinein_mgr(update) -> bool:
    """Return True (sudah reply) kalau dinein_mgr tidak tersedia."""
    if dinein_mgr is None:
        return True
    return False


async def pesanmeja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan tombol pilih meja, lalu tunggu teks order."""
    if update.message is None:
        return

    try:
        if dinein_mgr is None:
            await update.message.reply_text("Database tidak tersedia.")
            return

        kb = _table_select_keyboard()
        if kb is None:
            await update.message.reply_text(
                "Belum ada meja yang dibuat.\n"
                "Admin bisa buat meja dengan /buatmeja NOMOR.\n\n"
                "Contoh:\n/buatmeja IN1\n/buatmeja IN2\n/buatmeja OUT1"
            )
            return

        await update.message.reply_text(
            "🍽️ Pilih meja untuk memasukkan order:\n"
            "🟢 = kosong  |  🔴 = terisi",
            reply_markup=kb,
        )

    except Exception as e:
        error(f"pesanmeja error: {e!r}")
        await update.message.reply_text(
            f"Terjadi error: {e}\n\n"
            "Pastikan meja sudah dibuat dengan /buatmeja NOMOR."
        )


async def ordermeja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Masukkan order langsung ke meja tertentu tanpa perlu /dinein dulu.

    Format:
        /ordermeja A1
        Budi: NASI GORENG
        Siti: ES TEH
    """
    if update.message is None:
        return
    if dinein_mgr is None:
        await update.message.reply_text("Database tidak tersedia.")
        return

    text = update.message.text or ""
    # Ambil semua teks setelah "/ordermeja(@bot)?"
    body = re.sub(r"^/ordermeja(@\w+)?", "", text, flags=re.IGNORECASE).strip()

    # Baris pertama = nomor meja, sisanya = teks order
    lines = body.split("\n", 1)
    table_no = lines[0].strip().upper()
    order_text = lines[1].strip() if len(lines) > 1 else ""

    if not table_no:
        await update.message.reply_text(
            "Format:\n"
            "/ordermeja NOMOR\n"
            "NAMA: MENU\n\n"
            "Contoh:\n"
            "/ordermeja A1\n"
            "Budi: NASI GORENG\n"
            "Siti: ES TEH"
        )
        return

    if not order_text:
        await update.message.reply_text(
            f"Nomor meja {table_no} diterima, tapi teks order kosong.\n\n"
            "Format:\n"
            f"/ordermeja {table_no}\n"
            "Budi: NASI GORENG\n"
            "Siti: ES TEH"
        )
        return

    # Pastikan meja ada
    tables = {t["table_no"] for t in dinein_mgr.list_tables()}
    if table_no not in tables:
        await update.message.reply_text(
            f"Meja {table_no} tidak ditemukan.\n"
            "Gunakan /daftarmeja untuk melihat daftar meja.\n"
            "Admin bisa buat meja baru dengan /buatmeja."
        )
        return

    username = ""
    if update.effective_user:
        username = update.effective_user.username or update.effective_user.full_name

    chat_id = _chat_id(update)

    # Parse order (di thread terpisah supaya tidak blokir)
    reply = await asyncio.to_thread(
        adapter.parse_message,
        order_text,
        username,
        chat_id,
    )

    # Ambil hasil parse dan masukkan ke sesi meja
    last_summary = adapter.get_last_order(chat_id)
    if last_summary and last_summary.invoices:
        session_id, table_summary = dinein_mgr.get_or_create_session(table_no)
        merged = _merge_summaries(table_summary, last_summary)
        dinein_mgr.update_session(session_id, merged)
        total_str = f"{merged.grand_total_riel:,.0f}".replace(",", ".")
        footer = (
            f"\n\n🪑 Masuk ke Meja {table_no} | "
            f"Total meja: {total_str} Riel"
        )
    else:
        footer = f"\n\n⚠️ Order tidak berhasil diparse — tidak disimpan ke Meja {table_no}."

    await update.message.reply_text(reply + footer, reply_markup=_order_keyboard())


async def buatmeja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: buat meja dine-in baru."""
    if update.message is None:
        return
    if await _reject_non_admin(update):
        return
    if _require_dinein_mgr(update):
        await update.message.reply_text("Database tidak tersedia.")
        return

    text = update.message.text or ""
    table_no = re.sub(r"^/buatmeja(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    if not table_no:
        await update.message.reply_text(
            "Format: /buatmeja NOMOR\n\nContoh:\n/buatmeja 1\n/buatmeja A1\n/buatmeja VIP"
        )
        return

    ok, msg = dinein_mgr.create_table(table_no)
    await update.message.reply_text(msg)


async def hapusmeja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: hapus meja dine-in."""
    if update.message is None:
        return
    if await _reject_non_admin(update):
        return
    if _require_dinein_mgr(update):
        await update.message.reply_text("Database tidak tersedia.")
        return

    text = update.message.text or ""
    table_no = re.sub(r"^/hapusmeja(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    if not table_no:
        await update.message.reply_text(
            "Format: /hapusmeja NOMOR\n\nContoh: /hapusmeja A1\n\n"
            "Gunakan /daftarmeja untuk melihat daftar meja."
        )
        return

    ok, msg = dinein_mgr.delete_table(table_no)
    await update.message.reply_text(msg)


async def daftarmeja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan semua meja dine-in dan statusnya."""
    if update.message is None:
        return
    if _require_dinein_mgr(update):
        await update.message.reply_text("Database tidak tersedia.")
        return

    tables = dinein_mgr.list_tables()
    if not tables:
        await update.message.reply_text(
            "Belum ada meja yang dibuat.\n"
            "Admin bisa buat meja baru dengan /buatmeja NOMOR."
        )
        return

    lines = [f"🍽️ DAFTAR MEJA DINE-IN ({len(tables)} meja)\n"]
    for t in tables:
        icon = "🔴 TERISI" if t["active"] else "🟢 KOSONG"
        lines.append(f"• Meja {t['table_no']}  —  {icon}")

    lines += [
        "",
        "Untuk melayani meja: /dinein NOMOR",
        "Untuk lihat tagihan: /tagihan NOMOR",
    ]
    await update.message.reply_text("\n".join(lines))


async def dinein(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set meja dine-in aktif untuk chat ini. Order berikutnya masuk ke meja."""
    if update.message is None:
        return
    if _require_dinein_mgr(update):
        await update.message.reply_text("Database tidak tersedia.")
        return

    text = update.message.text or ""
    table_no = re.sub(r"^/dinein(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    if not table_no:
        chat_id = _chat_id(update)
        active = dinein_mgr.get_active_table(chat_id)
        if active:
            await update.message.reply_text(
                f"🪑 Chat ini sedang melayani Meja {active}.\n\n"
                f"Kirim teks order seperti biasa untuk menambah ke meja ini.\n"
                f"Gunakan /tagihan untuk lihat tagihan.\n"
                f"Gunakan /bayar untuk selesaikan dan tutup meja.\n"
                f"Gunakan /selesaidinein untuk berhenti tanpa bayar."
            )
        else:
            await update.message.reply_text(
                "Format: /dinein NOMOR\n\nContoh: /dinein A1\n\n"
                "Gunakan /daftarmeja untuk melihat daftar meja."
            )
        return

    chat_id = _chat_id(update)
    ok, result = dinein_mgr.set_active_table(chat_id, table_no)
    if not ok:
        await update.message.reply_text(result)
        return

    # Buka atau lanjutkan sesi meja
    session_id, summary = dinein_mgr.get_or_create_session(result)
    has_orders = bool(summary.invoices)

    if has_orders:
        total_str = f"{summary.grand_total_riel:,.0f}".replace(",", ".")
        await update.message.reply_text(
            f"🪑 Meja {result} — lanjut sesi yang sudah ada.\n"
            f"Total sejauh ini: {total_str} Riel\n\n"
            f"Kirim teks order untuk tambah pesanan.\n"
            f"Gunakan /tagihan untuk lihat detail.\n"
            f"Gunakan /bayar untuk selesaikan."
        )
    else:
        await update.message.reply_text(
            f"🪑 Meja {result} — sesi dine-in dimulai!\n\n"
            f"Kirim teks order seperti biasa untuk mencatat pesanan.\n"
            f"Gunakan /tagihan untuk lihat tagihan.\n"
            f"Gunakan /bayar untuk selesaikan dan tutup meja."
        )


async def selesaidinein(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Keluar dari mode dine-in tanpa menutup sesi (tanpa bayar)."""
    if update.message is None:
        return

    chat_id = _chat_id(update)
    if dinein_mgr is None:
        await update.message.reply_text("Database tidak tersedia.")
        return

    active = dinein_mgr.get_active_table(chat_id)
    if not active:
        await update.message.reply_text(
            "Chat ini tidak sedang dalam mode dine-in."
        )
        return

    dinein_mgr.clear_active_table(chat_id)
    await update.message.reply_text(
        f"✅ Keluar dari mode dine-in Meja {active}.\n"
        f"Sesi meja tetap terbuka -- tagihan bisa dilihat dengan /tagihan {active}.\n"
        f"Gunakan /dinein {active} untuk kembali melayani meja ini."
    )


async def tagihan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lihat tagihan sesi aktif meja."""
    if update.message is None:
        return
    if dinein_mgr is None:
        await update.message.reply_text("Database tidak tersedia.")
        return

    text = update.message.text or ""
    arg = re.sub(r"^/tagihan(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    chat_id = _chat_id(update)
    table_no = arg.upper() if arg else dinein_mgr.get_active_table(chat_id)

    if not table_no:
        await update.message.reply_text(
            "Format: /tagihan NOMOR\n\nAtau gunakan /dinein NOMOR dulu, "
            "lalu /tagihan tanpa argumen."
        )
        return

    result = dinein_mgr.get_session(table_no)
    if result is None:
        await update.message.reply_text(
            f"Meja {table_no} tidak memiliki sesi aktif.\n"
            "Gunakan /dinein untuk memulai sesi."
        )
        return

    session_id, summary = result
    if not summary.invoices:
        await update.message.reply_text(
            f"Meja {table_no} belum ada pesanan.\n"
            "Kirim teks order untuk mencatat pesanan."
        )
        return

    row = adapter.store.get_active_dine_in_session(table_no)
    started_at = row["started_at"] if row else ""
    bill_text = _format_table_bill(table_no, summary, started_at)
    await update.message.reply_text(
        bill_text,
        reply_markup=_table_action_keyboard(table_no, True),
    )


async def bayar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tandai meja sudah bayar dan tutup sesi dine-in."""
    if update.message is None:
        return
    if dinein_mgr is None:
        await update.message.reply_text("Database tidak tersedia.")
        return

    text = update.message.text or ""
    arg = re.sub(r"^/bayar(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    chat_id = _chat_id(update)
    table_no = arg.upper() if arg else dinein_mgr.get_active_table(chat_id)

    if not table_no:
        await update.message.reply_text(
            "Format: /bayar NOMOR\n\nContoh: /bayar A1\n\n"
            "Atau gunakan /dinein NOMOR dulu, lalu /bayar tanpa argumen."
        )
        return

    summary = dinein_mgr.close_session(table_no)
    if summary is None:
        await update.message.reply_text(
            f"Meja {table_no} tidak memiliki sesi aktif."
        )
        return

    total_str = f"{summary.grand_total_riel:,.0f}".replace(",", ".")
    kb = _table_select_keyboard()
    await update.message.reply_text(
        f"✅ Meja {table_no} — LUNAS!\n"
        f"Total yang dibayar: {total_str} Riel\n\n"
        f"Meja sekarang kosong dan siap untuk tamu berikutnya.",
        reply_markup=kb,
    )


async def invoicemeja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kirim invoice meja: PNG + PDF sekaligus."""
    if update.message is None:
        return
    if dinein_mgr is None:
        await update.message.reply_text("Database tidak tersedia.")
        return

    text = update.message.text or ""
    arg = re.sub(r"^/(invoicemeja|strukmeja)(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()

    chat_id = _chat_id(update)
    table_no = arg.upper() if arg else dinein_mgr.get_active_table(chat_id)

    if not table_no:
        await update.message.reply_text(
            "Format: /invoicemeja NOMOR\n\nContoh: /invoicemeja IN1"
        )
        return

    result = dinein_mgr.get_session(table_no)
    if result is None:
        await update.message.reply_text(
            f"Meja {table_no} tidak memiliki sesi aktif."
        )
        return

    _, summary = result
    if not summary.invoices:
        await update.message.reply_text(f"Meja {table_no} belum ada pesanan.")
        return

    await _send_invoice_bundle(
        update.message,
        summary,
        f"invoice_meja{table_no}",
        reply_markup=_table_action_keyboard(table_no, True),
    )


async def invoice_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kirim invoice order terakhir: PNG + PDF sekaligus."""
    chat_id = _chat_id(update)
    summary = adapter.get_last_order(chat_id)

    if not summary or not summary.invoices:
        await update.message.reply_text(
            "Belum ada order yang diproses di chat ini. "
            "Kirim dulu teks order-nya."
        )
        return

    await _send_invoice_bundle(update.message, summary, "invoice")


# ==========================================================
# MESSAGE HANDLER
# ==========================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message is None:
        return

    text = update.message.text

    if not text:
        return

    username = ""

    if update.effective_user:
        username = update.effective_user.username or (
            update.effective_user.full_name
        )

    chat_id = _chat_id(update)

    info(f"Message dari {username}")

    location_reply = adapter.handle_location_reply(text=text, chat_id=chat_id)

    if location_reply is not None:
        await update.message.reply_text(
            location_reply, reply_markup=_order_keyboard()
        )
        return

    # Dijalankan di thread terpisah: kalau lapisan AI aktif, satu order
    # bisa makan beberapa detik -- jangan sampai memblokir bot menangani
    # pesan dari chat lain selama itu.
    reply = await asyncio.to_thread(
        adapter.parse_message,
        text,
        username,
        chat_id,
    )

    # Tentukan meja tujuan: pending (dari tombol) > sticky (/dinein)
    active_table = _pending_table.pop(chat_id, None)
    if active_table is None and dinein_mgr:
        active_table = dinein_mgr.get_active_table(chat_id)

    if active_table and dinein_mgr:
        last_summary = adapter.get_last_order(chat_id)
        if last_summary and last_summary.invoices:
            session_id, table_summary = dinein_mgr.get_or_create_session(active_table)
            merged = _merge_summaries(table_summary, last_summary)
            dinein_mgr.update_session(session_id, merged)
            total_str = f"{merged.grand_total_riel:,.0f}".replace(",", ".")
            reply += (
                f"\n\n🪑 Masuk ke Meja {active_table} | "
                f"Total meja: {total_str} Riel"
            )
            # Tampilkan tombol aksi meja (bukan tombol order biasa)
            await update.message.reply_text(
                reply,
                reply_markup=_table_action_keyboard(active_table, True),
            )
        else:
            # Order gagal parse — tetap tampilkan reply, kembalikan pending
            await update.message.reply_text(reply, reply_markup=_order_keyboard())
    else:
        await update.message.reply_text(reply, reply_markup=_order_keyboard())

    if FOREIGN_RESTO_CHAT_ID:
        foreign_block = adapter.get_last_foreign_block(chat_id)

        if foreign_block:
            try:
                await context.bot.send_message(
                    chat_id=FOREIGN_RESTO_CHAT_ID,
                    text=foreign_block,
                )
                await update.message.reply_text(
                    "✅ Pesanan resto lain otomatis diteruskan."
                )
            except Exception as e:
                error(f"Gagal forward ke resto lain: {e!r}")
                await update.message.reply_text(
                    "⚠️ Gagal meneruskan otomatis ke resto lain -- "
                    "tolong copy-paste manual blok di atas."
                )


# ==========================================================
# CALLBACK QUERY (tombol interaktif)
# ==========================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query is None:
        return

    await query.answer()

    chat_id = _chat_id(update)
    data = query.data or ""

    # "order_struk" tetap diterima sebagai alias supaya tombol Struk
    # di pesan-pesan lama tidak mati -- keduanya kirim PNG + PDF.
    if data in ("order_invoice", "order_struk"):
        summary = adapter.get_last_order(chat_id)

        if not summary or not summary.invoices:
            await query.message.reply_text(
                "Belum ada order yang diproses di chat ini."
            )
            return

        await _send_invoice_bundle(query.message, summary, "invoice")

    elif data == "order_tambah":
        await query.message.reply_text(
            "Kirim /tambah diikuti menu tambahannya, contoh:\n\n"
            "/tambah\nNASI GORENG: 12.000R\nBudi"
        )

    elif data == "order_hapus":
        await query.message.reply_text(
            "Kirim /hapus diikuti nama menu, contoh:\n\n"
            "/hapus katsu curry\n"
            "/hapus katsu curry budi"
        )

    elif data == "order_ganti":
        await query.message.reply_text(
            "Kirim /ganti diikuti menu baru, contoh:\n\n"
            "/ganti nasi ayam kalasan rama\n"
            "/ganti bubur ayam jadi nasi ayam kalasan rama"
        )

    elif data == "order_cekai":
        result = adapter.check_with_ai(chat_id)
        await query.message.reply_text(result)

    elif data.startswith("dinein_table:"):
        table_no = data.split(":", 1)[1]
        if dinein_mgr is None:
            await query.answer("Database tidak tersedia.")
            return

        # Selalu tampilkan halaman manajemen meja —
        # dengan pesanan (kalau ada) + keyboard aksi lengkap.
        result = dinein_mgr.get_session(table_no)
        has_orders = bool(result and result[1].invoices)

        if has_orders:
            _, summary = result
            row = adapter.store.get_active_dine_in_session(table_no)
            started_at = row["started_at"] if row else ""
            header = _format_table_bill(table_no, summary, started_at)
        else:
            header = f"🪑 MEJA {table_no}\n\nBelum ada pesanan."

        try:
            await query.edit_message_text(
                header + "\n\n— Pilih aksi —",
                reply_markup=_table_action_keyboard(table_no, has_orders),
            )
        except Exception:
            await query.message.reply_text(
                header + "\n\n— Pilih aksi —",
                reply_markup=_table_action_keyboard(table_no, has_orders),
            )

    elif data.startswith("dinein_action:"):
        parts = data.split(":")
        table_no = parts[1] if len(parts) > 1 else ""
        action   = parts[2] if len(parts) > 2 else ""
        if not table_no or dinein_mgr is None:
            await query.answer("Data tidak valid.")
            return

        if action == "add":
            _pending_table[chat_id] = table_no
            try:
                await query.edit_message_text(
                    f"🪑 Meja {table_no} — Tambah Order\n\nKirim teks order sekarang:"
                )
            except Exception:
                await query.message.reply_text(
                    f"🪑 Meja {table_no} — kirim teks order:"
                )

        elif action == "view":
            result = dinein_mgr.get_session(table_no)
            if result is None:
                await query.answer("Tidak ada sesi aktif.")
                return
            _, summary = result
            row = adapter.store.get_active_dine_in_session(table_no)
            started_at = row["started_at"] if row else ""
            bill_text = _format_table_bill(table_no, summary, started_at)
            try:
                await query.edit_message_text(
                    bill_text + "\n\nPilih aksi:",
                    reply_markup=_table_action_keyboard(table_no, bool(summary.invoices)),
                )
            except Exception:
                await query.message.reply_text(bill_text)

        elif action == "remove":
            kb = _table_remove_keyboard(table_no)
            if kb is None:
                await query.answer("Tidak ada item untuk dihapus.")
                return
            try:
                await query.edit_message_text(
                    f"🗑️ Pilih item yang ingin dihapus dari Meja {table_no}:\n"
                    "(Klik item untuk langsung menghapusnya)",
                    reply_markup=kb,
                )
            except Exception:
                await query.message.reply_text(
                    f"🗑️ Pilih item yang ingin dihapus dari Meja {table_no}:",
                    reply_markup=kb,
                )

        elif action == "pay":
            summary = dinein_mgr.close_session(table_no)
            if summary is None:
                await query.answer("Tidak ada sesi aktif.")
                return
            total_str = f"{summary.grand_total_riel:,.0f}".replace(",", ".")
            try:
                await query.edit_message_text(
                    f"✅ Meja {table_no} — LUNAS!\n"
                    f"Total dibayar: {total_str} Riel\n\n"
                    "Meja sekarang kosong dan siap untuk tamu berikutnya."
                )
            except Exception:
                await query.message.reply_text(
                    f"✅ Meja {table_no} — LUNAS! Total: {total_str} Riel"
                )

        elif action in ("invoice", "struk"):
            # "struk" = alias tombol lama di pesan yang sudah terkirim
            result = dinein_mgr.get_session(table_no)
            if result is None:
                await query.answer("Tidak ada sesi aktif.")
                return
            _, summary = result
            if not summary.invoices:
                await query.answer("Belum ada pesanan di meja ini.")
                return
            await _send_invoice_bundle(
                query.message,
                summary,
                f"invoice_meja{table_no}",
                reply_markup=_table_action_keyboard(table_no, True),
            )

    elif data.startswith("dinein_remove:"):
        parts = data.split(":")
        table_no = parts[1] if len(parts) > 1 else ""
        flat_idx = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
        if not table_no or flat_idx < 0 or dinein_mgr is None:
            await query.answer("Data tidak valid.")
            return

        ok, updated_summary, item_name_or_err = dinein_mgr.remove_item_from_session(
            table_no, flat_idx
        )
        if not ok:
            await query.answer(f"Gagal: {item_name_or_err}")
            return

        await query.answer(f"✅ {item_name_or_err} dihapus.")

        if updated_summary and updated_summary.invoices:
            row = adapter.store.get_active_dine_in_session(table_no)
            started_at = row["started_at"] if row else ""
            bill_text = _format_table_bill(table_no, updated_summary, started_at)
            try:
                await query.edit_message_text(
                    bill_text + "\n\nPilih aksi:",
                    reply_markup=_table_action_keyboard(table_no, True),
                )
            except Exception:
                await query.message.reply_text(
                    bill_text,
                    reply_markup=_table_action_keyboard(table_no, True),
                )
        else:
            try:
                await query.edit_message_text(
                    f"✅ Semua item dihapus. Meja {table_no} sekarang kosong.\n\n"
                    "Kirim order baru kapan saja.",
                    reply_markup=_table_action_keyboard(table_no, False),
                )
            except Exception:
                await query.message.reply_text(
                    f"✅ Meja {table_no} sekarang kosong.",
                    reply_markup=_table_action_keyboard(table_no, False),
                )

    elif data == "dinein_back":
        kb = _table_select_keyboard()
        if kb is None:
            try:
                await query.edit_message_text("Tidak ada meja yang terdaftar.")
            except Exception:
                await query.message.reply_text("Tidak ada meja yang terdaftar.")
            return
        try:
            await query.edit_message_text(
                "🍽️ Pilih meja untuk memasukkan order:\n"
                "🟢 = kosong  |  🔴 = terisi",
                reply_markup=kb,
            )
        except Exception:
            await query.message.reply_text(
                "🍽️ Pilih meja:",
                reply_markup=kb,
            )

    elif data == "dinein_cancel":
        _pending_table.pop(chat_id, None)
        try:
            await query.edit_message_text("❌ Pemilihan meja dibatalkan.")
        except Exception:
            await query.message.reply_text("❌ Pemilihan meja dibatalkan.")


# ==========================================================
# TEST AI (admin only)
# ==========================================================

async def testai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cek koneksi AI dan tampilkan error spesifik kalau gagal."""
    if update.message is None:
        return

    if await _reject_non_admin(update):
        return

    ai = adapter.ai_parser

    if ai is None or not ai.available:
        await update.message.reply_text(
            "❌ AI tidak aktif.\n"
            "Pastikan GROQ_API_KEY atau GEMINI_API_KEY sudah diisi di .env"
        )
        return

    await update.message.reply_text(
        f"🔄 Mencoba koneksi ke {ai.provider} (model: {ai.model})..."
    )

    result = ai.parse("tes koneksi ai")
    if result is not None:
        await update.message.reply_text(
            f"✅ AI ({ai.provider}) berhasil terhubung!\nModel: {ai.model}"
        )
    else:
        err = ai.last_error or "error tidak diketahui"
        await update.message.reply_text(
            f"❌ AI ({ai.provider}) gagal:\n\n{err}\n\nModel dicoba: {ai.model}"
        )


# ==========================================================
# ERROR HANDLER
# ==========================================================

async def telegram_error(update, context):

    error(str(context.error))


# ==========================================================
# MAIN
# ==========================================================

def main():

    startup(f"{APP_NAME} v{BOT_VERSION} starting...")

    pos_port = start_pos_server()

    # Sambungkan OrderStore ke keepalive store (dipakai komponen lain jika ada)
    if adapter.store is not None:
        set_order_store(adapter.store)

    if pos_port:
        startup(f"POS Web aktif di http://0.0.0.0:{pos_port}/pos")

    try:

        try:
            asyncio.get_running_loop()

        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

        application = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .build()
        )

        application.add_error_handler(
            telegram_error
        )

        application.add_handler(
            CommandHandler("start", start)
        )

        application.add_handler(
            CommandHandler("help", help_command)
        )

        application.add_handler(
            CommandHandler("version", version)
        )

        application.add_handler(
            CommandHandler("ping", ping)
        )

        application.add_handler(
            CommandHandler("parser", parser)
        )

        application.add_handler(
            CommandHandler("stat", stat)
        )

        application.add_handler(
            CommandHandler("health", health)
        )

        application.add_handler(
            # /struk lama dialihkan ke invoice (PNG + PDF)
            CommandHandler("struk", invoice_pdf)
        )

        application.add_handler(
            CommandHandler("invoice", invoice_pdf)
        )

        application.add_handler(
            CommandHandler("tambah", tambah)
        )

        application.add_handler(
            CommandHandler("hapus", hapus)
        )

        application.add_handler(
            CommandHandler("ganti", ganti)
        )

        application.add_handler(
            CommandHandler("pakaiai", pakaiai)
        )

        application.add_handler(
            CommandHandler("lihatpreorder", lihatpreorder)
        )

        application.add_handler(
            CommandHandler("batalpreorder", batalpreorder)
        )

        application.add_handler(
            CommandHandler("backup", backup)
        )

        application.add_handler(
            CommandHandler("tambahmenu", tambahmenu)
        )

        application.add_handler(
            CommandHandler("hapusmenu", hapusmenu)
        )

        application.add_handler(
            CommandHandler("updatemenu", updatemenu)
        )

        application.add_handler(
            CommandHandler("daftarmenu", daftarmenu)
        )

        application.add_handler(
            CommandHandler("exportmenu", exportmenu)
        )

        application.add_handler(
            CommandHandler("importmenu", importmenu)
        )

        application.add_handler(
            CommandHandler("testai", testai)
        )

        application.add_handler(
            CommandHandler("readymenu", readymenu)
        )

        application.add_handler(
            CommandHandler("notready", notready)
        )

        application.add_handler(
            CommandHandler("lihatready", lihatready)
        )

        application.add_handler(
            CommandHandler("resetready", resetready)
        )

        application.add_handler(
            CommandHandler("pesanmeja", pesanmeja)
        )

        application.add_handler(
            CommandHandler("ordermeja", ordermeja)
        )

        application.add_handler(
            CommandHandler("daftarmeja", daftarmeja)
        )

        application.add_handler(
            CommandHandler("dinein", dinein)
        )

        application.add_handler(
            CommandHandler("selesaidinein", selesaidinein)
        )

        application.add_handler(
            CommandHandler("tagihan", tagihan)
        )

        application.add_handler(
            CommandHandler("bayar", bayar)
        )

        application.add_handler(
            # /strukmeja lama dialihkan ke invoice meja (PNG + PDF)
            CommandHandler("strukmeja", invoicemeja)
        )

        application.add_handler(
            CommandHandler("invoicemeja", invoicemeja)
        )

        application.add_handler(
            CommandHandler("setdiskon", setdiskon)
        )

        application.add_handler(
            CommandHandler("hapusdiskon", hapusdiskon)
        )

        application.add_handler(
            CommandHandler("daftardiskon", daftardiskon)
        )

        application.add_handler(
            CommandHandler("tutor", tutor)
        )

        application.add_handler(
            CommandHandler("daftargrup", daftargrup)
        )

        application.add_handler(
            CommandHandler("keluargrup", keluargrup)
        )

        application.add_handler(
            CommandHandler("listgrup", listgrup)
        )

        application.add_handler(
            CommandHandler("broadcast", broadcast_cmd)
        )

        # Job harian: proses pre-order jam 07:00 WIB
        if application.job_queue is not None:
            application.job_queue.run_daily(
                proses_preorder_harian,
                time=dt_time(hour=7, minute=0, tzinfo=JAKARTA_TZ),
            )
            startup("Job pre-order harian aktif (07:00 WIB)")

        # Backup DB otomatis tiap malam ke chat admin (kalau diaktifkan)
        if ADMIN_CHAT_IDS:
            if application.job_queue is not None:
                application.job_queue.run_daily(
                    backup_harian,
                    time=dt_time(hour=22, minute=0, tzinfo=JAKARTA_TZ),
                )
                startup("Backup harian aktif (22:00 WIB ke chat admin)")
            else:
                warning(
                    "ADMIN_CHAT_ID diisi tapi JobQueue tidak tersedia -- "
                    "install 'python-telegram-bot[job-queue]' supaya "
                    "backup harian jalan. /backup manual tetap bisa."
                )

        application.add_handler(
            CallbackQueryHandler(handle_callback)
        )

        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                handle_message,
            )
        )

        print("=" * 45)
        print(APP_NAME)
        print(f"Version : {BOT_VERSION}")

        if adapter.ai_parser is not None and adapter.ai_parser.available:
            print(
                f"Lapisan AI : AKTIF ({adapter.ai_parser.provider}, "
                f"model {adapter.ai_parser.model})"
            )
        else:
            print(
                "Lapisan AI : nonaktif (isi GROQ_API_KEY atau "
                "GEMINI_API_KEY untuk mengaktifkan)"
            )

        print("=" * 45)

        adapter.print_startup_banner()

        startup("Bot mulai polling Telegram")

        application.run_polling(
            drop_pending_updates=True
        )

    except KeyboardInterrupt:

        warning("Bot dihentikan oleh user.")

    except Exception as e:

        error(str(e))
        raise


# ==========================================================
# HEALTH CHECK SERVER (untuk Render type:web health check)
# ==========================================================

def _start_health_server() -> None:
    """HTTP server minimal di background thread.

    Render (type:web) membutuhkan aplikasi yang mendengarkan port
    agar health check tidak gagal dengan Bad Gateway. Bot polling
    tidak perlu HTTP, tapi server ini cukup menjawab 200 OK.
    """
    port = int(os.getenv("PORT", "10000"))

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Madam Lily Bot is running.")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, fmt, *args):
            pass  # suppress HTTP access logs

    class _ReuseAddrHTTPServer(HTTPServer):
        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            super().server_bind()

    try:
        server = _ReuseAddrHTTPServer(("0.0.0.0", port), _Handler)
        info(f"Health check server listening on port {port}")
        server.serve_forever()
    except OSError as e:
        warning(f"Health check server gagal bind port {port}: {e}. Bot tetap jalan.")
        # Jangan crash, bot tetap bisa berjalan via polling


# ==========================================================
# ENTRY
# ==========================================================

if __name__ == "__main__":
    # Jalankan health check server di background sebelum bot mulai
    threading.Thread(target=_start_health_server, daemon=True).start()
    main()
