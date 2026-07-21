USD_RATE = 4000

BOT_NAME = "Madam Lily Bot"

VERSION = "1.0.0"

MATCH_SCORE = 70

# ==========================================================
# PARSER CONFIG
# ==========================================================

DEFAULT_CHICKEN_PART = "PAHA ATAS"

DEFAULT_SPLIT_COMBO = r"\s*\+\s*"

FUZZY_MATCH_SCORE = 80

ENABLE_LOG = True

ENABLE_DEBUG = False

# ==========================================================
# SMART ORDER INTELLIGENCE
# ==========================================================

# Saklar utama lapisan intelijen (app/intelligence/).
# False = bot berperilaku 100% seperti sebelum lapisan ini ada.
ENABLE_INTELLIGENCE = True

# Ambang confidence (0-100). Di bawah ini, teks ASLI yang dipakai
# (jalur lama + gate AI hybrid existing yang memutuskan).
INTELLIGENCE_CONFIDENCE = 70