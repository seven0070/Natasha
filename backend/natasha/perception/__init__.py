"""Multimodal perception: seeing (images, screenshots, documents) and hearing (audio, transcription).

Perception is *input only*. Everything it produces is data: it is tagged untrusted unless it came
from the owner directly, and it never carries instructions the executive loop could mistake for
authority. Unavailable providers degrade honestly instead of pretending.
"""

from .vision import (
    ImageAnalysis,
    VisionEngine,
    describe_image,
    extract_text_from_image,
    get_vision_engine,
)
from .hearing import AudioTranscription, HearingEngine, SpeechSegment, get_hearing_engine
from .documents import DocumentExtract, DocumentReader, get_document_reader

__all__ = [
    "ImageAnalysis", "VisionEngine", "describe_image", "extract_text_from_image", "get_vision_engine",
    "AudioTranscription", "HearingEngine", "SpeechSegment", "get_hearing_engine",
    "DocumentExtract", "DocumentReader", "get_document_reader",
]
