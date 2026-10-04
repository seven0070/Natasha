"""OAuth 2.0 flows: authorization code, PKCE, device authorization and refresh.

Uses ``httpx`` when available and falls back to ``urllib`` otherwise, so credential plumbing works
in minimal installs. Tokens are returned as :class:`TokenSet` and stored encrypted by the vault.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..core import CredentialError
from ..core.clock import iso, utcnow
from ..core.paths import get_paths

DEFAULT_TIMEOUT = 30.0


class OAuthError(CredentialError):
    """OAuth-specific failure (invalid grant, expired device code, ...)."""


@dataclass
class PKCEChallenge:
    verifier: str
    challenge: str
    method: str = "S256"

    @classmethod
    def generate(cls) -> "PKCEChallenge":
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return cls(verifier=verifier, challenge=challenge)


@dataclass
class TokenSet:
    access_token: str = ""
    refresh_token: str = ""
    token_type: str = "Bearer"
    expires_in: int = 0
    scope: str = ""
    id_token: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    obtained_at: str = field(default_factory=iso)

    @property
    def expires_at(self) -> float:
        return time.time() + max(0, self.expires_in)

    def is_expired(self, *, skew: int = 60) -> bool:
        return bool(self.expires_in and time.time() >= self.expires_at - skew)

    def to_secret(self) -> dict[str, Any]:
        return {
            "value": self.access_token,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "scope": self.scope,
            "obtained_at": self.obtained_at,
        }

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> "TokenSet":
        return cls(
            access_token=payload.get("access_token", ""),
            refresh_token=payload.get("refresh_token", ""),
            token_type=payload.get("token_type", "Bearer"),
            expires_in=int(payload.get("expires_in", 0) or 0),
            scope=payload.get("scope", ""),
            id_token=payload.get("id_token", ""),
            raw=payload,
        )


def _request(url: str, *, data: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
             method: str = "POST", timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    body = urllib.parse.urlencode(data or {}).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - explicit user-configured URL
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise OAuthError(f"token endpoint returned HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise OAuthError(f"token endpoint unreachable: {exc}") from exc
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise OAuthError(f"token endpoint returned non-JSON response: {raw[:200]}") from exc


class OAuth2Client:
    """Provider-agnostic OAuth2 helper."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str = "",
        authorize_url: str = "",
        token_url: str = "",
        device_url: str = "",
        redirect_uri: str = "http://127.0.0.1:8765/callback",
        scopes: list[str] | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.device_url = device_url or token_url
        self.redirect_uri = redirect_uri
        self.scopes = scopes or []

    # -- authorization code / PKCE -------------------------------------------- #
    def authorization_url(self, *, state: str = "", pkce: PKCEChallenge | None = None, extra: dict[str, str] | None = None) -> tuple[str, str, PKCEChallenge]:
        """Build the consent URL. Returns ``(url, state, pkce)``."""
        pkce = pkce or PKCEChallenge.generate()
        state = state or secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": pkce.challenge,
            "code_challenge_method": pkce.method,
        }
        params.update(extra or {})
        return f"{self.authorize_url}?{urllib.parse.urlencode(params)}", state, pkce

    def exchange_code(self, code: str, *, pkce: PKCEChallenge | None = None, state: str = "") -> TokenSet:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
        }
        if pkce is not None:
            payload["code_verifier"] = pkce.verifier
        if self.client_secret:
            payload["client_secret"] = self.client_secret
        return TokenSet.from_response(_request(self.token_url, data=payload))

    def refresh(self, refresh_token: str) -> TokenSet:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }
        if self.client_secret:
            payload["client_secret"] = self.client_secret
        tokens = TokenSet.from_response(_request(self.token_url, data=payload))
        if not tokens.refresh_token:
            tokens.refresh_token = refresh_token  # many providers omit it on refresh
        return tokens

    # -- device flow ----------------------------------------------------------- #
    def start_device_flow(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"client_id": self.client_id, "scope": " ".join(self.scopes)}
        payload.update(extra or {})
        data = _request(self.device_url, data=payload)
        if "device_code" not in data:
            raise OAuthError(f"device authorization failed: {str(data)[:200]}")
        data["expires_at"] = iso(utcnow())
        return data

    def poll_device_flow(self, device_code: str, *, interval: int = 5, timeout: int = 600) -> TokenSet:
        """Poll until the owner completes consent (RFC 8628 semantics)."""
        deadline = time.time() + timeout
        payload = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": self.client_id,
        }
        while time.time() < deadline:
            try:
                return TokenSet.from_response(_request(self.token_url, data=payload))
            except OAuthError as exc:
                message = str(exc).lower()
                if "authorization_pending" in message or "slow_down" in message:
                    time.sleep(interval)
                    continue
                raise
        raise OAuthError("device authorization timed out")

    # -- revocation ------------------------------------------------------------ #
    def revoke(self, token: str, *, revoke_url: str = "") -> bool:
        url = revoke_url or self.token_url.replace("/token", "/revoke")
        try:
            _request(url, data={"token": token, "client_id": self.client_id})
            return True
        except OAuthError:
            return False


def store_token_set(reference: str, tokens: TokenSet, *, provider: str, scopes: list[str] | None = None,
                    actor: str = "owner") -> Any:
    """Persist an OAuth token set through the credential manager."""
    from .manager import get_credential_manager

    return get_credential_manager().store(
        reference, tokens.to_secret(), kind="oauth2", provider=provider,
        scopes=scopes or tokens.scope.split(), label=f"{provider} OAuth token",
        actor=actor,
    )


def default_redirect_uri() -> str:
    return "http://127.0.0.1:8765/callback"


def token_cache_path() -> str:
    return str(get_paths().ensure().keys / "oauth_cache.json")
