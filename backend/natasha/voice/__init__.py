"""Voice: text-to-speech output and speech-to-text input.

Speech *in* is the hearing engine (perception). Speech *out* is here. Backends are detected on the
host - piper, espeak-ng/espeak, macOS ``say``, Windows SAPI, or an OpenAI-compatible TTS endpoint via
the credential broker. When no backend exists, the engine says so instead of writing a silent file.
"""

from .conversation import VoiceLoop, VoiceTurn, get_voice_loop, reset_voice_loop
from .engine import VoiceEngine, VoiceResult, get_voice_engine, reset_voice_engine
from .wakeword import WakeWordDetector, get_wakeword_detector

__all__ = ["VoiceEngine", "VoiceResult", "get_voice_engine", "reset_voice_engine",
           "VoiceLoop", "VoiceTurn", "get_voice_loop", "reset_voice_loop",
           "WakeWordDetector", "get_wakeword_detector"]
