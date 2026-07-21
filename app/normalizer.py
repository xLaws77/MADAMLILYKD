import re


class Normalizer:

    def __init__(self):
        pass

    def clean(self, text: str) -> str:

        if not text:
            return ""

        # Huruf besar
        text = text.upper()

        # Hilangkan emoji
        emojis = [
            "🍱", "🍛", "🐓", "🍚", "🍲",
            "🐮", "🍜", "🥣", "🐟", "🥤",
            "🍗", "🥟", "🥪", "🥘"
        ]

        for e in emojis:
            text = text.replace(e, "")

        # Hilangkan harga
        text = re.sub(r"\d{1,3}\.\d{3}\s*R", "", text)
        text = re.sub(r"\d+\s*R", "", text)

        # Ganti simbol menjadi spasi
        symbols = [
            ":",
            ";",
            ",",
            "|",
            "+",
            "-",
            ">",
            "<",
            "=",
            "(",
            ")",
            "[",
            "]"
        ]

        for s in symbols:
            text = text.replace(s, " ")

        # Rapikan spasi
        text = re.sub(r"\s+", " ", text)

        return text.strip()


if __name__ == "__main__":

    n = Normalizer()

    tests = [

        "🍱CHICKEN KATSU+RICE : 12.000R (Rafly)",

        "🐟BATAGOR KUAH : 12.000R - Dio",

        "🍛KATSU CURRY RICE : 13.000R = Lux",

        "🍚NASI UDUK KOMPLIT : 10.000R > Captain"

    ]

    for t in tests:

        print("ASLI   :", t)

        print("BERSIH :", n.clean(t))

        print("-" * 50)