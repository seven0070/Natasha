"""Request bodies for the API (responses are plain dicts produced by the subsystems)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LoginBody(BaseModel):
    passphrase: str = Field(min_length=1)
    client: str = "web"


class SetupBody(BaseModel):
    passphrase: str = Field(min_length=8)
    owner_id: str = "owner"


class ChangePassphraseBody(BaseModel):
    current: str
    new: str = Field(min_length=8)


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=200_000)
    conversation_id: str = ""
    mission_id: str = ""
    images: list[str] = Field(default_factory=list)
    documents: list[dict[str, str]] = Field(default_factory=list)
    extra_instructions: str = ""


class ToolCallBody(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    approval_id: str = ""
    actor: str = "owner"


class MissionBody(BaseModel):
    objective: str = Field(min_length=1, max_length=20_000)
    title: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    verification_plan: list[str] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    auto_run: bool = True


class MemoryBody(BaseModel):
    kind: str
    content: str
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.8
    retention: str = "long_term"
    source: str = "owner"


class CorrectMemoryBody(BaseModel):
    memory_id: str
    content: str = Field(min_length=1)
    reason: str = ""


class ConnectBody(BaseModel):
    credential_ref: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ApprovalDecisionBody(BaseModel):
    note: str = ""
    ttl_seconds: int = 900


class CreationBody(BaseModel):
    kind: str
    brief: str
    options: dict[str, Any] = Field(default_factory=dict)


class CredentialBody(BaseModel):
    name: str
    kind: str = "api_key"
    secret: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class VoiceBody(BaseModel):
    text: str
    voice: str = ""
    out_path: str = ""
    play: bool = False


class SettingsBody(BaseModel):
    updates: dict[str, Any] = Field(default_factory=dict)


class SkillRunBody(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    version: str = ""


class DelegationBody(BaseModel):
    role: str
    objective: str
    context: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float | None = None


class BrowserBody(BaseModel):
    url: str
    selector: str = ""
    text: str = ""
    max_chars: int = 20000


class MCPInstallBody(BaseModel):
    name: str
    command: list[str] = Field(default_factory=list)
    url: str = ""
    transport: str = "stdio"
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class MCPCallBody(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class IntegrationBody(BaseModel):
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class VoiceTurnBody(BaseModel):
    """One pass around the voice loop, driven by text, a file or the host microphone."""

    text: str = ""
    path: str = ""
    conversation_id: str = ""
    mission_id: str = ""
    language: str = ""
    seconds: float = 5.0
    speak: bool = True


class VisionAnalyseBody(BaseModel):
    """An image to look at: either a path on disk or an inline data URL."""

    path: str = ""
    data_url: str = ""
    prompt: str = ""


class VisionOcrBody(BaseModel):
    """Text extraction from an image or a document."""

    path: str = ""
    data_url: str = ""


class CameraBody(BaseModel):
    """One frame from a webcam."""

    index: int = 0
    prompt: str = ""


class VideoBody(BaseModel):
    """Sample frames from a video file."""

    path: str = ""
    frames: int = 3
    prompt: str = ""


class UpgradeBody(BaseModel):
    summary: str
    changes: list[dict[str, Any]] = Field(default_factory=list)
    tests: dict[str, Any] = Field(default_factory=dict)
