"""
font_utils.py

Cari file font DejaVu yang tersedia di sistem. Pillow's
ImageFont.truetype() tidak scan lokasi non-standar (mis. Termux) kalau
cuma dikasih nama file -- akibatnya font tidak ketemu dan Pillow jatuh
ke bitmap default yang bikin layout invoice/struk berantakan.

Fungsi ini coba beberapa PATH ABSOLUT umum (Termux, Linux, Debian,
macOS) sebelum fallback ke nama file (untuk Windows / cwd) dan
akhirnya bitmap default. Cukup panggil load_font(size, bold) -- yang
mana pun yang ketemu duluan itu yang dipakai.
"""

from PIL import ImageFont

# Lokasi umum instalasi DejaVu di berbagai sistem
_FONT_DIRS = [
    # Termux (Android)
    "/data/data/com.termux/files/usr/share/fonts/TTF",
    # Debian / Ubuntu / Fedora / RHEL
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/TTF",
    # macOS Homebrew
    "/opt/homebrew/share/fonts",
    "/usr/local/share/fonts",
    # Alpine
    "/usr/share/fonts/dejavu",
]


def _candidates(basename):
    """Return list absolute path + basename saja (Pillow scan cwd)."""
    return [f"{d}/{basename}" for d in _FONT_DIRS] + [basename]


def load_font(size, bold=False, mono_preferred=False):
    """Load font TrueType dengan fallback path yang komprehensif.

    - size: ukuran pt
    - bold: pakai varian Bold
    - mono_preferred: pakai DejaVu Sans Mono duluan (untuk struk
      thermal yang butuh lebar seragam), False = Sans proportional.
    """

    if mono_preferred:
        names = (
            ["DejaVuSansMono-Bold.ttf", "DejaVuSans-Bold.ttf"]
            if bold
            else ["DejaVuSansMono.ttf", "DejaVuSans.ttf"]
        )
    else:
        names = (
            ["DejaVuSans-Bold.ttf", "DejaVuSansMono-Bold.ttf"]
            if bold
            else ["DejaVuSans.ttf", "DejaVuSansMono.ttf"]
        )

    for name in names:
        for path in _candidates(name):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue

    return ImageFont.load_default()
