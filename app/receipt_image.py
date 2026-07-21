"""
receipt_image.py

Generate struk thermal (PNG) untuk SATU FILE gabungan berisi semua
customer dalam order (bukan satu file per customer), ukuran 58mm/80mm.
Dipakai untuk kirim foto struk lewat Telegram (bot.send_photo) atau
langsung diprint ke printer thermal.

Nama menu yang kepanjangan otomatis word-wrap ke baris berikutnya
supaya tidak tumpang tindih dengan harga di sebelah kanan.
"""

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

try:
    from .timezone_utils import now_jakarta
except ImportError:
    from timezone_utils import now_jakarta


# Lebar dalam pixel untuk printer thermal umum (203 dpi)
THERMAL_WIDTH_PX = {
    58: 384,
    80: 576,
}


def _load_font(size, bold=False):
    try:
        from .font_utils import load_font
    except ImportError:
        from font_utils import load_font

    # Struk thermal: monospace duluan supaya kolom rapi
    return load_font(size, bold=bold, mono_preferred=True)


def _wrap_text(draw, text, font, max_width):
    words = text.split()

    if not words:
        return [""]

    lines = []
    current = ""

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


def _build_rows(order_summary, shop_name, width, padding, line_height, sep_height, fonts):
    font_title, font_normal, font_bold = fonts

    # Draw dummy hanya untuk mengukur lebar teks sebelum ukuran gambar
    # final diketahui.
    measure_img = Image.new("RGB", (width, 10), "white")
    measure_draw = ImageDraw.Draw(measure_img)

    max_text_width = width - padding * 2
    rows = []

    rows.append(("center", shop_name, font_title, line_height))
    rows.append(
        ("center", now_jakarta().strftime("%d-%m-%Y %H:%M"), font_normal, line_height)
    )

    if getattr(order_summary, "destination", ""):
        rows.append(("center", f"Lokasi: {order_summary.destination}", font_normal, line_height))

    if getattr(order_summary, "orderer_name", ""):
        rows.append(("center", f"Pemesan: {order_summary.orderer_name}", font_normal, line_height))

    rows.append(("sep", None, None, sep_height))

    for n, invoice in enumerate(order_summary.invoices):
        if n > 0:
            rows.append(("sep", None, None, sep_height))

        block_name = invoice.telegram_name or ""

        # Order polos tanpa nama customer per-item ditandai "UNKNOWN" --
        # tampilkan nama penanggung jawab order (dari balasan
        # LOKASI/NAMA) di struk supaya tetap ada nama yang jelas, tidak
        # menampilkan literal "UNKNOWN".
        if block_name.strip().upper() == "UNKNOWN":
            block_name = getattr(order_summary, "orderer_name", "") or block_name

        rows.append(("left", block_name.upper(), font_bold, line_height))

        for item in invoice.items:
            qty_font = font_bold if item.qty > 1 else font_normal
            qty_color = (211, 84, 0) if item.qty > 1 else None  # oranye gelap bila qty > 1

            menu_part = item.menu
            qty_part = f" x{item.qty}"
            note_part = f" ({item.note})" if item.note else ""
            disc = getattr(item, "discount_riel", 0)
            eff_price = max(0, item.price - disc)
            subtotal = f"{eff_price * item.qty:,.0f}"
            if disc > 0:
                note_part = f" (diskon {disc:,.0f}R)" + (f" ({item.note})" if item.note else "")
            price_width = measure_draw.textlength(subtotal, font=font_normal)

            # Lebar total nama: menu (normal) + qty (bold jika >1) + note (normal)
            name_width = (
                measure_draw.textlength(menu_part, font=font_normal)
                + measure_draw.textlength(qty_part, font=qty_font)
                + (measure_draw.textlength(note_part, font=font_normal) if note_part else 0)
            )

            if name_width + price_width + 12 <= max_text_width:
                # Muat dalam satu baris: simpan komponen terpisah untuk
                # bisa digambar dengan font/warna berbeda.
                rows.append(("item", menu_part, qty_part, note_part, qty_color, subtotal, line_height))
            else:
                # Kepanjangan -- fallback ke teks gabungan dengan wrap.
                full_name = menu_part + qty_part + note_part
                for line in _wrap_text(measure_draw, full_name, font_normal, max_text_width):
                    rows.append(("left", line, font_normal, line_height))
                rows.append(("price_only", subtotal, font_normal, line_height))

    rows.append(("sep", None, None, sep_height))
    rows.append(
        ("total", "GRAND TOTAL (Riel)", f"{order_summary.grand_total_riel:,.0f}", line_height)
    )
    rows.append(
        ("total", "GRAND TOTAL (USD)", f"${order_summary.grand_total_usd:,.2f}", line_height)
    )
    rows.append(("sep", None, None, sep_height))
    rows.append(("center", "Terima kasih!", font_normal, line_height))

    return rows


def render_thermal_receipt(
    order_summary,
    shop_name: str = "MADAM LILY",
    width_mm: int = 58,
) -> Image.Image:
    """
    order_summary: instance receipt_data.OrderSummary (SUDAH dihitung
                   lewat BillGenerator, yaitu invoice.total_riel &
                   invoice.total_usd sudah terisi untuk semua invoice)
    width_mm: 58 atau 80
    Return: PIL.Image (mode RGB) -- SATU gambar berisi semua customer.
    """

    width = THERMAL_WIDTH_PX.get(width_mm, 384)
    padding = 10
    line_height = 22
    sep_height = 12

    font_title = _load_font(22, bold=True)
    font_normal = _load_font(18)
    font_bold = _load_font(18, bold=True)

    rows = _build_rows(
        order_summary,
        shop_name,
        width,
        padding,
        line_height,
        sep_height,
        (font_title, font_normal, font_bold),
    )

    height = padding * 2 + sum(row[-1] for row in rows)

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    y = padding

    for row in rows:
        kind, row_height = row[0], row[-1]

        if kind == "center":
            text, font = row[1], row[2]
            w = draw.textlength(text, font=font)
            draw.text(((width - w) / 2, y), text, font=font, fill="black")

        elif kind == "left":
            text, font = row[1], row[2]
            draw.text((padding, y), text, font=font, fill="black")

        elif kind == "sep":
            mid = y + row_height / 2
            draw.line((padding, mid, width - padding, mid), fill="black", width=1)

        elif kind == "item":
            menu_part, qty_part, note_part, qty_color, price_text = row[1], row[2], row[3], row[4], row[5]
            x = padding

            # Menu name (normal)
            draw.text((x, y), menu_part, font=font_normal, fill="black")
            x += draw.textlength(menu_part, font=font_normal)

            # Qty — bold + warna oranye bila qty > 1
            qty_font_draw = font_bold if qty_color else font_normal
            qty_fill = qty_color if qty_color else "black"
            draw.text((x, y), qty_part, font=qty_font_draw, fill=qty_fill)
            x += draw.textlength(qty_part, font=qty_font_draw)

            # Note (normal, jika ada)
            if note_part:
                draw.text((x, y), note_part, font=font_normal, fill="black")

            # Harga di kanan
            w = draw.textlength(price_text, font=font_normal)
            draw.text((width - padding - w, y), price_text, font=font_normal, fill="black")

        elif kind == "price_only":
            price_text, font = row[1], row[2]
            w = draw.textlength(price_text, font=font)
            draw.text((width - padding - w, y), price_text, font=font, fill="black")

        elif kind == "total":
            label, value = row[1], row[2]
            draw.text((padding, y), label, font=font_bold, fill="black")
            w = draw.textlength(value, font=font_bold)
            draw.text((width - padding - w, y), value, font=font_bold, fill="black")

        y += row_height

    return img


def render_thermal_receipt_bytes(order_summary, width_mm: int = 58) -> BytesIO:
    """Helper: langsung return BytesIO PNG, siap dikirim ke bot.send_photo()."""
    img = render_thermal_receipt(order_summary, width_mm=width_mm)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "struk.png"
    return buf
