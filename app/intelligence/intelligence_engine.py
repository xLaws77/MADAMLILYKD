"""
intelligence_engine.py

Orchestrator Smart Order Intelligence.

Tugas: menerima teks order mentah dari Telegram, menjalankan para
detector (customer, menu, qty, note, intent), lalu menghasilkan
IntelligenceResult berisi teks yang sudah dinormalkan + metadata +
confidence.

Kontrak dengan pemanggil (TelegramAdapter):
- analyze(text) TIDAK PERNAH raise -- kalau ada error internal,
  hasilnya passthrough (teks asli, confidence 0) supaya pipeline
  lama tetap jalan seperti biasa.
- confidence < threshold  -> pemanggil pakai teks ASLI (jalur lama,
  termasuk gate AI hybrid yang sudah ada).
- confidence >= threshold -> pemanggil boleh pakai normalized_text.

CATATAN VERSI INI (Langkah 1 -- skeleton):
Belum ada detector yang dipasang. analyze() selalu mengembalikan
passthrough, jadi perilaku bot 100% sama dengan sebelum lapisan ini
ada. Detector dipasang bertahap di langkah berikutnya.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List

try:
    from ..logger import info, error
except ImportError:
    try:
        from app.logger import info, error
    except ImportError:
        def info(msg): print(f"INFO: {msg}")
        def error(msg): print(f"ERROR: {msg}")


@dataclass
class IntelligenceResult:
    """Hasil analisa lapisan intelijen untuk SATU pesan order."""

    # Teks siap-parser (format yang ParserEngine sudah mengerti).
    # Kalau lapisan ini tidak yakin, isinya = teks asli.
    normalized_text: str = ""

    # Metadata hasil deteksi (diisi detector di langkah berikutnya).
    customer: str = ""
    items: List[Dict[str, Any]] = field(default_factory=list)  # {menu, qty, note}
    intent: str = "ORDER"

    # 0-100. 0 = tidak ada analisa / passthrough murni.
    confidence: int = 0

    # True kalau normalized_text berbeda dari teks asli DAN layak dipakai.
    rewritten: bool = False

    def merge_confidence(self, score: int) -> None:
        """Gabungkan skor dari satu detector: confidence akhir = skor
        TERLEMAH di antara detector yang beraksi (weakest link) --
        satu deteksi yang ragu bikin keseluruhan hasil dianggap ragu."""

        self.confidence = (
            score if self.confidence == 0 else min(self.confidence, score)
        )


class IntelligenceEngine:
    """Menjalankan detector satu per satu dan menggabungkan hasilnya.

    Detector didaftarkan bertahap (SOLID: tiap detector satu tanggung
    jawab, engine hanya orkestrasi). Semua deteksi menu WAJIB lewat
    MatchingEngine existing -- engine ini tidak pernah membuat menu.
    """

    def __init__(self, parser_provider=None):
        # parser_provider: callable -> ParserEngine (biasanya
        # TelegramAdapter._new_parser, supaya koreksi hasil belajar
        # ikut terpakai). Dipakai detector yang perlu bertanya ke
        # logika parser lama (mis. is_menu_line) -- reuse, bukan tulis
        # ulang.
        try:
            from .quantity_detector import QuantityDetector
            from .note_detector import NoteDetector
            from .customer_detector import CustomerDetector
            from .menu_detector import MenuDetector
            from .intent_detector import IntentDetector
        except ImportError:
            from quantity_detector import QuantityDetector
            from note_detector import NoteDetector
            from customer_detector import CustomerDetector
            from menu_detector import MenuDetector
            from intent_detector import IntentDetector

        # Urutan penting: intent dibaca dari teks asli dulu; qty & note
        # menormalkan teks; customer dan menu membaca teks yang sudah
        # normal itu.
        self.detectors = [
            IntentDetector(),
            QuantityDetector(parser_provider),
            NoteDetector(parser_provider),
            CustomerDetector(parser_provider),
            MenuDetector(parser_provider),
        ]

    def analyze(self, text: str) -> IntelligenceResult:
        """Analisa teks order. TIDAK PERNAH raise (fail-open)."""

        result = IntelligenceResult(normalized_text=text or "")

        if not text or not text.strip():
            return result

        try:
            return self._run(text, result)
        except Exception as e:  # fail-open: jangan pernah mematahkan pipeline
            error(f"IntelligenceEngine gagal, fallback ke teks asli: {e!r}")
            return IntelligenceResult(normalized_text=text)

    # ------------------------------------------------------------------
    def _run(self, text: str, result: IntelligenceResult) -> IntelligenceResult:
        # Detector dijalankan BERANTAI: masing-masing membaca teks kerja
        # terkini (result.normalized_text, hasil detector sebelumnya)
        # dan menyumbang metadata + skor confidence-nya sendiri.
        for detector in self.detectors:
            detector.detect(result.normalized_text, result)

        self._log(result)
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _log(result: IntelligenceResult) -> None:
        if not result.rewritten and result.confidence == 0:
            return  # passthrough murni: tidak ada yang perlu dilaporkan

        info(
            "[INTELLIGENCE] "
            f"customer={result.customer or '-'} "
            f"items={len(result.items)} "
            f"intent={result.intent} "
            f"confidence={result.confidence}"
        )
