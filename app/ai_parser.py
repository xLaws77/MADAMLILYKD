"""
ai_parser.py

Lapisan AI untuk parsing order yang TIDAK berhasil dibaca penuh oleh
parser regex biasa (pola hybrid):

1. Setiap order tetap diproses parser regex dulu (cepat & gratis).
2. HANYA kalau ada baris tak dikenal / menu tak ketemu, teks order
   dikirim ke AI bersama daftar menu, dan AI mengembalikan JSON
   terstruktur (customer, menu, qty, catatan).
3. Kalau API gagal (kuota habis, jaringan, jawaban rusak), bot langsung
   fallback ke hasil parser regex -- AI tidak pernah bikin bot mati.

Dua provider didukung, dipilih otomatis lewat environment variable
(GROQ_API_KEY diprioritaskan kalau dua-duanya diisi):

- Groq (gratis, https://console.groq.com/keys) -- lebih cepat & limit
  harian lebih longgar, direkomendasikan.
- Google Gemini (gratis, https://aistudio.google.com/apikey).

Tanpa key sama sekali, lapisan ini nonaktif dan bot berjalan persis
seperti sebelumnya.

Catatan free tier: ada batas request per menit/hari (cukup longgar
untuk volume kantin), dan data yang dikirim dapat dipakai penyedia
untuk peningkatan model -- jangan kirim data sensitif.
"""

import json
import os
from typing import Any, Dict, List, Optional

import httpx

try:
    from .logger import info, warning
except ImportError:
    try:
        from app.logger import info, warning
    except ImportError:
        def info(msg): print(f"INFO: {msg}")
        def warning(msg): print(f"WARNING: {msg}")


REQUEST_TIMEOUT_SECONDS = 30.0

# ==========================================================
# GROQ (OpenAI-compatible)
# ==========================================================

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"

GROQ_DEFAULT_MODEL = "llama-3.3-70b-versatile"
GROQ_FALLBACK_MODELS = ["llama-3.1-8b-instant", "llama-4-scout"]

# ==========================================================
# OPENROUTER (OpenAI-compatible, gratis untuk beberapa model)
# ==========================================================

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# Model gratis di OpenRouter (suffix ":free")
OPENROUTER_DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
OPENROUTER_FALLBACK_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "mistralai/mistral-7b-instruct:free",
    "qwen/qwen3-8b:free",
]

# ==========================================================
# GOOGLE GEMINI
# ==========================================================

GEMINI_ENDPOINT_V1BETA = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)
GEMINI_ENDPOINT_V1 = (
    "https://generativelanguage.googleapis.com/v1/models/"
    "{model}:generateContent"
)
GEMINI_MODELS_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Alias default — diganti ke v1 otomatis kalau v1beta kena 401
GEMINI_ENDPOINT = GEMINI_ENDPOINT_V1BETA

GEMINI_DEFAULT_MODEL = "gemini-2.0-flash"
GEMINI_FALLBACK_MODELS = ["gemini-1.5-flash", "gemini-2.5-flash", "gemini-flash-latest"]


PROMPT_TEMPLATE = """Kamu adalah sistem parsing order untuk kantin Indonesia.
Tugasmu membaca teks order pelanggan (bahasa campuran Indonesia/Inggris,
format bebas dan sering berantakan) lalu mengubahnya jadi JSON terstruktur.

DAFTAR MENU RESMI (satu-satunya menu yang boleh dipakai, format: NAMA | HARGA):
{catalogue}

ATURAN:
1. Field "menu" WAJIB persis salah satu NAMA dari daftar di atas (huruf besar,
   ejaan sama persis). Kalau pelanggan menulis singkatan/typo, pilih menu yang
   paling cocok dari daftar.
2. "customer" = nama pemesan item itu (string kosong "" kalau tidak disebut).
   Nama customer biasanya kata pendek yang berdiri sendiri di baris terpisah,
   atau nempel di baris menu (dalam kurung, setelah tanda -, atau di awal
   baris sebelum tanda : atau ;).
3. "qty" = jumlah porsi (angka bulat, minimal 1). Harga yang ditulis pelanggan
   (mis. 12.000R, 12000) BUKAN qty.
4. "note" = permintaan khusus untuk item itu (mis. "no kacang", "kuah banyak"),
   string kosong kalau tidak ada.
5. Baris yang bukan menu dan bukan nama (mis. basa-basi) diabaikan.
6. Jawab HANYA dengan JSON valid, tanpa teks lain, dengan bentuk persis:
{{"items": [{{"customer": "", "menu": "", "qty": 1, "note": ""}}]}}

TEKS ORDER PELANGGAN:
{order_text}"""


class AIParser:
    def __init__(self, menus: List[Dict[str, Any]]):
        self.menus = menus
        self._catalogue = "\n".join(
            f"{menu['name']} | {menu['price']}" for menu in menus
        )
        self.last_error: Optional[str] = None

        groq_key = os.getenv("GROQ_API_KEY", "").strip()
        openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

        # Prioritas: Groq > OpenRouter > Gemini
        if groq_key:
            self.provider = "groq"
            self.api_key = groq_key
            self.model = (
                os.getenv("GROQ_MODEL", "").strip() or GROQ_DEFAULT_MODEL
            )
        elif openrouter_key:
            self.provider = "openrouter"
            self.api_key = openrouter_key
            self.model = (
                os.getenv("OPENROUTER_MODEL", "").strip()
                or OPENROUTER_DEFAULT_MODEL
            )
        elif gemini_key:
            self.provider = "gemini"
            self.api_key = gemini_key
            self.model = (
                os.getenv("GEMINI_MODEL", "").strip() or GEMINI_DEFAULT_MODEL
            )
        else:
            self.provider = None
            self.api_key = ""
            self.model = ""

        self._auth_scheme = "bearer"
        # Endpoint Gemini -- dicoba v1beta dulu, fallback ke v1 kalau 401
        self._gemini_endpoint = GEMINI_ENDPOINT_V1BETA

    @property
    def available(self) -> bool:
        return bool(self.provider)

    def _build_prompt(self, order_text: str, context: Optional[Dict]) -> str:
        """Bangun prompt dengan konteks opsional dari parser regex.
        Konteks membantu AI fokus pada baris bermasalah dan tidak
        mengabaikan customer yang sudah dikenali parser."""
        base = PROMPT_TEMPLATE.format(
            catalogue=self._catalogue,
            order_text=order_text,
        )

        if not context:
            return base

        parts = []
        known_customers = context.get("known_customers") or []
        parsed_items = context.get("parsed_items") or []
        unknown_lines = context.get("unknown_lines") or []

        if known_customers:
            parts.append(
                "Customer yang sudah teridentifikasi: "
                + ", ".join(known_customers)
            )

        if parsed_items:
            items_str = "; ".join(
                f"{'?' if not i['customer'] else i['customer']}"
                f": {i['menu']} x{i['qty']}"
                for i in parsed_items[:10]
            )
            parts.append(f"Item yang sudah terbaca parser: {items_str}")

        if unknown_lines:
            joined = "\n".join(f"  * {l}" for l in unknown_lines[:15])
            parts.append(
                f"Baris/menu yang BELUM berhasil dibaca parser "
                f"(fokuskan pembacaan ke sini):\n{joined}"
            )

        if not parts:
            return base

        ctx_block = (
            "KONTEKS DARI PARSER REGEX (gunakan sebagai panduan tambahan):\n"
            + "\n".join(parts)
        )

        return base.replace(
            "\nTEKS ORDER PELANGGAN:",
            f"\n{ctx_block}\n\nTEKS ORDER PELANGGAN:",
        )

    def parse(
        self, order_text: str, context: Optional[Dict] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """Return list of {customer, menu, qty, note} atau None kalau gagal.
        TIDAK pernah raise -- kegagalan apa pun jadi None supaya caller
        selalu bisa fallback ke parser regex.

        context: dict opsional dari TelegramAdapter berisi known_customers,
        parsed_items, dan unknown_lines -- dipakai untuk memperkuat
        pembacaan AI pada order yang sebagian sudah terbaca parser."""

        if not self.available:
            return None

        prompt = self._build_prompt(order_text, context)

        self.last_error = None

        try:
            raw = self._call_with_fallback(prompt)

            data = json.loads(raw)
            items = data.get("items")

            if not isinstance(items, list):
                msg = "Struktur JSON dari AI tidak sesuai"
                warning(f"AIParser: {msg}")
                self.last_error = msg
                return None

            cleaned = []

            for entry in items:
                if not isinstance(entry, dict):
                    continue

                menu = str(entry.get("menu", "")).strip()

                if not menu:
                    continue

                try:
                    qty = max(1, int(entry.get("qty") or 1))
                except (TypeError, ValueError):
                    qty = 1

                cleaned.append(
                    {
                        "customer": str(entry.get("customer", "")).strip(),
                        "menu": menu,
                        "qty": qty,
                        "note": str(entry.get("note", "")).strip(),
                    }
                )

            if not cleaned:
                msg = "AI mengembalikan daftar item kosong"
                warning(f"AIParser: {msg}")
                self.last_error = msg
                return None

            info(
                f"AIParser ({self.provider}): mengembalikan {len(cleaned)} item"
            )
            return cleaned

        except Exception as e:
            self.last_error = str(e)
            warning(f"AIParser ({self.provider}) gagal: {e!r}")
            return None

    # ==========================================================
    # HTTP HELPER (dipakai kedua provider)
    # ==========================================================

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        # Tiga cara auth dicoba berurutan sampai salah satu berhasil:
        # - "header"     : x-goog-api-key (Gemini key format AIzaSy...)
        # - "bearer"     : Authorization: Bearer (Groq, atau Gemini alternatif)
        # - "queryparam" : ?key=... (Gemini key format baru AQ.xxx)
        schemes = [self._auth_scheme] + [
            s for s in ("header", "bearer", "queryparam") if s != self._auth_scheme
        ]

        response = None

        for scheme in schemes:
            call_kwargs = dict(kwargs)

            if scheme == "header":
                headers = {"x-goog-api-key": self.api_key}
            elif scheme == "bearer":
                headers = {"Authorization": f"Bearer {self.api_key}"}
            else:  # queryparam
                headers = {}
                existing = call_kwargs.pop("params", {}) or {}
                call_kwargs["params"] = {**existing, "key": self.api_key}

            response = httpx.request(
                method,
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
                **call_kwargs,
            )

            if response.status_code in (401, 403):
                warning(
                    f"AIParser: auth cara '{scheme}' ditolak "
                    f"({response.status_code}), coba cara lain"
                )
                continue

            if scheme != self._auth_scheme:
                info(f"AIParser: pakai auth cara '{scheme}'")
                self._auth_scheme = scheme

            return response

        # Semua cara ditolak -- kembalikan respons terakhir supaya
        # caller melempar error 401/403 yang jelas.
        return response

    # ==========================================================
    # FALLBACK CHAIN
    # ==========================================================

    def _call_with_fallback(self, prompt: str) -> str:
        """Panggil provider utama; kalau kena 429 coba provider cadangan.
        Urutan cadangan: Groq -> OpenRouter -> Gemini (sesuai key yang diisi)."""

        def _call(prov: str) -> str:
            if prov in ("groq", "openrouter"):
                old = self.provider
                self.provider = prov
                try:
                    return self._call_openai_compat(prompt)
                except Exception:
                    self.provider = old
                    raise
            else:
                return self._call_gemini(prompt)

        # Bangun urutan provider berdasarkan key yang tersedia
        chain = [self.provider]
        openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        groq_key = os.getenv("GROQ_API_KEY", "").strip()

        for prov, key in [
            ("groq", groq_key),
            ("openrouter", openrouter_key),
            ("gemini", gemini_key),
        ]:
            if key and prov != self.provider:
                chain.append(prov)

        last_err: Optional[Exception] = None

        for prov in chain:
            # Siapkan key dan model untuk provider ini
            if prov == "groq":
                self.api_key = groq_key or self.api_key
                if prov != self.provider:
                    self.model = os.getenv("GROQ_MODEL", "").strip() or GROQ_DEFAULT_MODEL
            elif prov == "openrouter":
                self.api_key = openrouter_key or self.api_key
                if prov != self.provider:
                    self.model = (
                        os.getenv("OPENROUTER_MODEL", "").strip()
                        or OPENROUTER_DEFAULT_MODEL
                    )
            elif prov == "gemini":
                self.api_key = gemini_key or self.api_key
                if prov != self.provider:
                    self.model = os.getenv("GEMINI_MODEL", "").strip() or GEMINI_DEFAULT_MODEL

            try:
                result = _call(prov)
                if prov != chain[0]:
                    info(f"AIParser: fallback ke {prov} berhasil")
                    self.provider = prov
                return result
            except Exception as e:
                last_err = e
                if "429" in str(e) and prov != chain[-1]:
                    warning(f"AIParser: {prov} rate limit (429), coba {chain[chain.index(prov)+1]}")
                    continue
                # Error bukan 429, atau sudah tidak ada fallback lagi
                raise e

        raise last_err or RuntimeError("semua provider AI gagal")

    # ==========================================================
    # GROQ / OPENROUTER
    # ==========================================================

    def _call_openai_compat(self, prompt: str) -> str:
        """Panggil provider OpenAI-compatible (Groq ATAU OpenRouter)."""
        if self.provider == "openrouter":
            chat_url = OPENROUTER_CHAT_URL
            fallback_models = [
                m for m in OPENROUTER_FALLBACK_MODELS if m != self.model
            ]
            # OpenRouter tidak butuh response_format json_object
            payload_base = {
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            }
        else:  # groq
            chat_url = GROQ_CHAT_URL
            fallback_models = [
                m for m in GROQ_FALLBACK_MODELS if m != self.model
            ]
            payload_base = {
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            }

        candidates = [self.model] + fallback_models
        last_error: Optional[Exception] = None

        for attempt, model in enumerate(candidates + [None]):
            if model is None:
                if self.provider == "groq":
                    discovered = self._discover_groq_model()
                    if not discovered or discovered in candidates:
                        break
                    model = discovered
                else:
                    break

            response = self._request(
                "POST", chat_url, json={**payload_base, "model": model}
            )

            if response.status_code in (400, 404):
                warning(
                    f"AIParser: model {self.provider} '{model}' tidak tersedia "
                    f"({response.status_code}), coba model berikutnya"
                )
                last_error = RuntimeError(f"model {model} tidak tersedia")
                continue

            response.raise_for_status()

            if model != self.model:
                info(f"AIParser: pindah ke model {self.provider} {model}")
                self.model = model

            body = response.json()
            content = body["choices"][0]["message"]["content"]

            # OpenRouter kadang wrap JSON dalam markdown code block
            if content.startswith("```"):
                content = content.strip("`").strip()
                if content.startswith("json"):
                    content = content[4:].strip()

            return content

        raise last_error or RuntimeError(
            f"semua model {self.provider} tidak tersedia"
        )

    def _call_groq(self, prompt: str) -> str:
        return self._call_openai_compat(prompt)

    def _discover_groq_model(self) -> Optional[str]:
        try:
            response = self._request("GET", GROQ_MODELS_URL)
            response.raise_for_status()
            models = response.json().get("data", [])
        except Exception as e:
            warning(f"AIParser: gagal ambil daftar model Groq: {e!r}")
            return None

        usable = [m.get("id", "") for m in models if m.get("id")]

        for name in usable:
            lowered = name.lower()

            if "llama" in lowered and "guard" not in lowered:
                info(f"AIParser: model Groq hasil discovery: {name}")
                return name

        if usable:
            info(f"AIParser: model Groq hasil discovery: {usable[0]}")
            return usable[0]

        return None

    # ==========================================================
    # GOOGLE GEMINI
    # ==========================================================

    def _call_gemini(self, prompt: str) -> str:
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0,
            },
        }

        # Coba model utama dulu; kalau 404 (model tidak tersedia untuk
        # key ini), coba model fallback. Kalau SEMUA tebakan gagal,
        # tanya langsung ke Google daftar model yang tersedia untuk key
        # ini (nama model free tier sering berubah). Model yang
        # berhasil diingat supaya request berikutnya langsung ke sana.
        candidates = [self.model] + [
            m for m in GEMINI_FALLBACK_MODELS if m != self.model
        ]

        last_error: Optional[Exception] = None

        # Endpoint yang dicoba: v1beta dulu, lalu v1 kalau 401
        endpoint_candidates = [self._gemini_endpoint] + [
            ep for ep in (GEMINI_ENDPOINT_V1BETA, GEMINI_ENDPOINT_V1)
            if ep != self._gemini_endpoint
        ]

        for attempt, model in enumerate(candidates + [None]):
            if model is None:
                discovered = self._discover_gemini_model()

                if not discovered or discovered in candidates:
                    break

                model = discovered

            response = None
            for endpoint_tmpl in endpoint_candidates:
                url = endpoint_tmpl.format(model=model)
                response = self._request("POST", url, json=payload)

                if response.status_code in (401, 403):
                    warning(
                        f"AIParser: endpoint {endpoint_tmpl.split('/v')[1][:3]} "
                        f"ditolak ({response.status_code}), coba endpoint lain"
                    )
                    continue

                if endpoint_tmpl != self._gemini_endpoint:
                    info(f"AIParser: pindah ke endpoint Gemini v{endpoint_tmpl.split('/v')[1][:1]}")
                    self._gemini_endpoint = endpoint_tmpl
                break

            if response is None:
                continue

            if response.status_code in (401, 403):
                last_error = RuntimeError(
                    f"401 Unauthorized -- semua endpoint ditolak. "
                    f"Pastikan GEMINI_API_KEY valid."
                )
                continue

            if response.status_code == 404:
                warning(
                    f"AIParser: model Gemini {model} tidak tersedia (404), "
                    "coba model berikutnya"
                )
                last_error = RuntimeError(f"model {model} tidak tersedia")
                continue

            response.raise_for_status()

            if model != self.model:
                info(f"AIParser: pindah ke model Gemini {model}")
                self.model = model

            body = response.json()
            return body["candidates"][0]["content"]["parts"][0]["text"]

        raise last_error or RuntimeError("semua model Gemini tidak tersedia")

    def _discover_gemini_model(self) -> Optional[str]:
        """Tanya endpoint ListModels: model apa saja yang tersedia untuk
        key ini. Pilih varian flash (cepat & murah) yang mendukung
        generateContent. Return None kalau gagal."""

        try:
            response = self._request(
                "GET", GEMINI_MODELS_LIST_URL, params={"pageSize": 200}
            )
            response.raise_for_status()
            models = response.json().get("models", [])
        except Exception as e:
            warning(f"AIParser: gagal ambil daftar model Gemini: {e!r}")
            return None

        usable = []

        for model in models:
            if "generateContent" not in model.get(
                "supportedGenerationMethods", []
            ):
                continue

            # "models/gemini-3.5-flash" -> "gemini-3.5-flash"
            usable.append(model.get("name", "").split("/", 1)[-1])

        # Prioritas: flash biasa (bukan varian khusus seperti lite/
        # image/tts/live/thinking yang lebih lambat atau beda fungsi)
        for name in usable:
            lowered = name.lower()

            if "flash" in lowered and not any(
                x in lowered for x in ("lite", "image", "tts", "live", "audio")
            ):
                info(f"AIParser: model Gemini hasil discovery: {name}")
                return name

        if usable:
            info(f"AIParser: model Gemini hasil discovery: {usable[0]}")
            return usable[0]

        return None
