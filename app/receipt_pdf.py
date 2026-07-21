"""
receipt_pdf.py

Generate invoice A4 (PDF) berisi seluruh customer dalam satu tabel
(Menu/Qty/Harga/Nama Pemesan), ditutup grand total. Kolom Menu diawali
emoji sesuai jenis menu (atau "-" untuk item tanpa emoji/extras), dan
kata "NASI UDUK" di-highlight kuning supaya gampang dibedakan dari
varian biasa -- mengikuti pola bon manual yang dipakai staf.
"""

import xml.sax.saxutils as saxutils
from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

try:
    from .timezone_utils import now_jakarta
except ImportError:
    from timezone_utils import now_jakarta

HEADER_BG = colors.HexColor("#2c3e50")
CUSTOMER_BG = colors.HexColor("#eef1f4")
CELL_H_PADDING = 6  # LEFTPADDING/RIGHTPADDING (pt) di TableStyle di bawah
COL_WIDTHS = [8.6 * cm, 1.3 * cm, 2.8 * cm, 5.5 * cm]

EMOJI_DIR = Path(__file__).parent / "assets" / "emoji"
EMOJI_RESERVED_WIDTH = 16  # pt -- lebar ikon emoji (13pt) + jarak

# Nama menu diusahakan selalu muat 1 baris: mulai dari ukuran terbesar,
# turun sedikit demi sedikit sampai muat di lebar kolom Menu. Nama yang
# sangat panjang (mis. "NASI UDUK KOMPLIT+AYAM KALASAN PAHA ATAS") jadi
# lebih kecil, tapi nama biasa tetap besar seperti biasa.
MENU_FONT_MAX = 11
MENU_FONT_STEPS = [11, 10.5, 10, 9.5, 9, 8.5, 8]

# Prefix nama menu yang di-highlight kuning supaya gampang dibedakan
# dari varian biasa (mis. "NASI UDUK+AYAM KREMES DADA" vs
# "NASI+AYAM KREMES DADA").
HIGHLIGHT_PREFIX = "NASI UDUK"

# Urutan baris di tabel invoice SELALU mengikuti urutan kategori ini
# (bukan urutan customer mengetik order-nya), supaya dapur gampang
# kerja dari 1 blok kategori ke blok berikutnya.
CATEGORY_ORDER = [
    "HOKI-HOKI BENTO",
    "PAKET HOKI",
    "ALA CARTE (TANPA NASI)",
    "BATAGOR",
    "AYAM GORENG KALASAN",
    "EXTRAS",
    "NASI UDUK",
    "BUBUR AYAM",
    "SOTO BETAWI",
    "BAKSO SOLO",
    "SOTOMIE",
    "JAJANAN PASAR",
    "MINUMAN",
]
CATEGORY_PRIORITY = {name: i for i, name in enumerate(CATEGORY_ORDER)}
DEFAULT_CATEGORY_PRIORITY = len(CATEGORY_ORDER)


def _invoice_priority(invoice):
    if not invoice.items:
        return DEFAULT_CATEGORY_PRIORITY

    return min(
        CATEGORY_PRIORITY.get(item.category, DEFAULT_CATEGORY_PRIORITY)
        for item in invoice.items
    )


def _sorted_rows(order_summary):
    """
    Satu customer (invoice) SELALU jadi satu blok utuh -- tidak dipecah
    walau item-nya beda kategori (mis. customer pesan menu bento
    sekaligus jajanan pasar, keduanya tetap nempel jadi 1 blok). Posisi
    blok mengikuti kategori PALING AWAL di antara item customer itu,
    supaya urutan keseluruhan tabel tetap mengikuti CATEGORY_ORDER.
    """

    rows = []

    for invoice in sorted(order_summary.invoices, key=_invoice_priority):
        # Order polos yang tidak menyebut nama customer sama sekali
        # ditandai "UNKNOWN" secara internal -- di invoice ini dibiarkan
        # KOSONG (bukan diisi nama pemesan keseluruhan) supaya kolom
        # Nama Pemesan cuma terisi kalau memang ada nama per-item.
        name = "" if (invoice.telegram_name or "").strip().upper() == "UNKNOWN" else (invoice.telegram_name or "").upper()

        for item in invoice.items:
            rows.append((item, name))

    return rows


def _emoji_tag(emoji_char: str, size: int = 13) -> str:
    if not emoji_char:
        return ""

    codepoint = "-".join(f"{ord(c):x}" for c in emoji_char)
    path = EMOJI_DIR / f"{codepoint}.png"

    if not path.exists():
        return ""

    return f'<img src="{path}" width="{size}" height="{size}" valign="middle"/> '


def _fit_menu_font_size(display_name: str, has_emoji: bool, available_pt: float) -> float:
    """Cari ukuran font terbesar (dari MENU_FONT_STEPS) supaya emoji/"-"
    + nama menu itu muat dalam 1 baris di lebar kolom Menu."""

    for size in MENU_FONT_STEPS:
        prefix_w = EMOJI_RESERVED_WIDTH if has_emoji else stringWidth("- ", "Helvetica", size)
        name_w = stringWidth(display_name, "Helvetica", size)

        if prefix_w + name_w <= available_pt:
            return size

    return MENU_FONT_STEPS[-1]


def _menu_cell(item, style, menu_col_width: float) -> Paragraph:
    has_emoji = bool(item.emoji) and _emoji_tag(item.emoji) != ""
    available_pt = menu_col_width - (2 * CELL_H_PADDING)
    font_size = _fit_menu_font_size(item.menu, has_emoji, available_pt)

    name = saxutils.escape(item.menu)

    if (item.menu or "").upper().startswith(HIGHLIGHT_PREFIX):
        cut = len(HIGHLIGHT_PREFIX)
        name = (
            f'<font backColor="yellow">{saxutils.escape(item.menu[:cut])}</font>'
            f"{saxutils.escape(item.menu[cut:])}"
        )

    prefix = _emoji_tag(item.emoji) if has_emoji else "- "
    text = f"{prefix}{name}"

    # Catatan ditaruh di baris baru di bawah nama menu (bukan menempel
    # di belakang nama dalam 1 baris), huruf besar semua & tebal supaya
    # gampang dilihat dapur.
    if item.note:
        text += f"<br/><b>{saxutils.escape((item.note or '').upper())}</b>"

    cell_style = ParagraphStyle(
        f"MenuCell{font_size}",
        parent=style,
        fontSize=font_size,
        leading=font_size * 1.3,
    )

    return Paragraph(text, cell_style)


def _build_elements(order_summary, shop_name, styles):
    title_style = styles["Title"]
    title_style.fontSize = 20
    title_style.spaceAfter = 4

    date_style = ParagraphStyle(
        "DateLine", parent=styles["Normal"], fontSize=12, spaceAfter=2, leading=15
    )

    menu_cell_style = ParagraphStyle(
        "MenuCell", parent=styles["Normal"], fontSize=11, leading=14
    )

    elements = [
        Paragraph(shop_name, title_style),
        Paragraph(now_jakarta().strftime("%d %B %Y, %H:%M"), date_style),
    ]

    if order_summary.destination:
        elements.append(Paragraph(f"Lokasi: {order_summary.destination}", date_style))

    if order_summary.orderer_name:
        elements.append(Paragraph(f"Pemesan: {order_summary.orderer_name}", date_style))

    elements.append(Spacer(1, 6))

    data = [["Menu", "Qty", "Harga", "Nama Pemesan"]]
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("ALIGN", (1, 0), (2, -1), "RIGHT"),
        ("ALIGN", (3, 0), (3, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), CELL_H_PADDING),
        ("RIGHTPADDING", (0, 0), (-1, -1), CELL_H_PADDING),
        ("LINEBELOW", (0, 0), (-1, 0), 1, HEADER_BG),
        # Nama pemesan dibuat tebal supaya jelas terbaca.
        ("FONTNAME", (3, 1), (3, -1), "Helvetica-Bold"),
    ]

    row = 1
    shade = False
    previous_name = None

    for item, name in _sorted_rows(order_summary):
        if name != previous_name:
            shade = not shade
            previous_name = name

        band = CUSTOMER_BG if shade else colors.white

        eff_price = max(0, item.price - getattr(item, "discount_riel", 0))
        data.append(
            [
                _menu_cell(item, menu_cell_style, COL_WIDTHS[0]),
                str(item.qty),
                f"{eff_price * item.qty:,.0f}",
                name,
            ]
        )
        commands.append(("BACKGROUND", (0, row), (-1, row), band))

        # Qty > 1: bold + warna oranye supaya mudah terlihat di invoice
        if item.qty > 1:
            commands.append(("FONTNAME", (1, row), (1, row), "Helvetica-Bold"))
            commands.append(("TEXTCOLOR", (1, row), (1, row), colors.HexColor("#d35400")))

        row += 1

    # GRAND TOTAL jadi baris terakhir tabel (bukan teks terpisah di
    # bawahnya), 1 baris menyatu di seluruh kolom supaya jelas ini
    # penutup tabel.
    grand_total_style = ParagraphStyle(
        "GrandTotal",
        parent=styles["Normal"],
        fontSize=13,
        leading=16,
        fontName="Helvetica-Bold",
        textColor=colors.white,
        alignment=1,
    )
    grand_total_text = (
        f"GRAND TOTAL : {order_summary.grand_total_riel:,.0f} Riel "
        f"(${order_summary.grand_total_usd:,.2f})"
    )
    data.append([Paragraph(grand_total_text, grand_total_style), "", "", ""])
    commands.append(("SPAN", (0, row), (-1, row)))
    commands.append(("BACKGROUND", (0, row), (-1, row), HEADER_BG))
    commands.append(("TOPPADDING", (0, row), (-1, row), 8))
    commands.append(("BOTTOMPADDING", (0, row), (-1, row), 8))

    table = Table(data, colWidths=COL_WIDTHS, repeatRows=1)
    table.setStyle(TableStyle(commands))
    elements.append(table)

    return elements


def render_a4_invoice(order_summary, filepath: str, shop_name: str = "MADAM LILY") -> str:
    """
    order_summary: instance OrderSummary dari receipt_data.py
    filepath: path tujuan file .pdf
    """

    doc = SimpleDocTemplate(
        filepath,
        pagesize=A4,
        topMargin=1.2 * cm,
        bottomMargin=1.2 * cm,
        leftMargin=1.2 * cm,
        rightMargin=1.2 * cm,
    )

    styles = getSampleStyleSheet()
    doc.build(_build_elements(order_summary, shop_name, styles))
    return filepath


def render_a4_invoice_bytes(order_summary, shop_name: str = "MADAM LILY") -> BytesIO:
    """Helper: langsung return BytesIO PDF, siap dikirim ke bot.send_document()."""
    buf = BytesIO()
    buf.name = "invoice.pdf"

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        topMargin=1.2 * cm,
        bottomMargin=1.2 * cm,
        leftMargin=1.2 * cm,
        rightMargin=1.2 * cm,
    )
    styles = getSampleStyleSheet()
    doc.build(_build_elements(order_summary, shop_name, styles))

    buf.seek(0)
    return buf
