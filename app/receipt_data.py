"""
receipt_data.py

Menggabungkan hasil ParserEngine (item per customer) dengan BillGenerator
(hitung total_riel & total_usd), lalu membungkusnya jadi models.Invoice
per customer -- struktur data yang SUDAH ADA di models.py, tidak bikin
class baru yang tumpang tindih.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List

try:
    from .models import Invoice
    from .bill import BillGenerator
except ImportError:
    from models import Invoice
    from bill import BillGenerator


@dataclass
class OrderSummary:
    invoices: List[Invoice] = field(default_factory=list)
    grand_total_riel: int = 0
    grand_total_usd: float = 0.0

    # Lokasi pengantaran & nama telegram pemesan untuk SELURUH order ini
    # (diisi manual lewat balasan "LOKASI/NAMA") -- beda dari
    # invoice.telegram_name yang isinya nama tiap customer YANG ADA DI
    # DALAM pesanan (mis. ALI, AUNG, dst).
    destination: str = ""
    orderer_name: str = ""


class ReceiptBuilder:
    def __init__(self, bill_generator: BillGenerator = None):
        self.bill_generator = bill_generator or BillGenerator()

    def build(
        self,
        parser,
        delivery_type: str = "DELIVERY",
        destination: str = "",
    ) -> OrderSummary:
        """
        parser: instance ParserEngine yang SUDAH dipanggil .parse(text)
        """

        groups = parser.group_by_customer()
        today = datetime.now().strftime("%Y%m%d")

        invoices: List[Invoice] = []
        grand_riel = 0
        grand_usd = 0.0

        for i, (customer, items) in enumerate(groups.items(), start=1):
            if not items:
                continue

            invoice = Invoice(
                invoice_no=f"INV-{today}-{i:03d}",
                telegram_name=customer or "UNKNOWN",
                destination=destination,
                delivery_type=delivery_type,
                items=items,
            )

            invoice = self.bill_generator.calculate(invoice)

            invoices.append(invoice)
            grand_riel += invoice.total_riel
            grand_usd += invoice.total_usd

        return OrderSummary(
            invoices=invoices,
            grand_total_riel=grand_riel,
            grand_total_usd=round(grand_usd, 2),
        )
