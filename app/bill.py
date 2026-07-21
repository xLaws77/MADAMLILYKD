from app.config import USD_RATE


class BillGenerator:

    def __init__(self):
        pass

    def calculate(self, invoice):

        total = 0

        for item in invoice.items:

            effective = max(0, item.price - getattr(item, "discount_riel", 0))
            total += effective * item.qty

        invoice.total_riel = total

        invoice.total_usd = round(total / USD_RATE, 2)

        return invoice
