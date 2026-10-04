"""Model routing: task, modality, quality, latency, cost, privacy, hardware, context, availability.

The router is explainable by construction: every decision (and every fallback step) records *why*
the model was chosen, which the UI shows in the provider panel and the event log carries for audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core import ProviderUnavailable
from ..core.clock import iso
from .adapters.base import ProviderAdapter
from .models import Capability, ModelInfo, ModelRegistry
from .registry import ProviderRegistry

TASK_WEIGHTS: dict[str, dict[str, float]] = {
    "chat": {"quality": 0.30, "cost": 0.15, "latency": 0.15, "privacy": 0.25, "availability": 0.15},
    "reasoning": {"quality": 0.45, "cost": 0.05, "latency": 0.05, "privacy": 0.25, "availability": 0.20},
    "coding": {"quality": 0.40, "cost": 0.10, "latency": 0.10, "privacy": 0.25, "availability": 0.15},
    "summarize": {"quality": 0.20, "cost": 0.25, "latency": 0.25, "privacy": 0.20, "availability": 0.10},
    "extract": {"quality": 0.20, "cost": 0.30, "latency": 0.25, "privacy": 0.15, "availability": 0.10},
    "vision": {"quality": 0.35, "cost": 0.15, "latency": 0.15, "privacy": 0.25, "availability": 0.10},
    "embeddings": {"quality": 0.20, "cost": 0.35, "latency": 0.20, "privacy": 0.20, "availability": 0.05},
    "creative": {"quality": 0.38, "cost": 0.12, "latency": 0.10, "privacy": 0.25, "availability": 0.15},
}


class PrivacyPreference(str, Enum):
    LOCAL_ONLY = "local_only"
    PREFER_LOCAL = "prefer_local"
    ANY = "any"


@dataclass
class TaskProfile:
    """What the caller needs."""

    task: str = "chat"
    capability: Capability = Capability.TEXT
    min_quality: float = 0.0
    min_context: int = 0
    max_cost_per_1k: float | None = None
    privacy: PrivacyPreference = PrivacyPreference.PREFER_LOCAL
    max_latency_ms: float | None = None
    requires_tools: bool = False
    model_hint: str = ""
    provider_hint: str = ""

    def weights(self) -> dict[str, float]:
        return TASK_WEIGHTS.get(self.task, TASK_WEIGHTS["chat"])


@dataclass
class RouteExplanation:
    """Why a model was chosen (or not)."""

    chosen: str = ""
    provider: str = ""
    score: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)
    considered: int = 0
    rejected: list[dict[str, str]] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chosen": self.chosen, "provider": self.provider, "score": round(self.score, 4),
            "factors": {k: round(v, 4) for k, v in self.factors.items()},
            "considered": self.considered, "rejected": self.rejected[:20], "fallbacks": self.fallbacks,
            "rationale": self.rationale,
        }


@dataclass
class RoutingDecision:
    model: ModelInfo
    explanation: RouteExplanation
    created_at: str = field(default_factory=iso)

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model.to_dict(), "explanation": self.explanation.to_dict(), "created_at": self.created_at}


class Router:
    """Scores candidate models and builds fallback chains."""

    def __init__(
        self,
        providers: ProviderRegistry,
        *,
        models: ModelRegistry | None = None,
        weights: dict[str, float] | None = None,
        prefer_local: bool = True,
        local_privacy_boost: float = 0.35,
        context_window_floor: int = 8192,
    ) -> None:
        self.providers = providers
        self.models = models or providers.models
        self.default_weights = weights or TASK_WEIGHTS["chat"]
        self.prefer_local = prefer_local
        self.local_privacy_boost = local_privacy_boost
        self.context_window_floor = context_window_floor
        self._usage_quality: dict[str, float] = {}

    # -- scoring --------------------------------------------------------------- #
    def _availability(self, provider: str) -> float:
        health = self.providers.is_healthy(provider)
        if health is None:
            return 0.7  # unknown: usable but not preferred
        return 1.0 if health else 0.0

    def _quality(self, model: ModelInfo) -> float:
        """Base quality, nudged by observed outcomes for this model."""
        observed = self._usage_quality.get(model.key)
        return model.quality if observed is None else round(0.7 * model.quality + 0.3 * observed, 4)

    def _cost(self, model: ModelInfo, *, expected_tokens: int = 2000) -> float:
        if model.is_free:
            return 1.0
        estimate = (expected_tokens / 1000) * (model.cost_per_1k_in * 0.7 + model.cost_per_1k_out * 0.3)
        return max(0.0, 1.0 - min(1.0, estimate / 0.05))  # 5 cents of expected spend == cost score 0

    def _latency(self, model: ModelInfo) -> float:
        if self.providers.is_healthy(model.provider) and model.local:
            return 0.9
        if model.local:
            return 0.7
        return 0.6

    def _privacy(self, model: ModelInfo, preference: PrivacyPreference) -> float:
        if model.privacy_tier == "local":
            score = 1.0
        elif model.privacy_tier == "private_cloud":
            score = 0.6
        else:
            score = 0.35
        if model.local and self.prefer_local:
            score = min(1.0, score + self.local_privacy_boost)
        if preference is PrivacyPreference.LOCAL_ONLY and not model.local:
            return 0.0
        return score

    def score_model(self, model: ModelInfo, profile: TaskProfile, *, expected_tokens: int = 2000) -> tuple[float, dict[str, float]]:
        weights = self.default_weights | (profile.weights() if profile.task else {})
        factors = {
            "quality": self._quality(model),
            "cost": self._cost(model, expected_tokens=expected_tokens),
            "latency": self._latency(model),
            "privacy": self._privacy(model, profile.privacy),
            "availability": self._availability(model.provider),
        }
        total = sum(weights.get(name, 0.0) * value for name, value in factors.items())
        context_fit = min(1.0, model.context_window / max(1, profile.min_context or self.context_window_floor))
        total *= 0.6 + 0.4 * context_fit
        return round(total, 6), factors

    def _eligible(self, model: ModelInfo, profile: TaskProfile) -> tuple[bool, str]:
        if profile.provider_hint and model.provider != profile.provider_hint:
            return False, "provider hint mismatch"
        if not model.supports(profile.capability):
            return False, f"missing capability {profile.capability.value}"
        if profile.requires_tools and not model.supports(Capability.TOOLS):
            return False, "tools required but unsupported"
        if profile.privacy is PrivacyPreference.LOCAL_ONLY and not model.local:
            return False, "privacy requires a local model"
        if profile.min_context and model.context_window < profile.min_context:
            return False, f"context window {model.context_window} < {profile.min_context}"
        if model.quality < profile.min_quality:
            return False, f"quality {model.quality} < {profile.min_quality}"
        if profile.max_cost_per_1k is not None and model.cost_per_1k_in > profile.max_cost_per_1k:
            return False, "above cost ceiling"
        availability = self._availability(model.provider)
        if availability <= 0.0:
            return False, "provider unhealthy"
        return True, ""

    # -- routing --------------------------------------------------------------- #
    def choose(self, profile: TaskProfile | None = None, **kwargs: Any) -> RoutingDecision:
        profile = profile or TaskProfile(**kwargs)
        if profile.model_hint:
            hinted = self.models.get(profile.model_hint, provider=profile.provider_hint)
            if hinted is not None:
                eligible, reason = self._eligible(hinted, profile)
                if eligible:
                    score, factors = self.score_model(hinted, profile)
                    return RoutingDecision(
                        model=hinted,
                        explanation=RouteExplanation(
                            chosen=hinted.id, provider=hinted.provider, score=score, factors=factors, considered=1,
                            rationale=f"explicit model hint {profile.model_hint!r}",
                        ),
                    )
                # A hint that cannot satisfy the profile is recorded, not silently ignored.
                rejected = [{"model": hinted.key, "reason": reason}]
            else:
                rejected = [{"model": profile.model_hint, "reason": "model not discovered"}]
        else:
            rejected = []

        candidates: list[ModelInfo] = []
        for model in self.models.all():
            eligible, reason = self._eligible(model, profile)
            if eligible:
                candidates.append(model)
            elif len(rejected) < 40:
                rejected.append({"model": model.key, "reason": reason})

        if not candidates:
            fallback = self._last_resort(profile)
            if fallback is None:
                raise ProviderUnavailable(
                    "no model can satisfy this request: "
                    + "; ".join(f"{item['model']}: {item['reason']}" for item in rejected[:5])
                )
            score, factors = self.score_model(fallback, profile)
            return RoutingDecision(
                model=fallback,
                explanation=RouteExplanation(
                    chosen=fallback.id, provider=fallback.provider, score=score, factors=factors,
                    considered=len(rejected), rejected=rejected,
                    rationale="no candidate satisfied the constraints; using the offline provider",
                ),
            )

        scored = sorted(((self.score_model(model, profile) + (model,)) for model in candidates),
                        key=lambda item: item[0], reverse=True)
        chain = [model for _, factors, model in scored]
        best_score, best_factors, best = scored[0]
        fallbacks = [f"{model.provider}:{model.id}" for model in chain[1:5]]
        rationale_bits = [
            f"{name}={value:.2f}" for name, value in sorted(best_factors.items(), key=lambda kv: -kv[1])
        ]
        return RoutingDecision(
            model=best,
            explanation=RouteExplanation(
                chosen=best.id, provider=best.provider, score=best_score, factors=best_factors,
                considered=len(candidates), rejected=rejected, fallbacks=fallbacks,
                rationale=f"best weighted match for task={profile.task!r} ({', '.join(rationale_bits)})",
            ),
        )

    def build_fallback_chain(self, decision: RoutingDecision, profile: TaskProfile | None = None, *, depth: int = 3) -> list[ModelInfo]:
        """Ordered alternatives: other healthy models that satisfy the same profile."""
        profile = profile or TaskProfile()
        primary = decision.model
        alternatives = []
        for model in self.models.all():
            if model.key == primary.key:
                continue
            eligible, _ = self._eligible(model, profile)
            if eligible:
                alternatives.append(model)
        alternatives.sort(key=lambda model: self.score_model(model, profile)[0], reverse=True)
        return [primary, *alternatives[: max(0, depth - 1)]]

    def _last_resort(self, profile: TaskProfile) -> ModelInfo | None:
        """The offline echo provider, used only when nothing else can serve the request."""
        if not self.providers.has("echo"):
            return None
        existing = self.models.get("echo-1", provider="echo")
        if existing:
            return existing
        return ModelInfo(
            id="echo-1", provider="echo", local=True, quality=0.1, privacy_tier="local",
            capabilities={Capability.TEXT, Capability.STREAMING, Capability.EMBEDDINGS},
        )

    def adapter_for(self, model: ModelInfo) -> ProviderAdapter:
        return self.providers.get(model.provider)

    def note_outcome(self, model: ModelInfo, *, success: bool, latency_ms: float = 0.0) -> None:
        """Lightweight online adjustment so repeatedly failing models lose priority."""
        current = self._usage_quality.get(model.key, model.quality)
        target = min(1.0, max(0.0, (0.9 if success else 0.2) - min(0.2, latency_ms / 60_000)))
        self._usage_quality[model.key] = round(current * 0.85 + target * 0.15, 4)
