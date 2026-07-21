"""
invoice_image.py

Render invoice sebagai PNG dengan layout yang SAMA dengan PDF A4
(receipt_pdf.py): judul, tanggal, tabel Menu/Qty/Harga/Nama Pemesan,
banding warna per customer, dan baris GRAND TOTAL sebagai penutup.
Dipakai bot untuk kirim gambar invoice di samping file PDF-nya.
"""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    from .timezone_utils import now_jakarta
    from .receipt_pdf import _sorted_rows, HIGHLIGHT_PREFIX
except ImportError:
    from timezone_utils import now_jakarta
    from receipt_pdf import _sorted_rows, HIGHLIGHT_PREFIX

EMOJI_DIR = Path(__file__).parent / "assets" / "emoji"

# Lebar kanvas (px). Rasio kolom mengikuti COL_WIDTHS di receipt_pdf.
WIDTH = 1080
PAD = 40

HEADER_BG = (44, 62, 80)      # #2c3e50
BAND_BG = (238, 241, 244)     # #eef1f4
ORANGE = (211, 84, 0)          # qty > 1
YELLOW = (255, 235, 59)        # highlight NASI UDUK
WHITE = (255, 255, 255)
BLACK = (20, 20, 20)

# Rasio kolom: Menu, Qty, Harga, Nama Pemesan (mengikuti 8.6/1.3/2.8/5.5 cm)
_COL_RATIOS = [0.47, 0.08, 0.17, 0.28]

_FONT_SIZE = 22
_TITLE_SIZE = 40
_DATE_SIZE = 24
_TOTAL_SIZE = 26
_CELL_PAD_X = 12
_CELL_PAD_Y = 10
_LINE_GAP = 6


def _load_font(size, bold=False):
    try:
        from .font_utils import load_font
    except ImportError:
        from font_utils import load_font

    return load_font(size, bold=bold, mono_preferred=False)


def _wrap(draw, text, font, max_width):
    words = text.split()
    if not words:
        return [""]
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _emoji_img(emoji_char: str, size: int):
    if not emoji_char:
        return None
    codepoint = "-".join(f"{ord(c):x}" for c in emoji_char)
    path = EMOJI_DIR / f"{codepoint}.png"
    if not path.exists():
        return None
    try:
        img = Image.open(path).convert("RGBA")
        return img.resize((size, size))
    except Exception:
        return None


def render_a4_invoice_png_bytes(order_summary, shop_name: str = "MADAM LILY") -> BytesIO:
    """Render invoice PNG (layout tabel seperti PDF A4).

    Return BytesIO siap dikirim lewat bot.send_photo().
    """
    font_title = _load_font(_TITLE_SIZE, bold=True)
    font_date = _load_font(_DATE_SIZE)
    font_normal = _load_font(_FONT_SIZE)
    font_bold = _load_font(_FONT_SIZE, bold=True)
    font_total = _load_font(_TOTAL_SIZE, bold=True)
    font_note = _load_font(_FONT_SIZE - 2, bold=True)

    col_px = [int(r * (WIDTH - PAD * 2)) for r in _COL_RATIOS]
    col_x = [PAD]
    for w in col_px[:-1]:
        col_x.append(col_x[-1] + w)
    menu_text_w = col_px[0] - _CELL_PAD_X * 2 - (_FONT_SIZE + 6)  # ruang prefix emoji/"-"
    name_text_w = col_px[3] - _CELL_PAD_X * 2

    measure = ImageDraw.Draw(Image.new("RGB", (WIDTH, 10), "white"))
    line_h = _FONT_SIZE + _LINE_GAP

    # ---------- PASS 1: hitung tinggi ----------
    rows = []  # (item, name, menu_lines, name_lines, row_h)
    for item, name in _sorted_rows(order_summary):
        menu_lines = _wrap(measure, item.menu, font_normal, menu_text_w)
        note_lines = (
            _wrap(measure, (item.note or "").upper(), font_note, menu_text_w)
            if item.note else []
        )
        name_lines = _wrap(measure, name, font_bold, name_text_w) if name else [""]
        n_lines = max(len(menu_lines) + len(note_lines), len(name_lines), 1)
        row_h = n_lines * line_h + _CELL_PAD_Y * 2
        rows.append((item, name, menu_lines, note_lines, name_lines, row_h))

    header_h = _FONT_SIZE + _CELL_PAD_Y * 2 + 6
    title_block_h = _TITLE_SIZE + 10 + _DATE_SIZE + 10
    if getattr(order_summary, "destination", ""):
        title_block_h += _DATE_SIZE + 6
    if getattr(order_summary, "orderer_name", ""):
        title_block_h += _DATE_SIZE + 6
    total_row_h = _TOTAL_SIZE + _CELL_PAD_Y * 2 + 8

    height = (
        PAD + title_block_h + 16 + header_h
        + sum(r[5] for r in rows) + total_row_h + PAD
    )

    # ---------- PASS 2: gambar ----------
    img = Image.new("RGB", (WIDTH, height), "white")
    draw = ImageDraw.Draw(img)

    y = PAD
    draw.text((PAD, y), shop_name, font=font_title, fill=BLACK)
    y += _TITLE_SIZE + 10
    draw.text((PAD, y), now_jakarta().strftime("%d %B %Y, %H:%M"), font=font_date, fill=BLACK)
    y += _DATE_SIZE + 10
    if getattr(order_summary, "destination", ""):
        draw.text((PAD, y), f"Lokasi: {order_summary.destination}", font=font_date, fill=BLACK)
        y += _DATE_SIZE + 6
    if getattr(order_summary, "orderer_name", ""):
        draw.text((PAD, y), f"Pemesan: {order_summary.orderer_name}", font=font_date, fill=BLACK)
        y += _DATE_SIZE + 6

    y += 16

    # Header tabel
    draw.rectangle([PAD, y, WIDTH - PAD, y + header_h], fill=HEADER_BG)
    headers = ["Menu", "Qty", "Harga", "Nama Pemesan"]
    for i, htext in enumerate(headers):
        tx = col_x[i] + _CELL_PAD_X
        if i in (1, 2):  # Qty & Harga rata kanan
            tw = measure.textlength(htext, font=font_bold)
            tx = col_x[i] + col_px[i] - _CELL_PAD_X - tw
        draw.text((tx, y + _CELL_PAD_Y), htext, font=font_bold, fill=WHITE)
    y += header_h

    # Baris item
    shade = False
    previous_name = None
    for item, name, menu_lines, note_lines, name_lines, row_h in rows:
        if name != previous_name:
            shade = not shade
            previous_name = name
        if shade:
            draw.rectangle([PAD, y, WIDTH - PAD, y + row_h], fill=BAND_BG)

        ty = y + _CELL_PAD_Y

        # Kolom Menu: prefix emoji (kalau ada file-nya) atau "- "
        mx = col_x[0] + _CELL_PAD_X
        emoji = _emoji_img(item.emoji, _FONT_SIZE)
        if emoji is not None:
            img.paste(emoji, (mx, ty + 1), emoji)
        else:
            draw.text((mx, ty), "-", font=font_normal, fill=BLACK)
        text_x = mx + _FONT_SIZE + 6

        for li, mline in enumerate(menu_lines):
            ly = ty + li * line_h
            if li == 0 and (item.menu or "").upper().startswith(HIGHLIGHT_PREFIX):
                # Highlight kuning di prefix NASI UDUK (baris pertama)
                cut = len(HIGHLIGHT_PREFIX)
                hl = mline[:cut] if mline.upper().startswith(HIGHLIGHT_PREFIX) else ""
                if hl:
                    hw = measure.textlength(hl, font=font_normal)
                    draw.rectangle(
                        [text_x - 2, ly - 2, text_x + hw + 2, ly + _FONT_SIZE + 2],
                        fill=YELLOW,
                    )
            draw.text((text_x, ly), mline, font=font_normal, fill=BLACK)

        for ni, nline in enumerate(note_lines):
            ly = ty + (len(menu_lines) + ni) * line_h
            draw.text((text_x, ly), nline, font=font_note, fill=BLACK)

        # Kolom Qty (rata kanan; bold oranye kalau > 1)
        qty_str = str(item.qty)
        qty_font = font_bold if item.qty > 1 else font_normal
        qty_fill = ORANGE if item.qty > 1 else BLACK
        qw = measure.textlength(qty_str, font=qty_font)
        draw.text(
            (col_x[1] + col_px[1] - _CELL_PAD_X - qw, ty),
            qty_str, font=qty_font, fill=qty_fill,
        )

        # Kolom Harga (rata kanan, pakai harga efektif setelah diskon)
        eff_price = max(0, item.price - getattr(item, "discount_riel", 0))
        price_str = f"{eff_price * item.qty:,.0f}"
        pw = measure.textlength(price_str, font=font_normal)
        draw.text(
            (col_x[2] + col_px[2] - _CELL_PAD_X - pw, ty),
            price_str, font=font_normal, fill=BLACK,
        )

        # Kolom Nama Pemesan (bold)
        for li, nline in enumerate(name_lines):
            draw.text(
                (col_x[3] + _CELL_PAD_X, ty + li * line_h),
                nline, font=font_bold, fill=BLACK,
            )

        y += row_h

    # Baris GRAND TOTAL (menyatu seluruh kolom, teks putih di tengah)
    draw.rectangle([PAD, y, WIDTH - PAD, y + total_row_h], fill=HEADER_BG)
    total_text = (
        f"GRAND TOTAL : {order_summary.grand_total_riel:,.0f} Riel "
        f"(${order_summary.grand_total_usd:,.2f})"
    )
    tw = measure.textlength(total_text, font=font_total)
    draw.text(
        ((WIDTH - tw) / 2, y + _CELL_PAD_Y),
        total_text, font=font_total, fill=WHITE,
    )

    buf = BytesIO()
    buf.name = "invoice.png"
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
