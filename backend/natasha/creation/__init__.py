"""The creation engine: documents, images, audio and video, made honestly.

Every job knows which stages actually ran and which did not. If no image model is configured, the
artifact says so; if ffmpeg is missing, the video job returns a storyboard and an explanation instead
of a fake MP4. Fabricating success is the one thing this engine never does.
"""

from .engine import (
    CreationEngine,
    CreationJob,
    CreationStage,
    get_creation_engine,
    reset_creation_engine,
)
from .video import Storyboard, StoryboardShot, VideoPipeline

__all__ = ["CreationEngine", "CreationJob", "CreationStage", "get_creation_engine",
           "reset_creation_engine", "Storyboard", "StoryboardShot", "VideoPipeline"]
