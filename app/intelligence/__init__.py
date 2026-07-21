"""
Smart Order Intelligence

Lapisan intelijen DI ATAS ParserEngine: memahami teks order gaya
chat/natural sebelum diteruskan ke pipeline lama (ParserEngine ->
BusinessRules -> MatchingEngine -> Formatter).

Pipeline lama TIDAK diubah -- lapisan ini hanya menormalkan teks
dan mengumpulkan metadata (customer, qty, note, intent, confidence).
"""

from .intelligence_engine import IntelligenceEngine, IntelligenceResult

__all__ = ["IntelligenceEngine", "IntelligenceResult"]
