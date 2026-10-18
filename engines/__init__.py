"""引擎包：统一入口，按名称取引擎实例。"""

from .base import Engine, EngineError, Segment, Transcript, run_streaming, srt_time
from .sensevoice import SenseVoiceEngine
from .whisper import WhisperEngine

ENGINES = {
    "whisper": WhisperEngine,
    "sensevoice": SenseVoiceEngine,
}

__all__ = [
    "Engine",
    "EngineError",
    "Segment",
    "Transcript",
    "run_streaming",
    "srt_time",
    "WhisperEngine",
    "SenseVoiceEngine",
    "ENGINES",
]
