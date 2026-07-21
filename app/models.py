from dataclasses import dataclass, field
from typing import List
from datetime import datetime


@dataclass
class OrderItem:

    customer: str = ""

    menu: str = ""

    qty: int = 1

    price: int = 0

    category: str = ""

    raw: str = ""

    note: str = ""

    resto: str = ""

    emoji: str = ""

    # Diskon yang berlaku untuk item ini (dalam Riel, sudah dihitung).
    # 0 = tidak ada diskon. Diisi oleh _apply_discounts_to_summary().
    discount_riel: int = 0

    # Teks (sudah dibersihkan) yang benar-benar dicocokkan ke katalog
    # menu -- dipakai fitur "belajar dari koreksi": kalau staf /ganti
    # item ini, bot mengingat search_text -> menu yang benar.
    search_text: str = ""


@dataclass
class Invoice:

    invoice_no: str = ""

    telegram_name: str = ""

    destination: str = ""

    delivery_type: str = "DELIVERY"

    created_at: str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    items: List[OrderItem] = field(default_factory=list)

    total_riel: int = 0

    total_usd: float = 0