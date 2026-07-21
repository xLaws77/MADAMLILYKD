"""
timezone_utils.py

Bot ini dipakai untuk bisnis di Indonesia, tapi kalau di-deploy ke hosting
gratis (mis. Render) server-nya biasanya jalan di UTC -- jadi
datetime.now() tanpa timezone bikin tanggal/jam di struk & invoice salah.
now_jakarta() selalu mengembalikan waktu WIB (Asia/Jakarta) apapun
timezone server-nya.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

JAKARTA_TZ = ZoneInfo("Asia/Jakarta")


def now_jakarta() -> datetime:
    return datetime.now(JAKARTA_TZ)
