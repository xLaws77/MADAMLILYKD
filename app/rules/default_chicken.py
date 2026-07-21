try:
    from ..config import DEFAULT_CHICKEN_PART
except ImportError:
    from config import DEFAULT_CHICKEN_PART


class DefaultChickenRule:
    def apply(self, menu, parser=None):
        default_part = DEFAULT_CHICKEN_PART

        if "AYAM KALASAN" in menu:
            if "DADA" not in menu and "PAHA" not in menu:
                menu = menu.replace(
                    "AYAM KALASAN",
                    f"AYAM KALASAN {default_part}",
                )

                if parser is not None:
                    parser.warnings.append("Default PAHA ATAS digunakan")
                    parser.log("WARNING", "Default PAHA ATAS digunakan")

        if "AYAM KREMES" in menu:
            if "DADA" not in menu and "PAHA" not in menu:
                menu = menu.replace(
                    "AYAM KREMES",
                    f"AYAM KREMES {default_part}",
                )

                if parser is not None:
                    parser.warnings.append("Default PAHA ATAS digunakan")
                    parser.log("WARNING", "Default PAHA ATAS digunakan")

        menu = menu.replace(
            " PAHA ",
            f" {default_part} ",
        )

        return menu
