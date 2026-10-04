"""Concrete connectors: generic REST/webhook, GitHub, Slack and email."""

from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx

from ..core.risk import RiskLevel
from ..security.policy import Capability
from .base import Connector, ConnectorAction, ConnectorResult


class RestConnector(Connector):
    """Generic authenticated REST caller - the fallback for any HTTP API."""

    name = "rest"

    def __init__(self, *, base_url: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.base_url = base_url or self.settings.get("base_url", "")
        self.actions = [
            ConnectorAction("request", "Call an HTTP endpoint", method="ANY", capability=Capability.NET_HTTP,
                            risk=RiskLevel.MEDIUM, required_arguments=["url"], resource_field="url")
        ]

    async def perform(self, action: str, arguments: dict[str, Any], *, approval_id: str = "",
                      actor: str = "model:main") -> ConnectorResult:
        url = arguments.get("url", "")
        if url and not url.startswith("http") and self.base_url:
            url = f"{self.base_url.rstrip('/')}/{url.lstrip('/')}"
        method = str(arguments.get("method", "GET")).upper()
        headers = {**self.headers(purpose="rest", approval_id=approval_id, actor=actor), **(arguments.get("headers") or {})}
        async with httpx.AsyncClient(timeout=float(arguments.get("timeout_seconds", 30))) as client:
            response = await client.request(method, url, headers=headers,
                                            json=arguments.get("json"), content=arguments.get("body"))
        try:
            data = response.json()
        except Exception:
            text, suspicious = self.mark_untrusted(response.text[:50_000], source=f"rest:{url}")
            return ConnectorResult(response.is_success, data=text, status=response.status_code,
                                   untrusted_text=text, suspicious=suspicious,
                                   error="" if response.is_success else f"HTTP {response.status_code}")
        return ConnectorResult(response.is_success, data=data, status=response.status_code,
                               error="" if response.is_success else f"HTTP {response.status_code}: {str(data)[:200]}")

    def network_host(self, action: str, arguments: dict[str, Any]) -> str:
        """Relative URLs resolve against the configured base_url, so report that base's host."""
        url = str(arguments.get("url", ""))
        if url.startswith("http"):
            return url
        return self.base_url


class WebhookConnector(Connector):
    """Outbound webhook posts (Slack-compatible payloads by default)."""

    name = "webhook"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.actions = [
            ConnectorAction("send", "POST a JSON payload to a webhook URL", method="POST",
                            capability=Capability.NET_HTTP, risk=RiskLevel.MEDIUM, required_arguments=["url"],
                            resource_field="url")
        ]

    async def perform(self, action: str, arguments: dict[str, Any], *, approval_id: str = "",
                      actor: str = "model:main") -> ConnectorResult:
        url = arguments["url"]
        payload = arguments.get("payload") or {"text": arguments.get("text", "")}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload, headers=self.headers(purpose="webhook", approval_id=approval_id, actor=actor))
        return ConnectorResult(response.is_success, data={"status": response.status_code},
                               status=response.status_code,
                               error="" if response.is_success else f"HTTP {response.status_code}")


class GitHubConnector(Connector):
    """GitHub REST API: repositories, issues and pull requests."""

    name = "github"
    credential_ref = "credential://github"
    #: The API this connector always talks to (self-hosted instances set settings.base_url).
    default_base_url = "https://api.github.com"

    def network_host(self, action: str, arguments: dict[str, Any]) -> str:
        return str(self.settings.get("base_url") or self.default_base_url)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.actions = [
            ConnectorAction("list_issues", "List issues in a repository", "GET", "/repos/{repo}/issues",
                            Capability.NET_HTTP, RiskLevel.LOW, ["repo"], resource_field="repo"),
            ConnectorAction("create_issue", "Open an issue", "POST", "/repos/{repo}/issues",
                            Capability.NET_HTTP, RiskLevel.MEDIUM, ["repo", "title"], resource_field="repo"),
            ConnectorAction("list_pull_requests", "List pull requests", "GET", "/repos/{repo}/pulls",
                            Capability.NET_HTTP, RiskLevel.LOW, ["repo"], resource_field="repo"),
            ConnectorAction("get_repo", "Repository metadata", "GET", "/repos/{repo}",
                            Capability.NET_HTTP, RiskLevel.LOW, ["repo"], resource_field="repo"),
        ]

    async def perform(self, action: str, arguments: dict[str, Any], *, approval_id: str = "",
                      actor: str = "model:main") -> ConnectorResult:
        repo = arguments.get("repo", "")
        headers = {"Accept": "application/vnd.github+json", **self.headers(purpose="github", approval_id=approval_id, actor=actor)}
        base = self.settings.get("base_url", "https://api.github.com")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if action == "list_issues":
                    response = await client.get(f"{base}/repos/{repo}/issues",
                                                params={"state": arguments.get("state", "open"),
                                                        "per_page": arguments.get("limit", 20)},
                                                headers=headers)
                elif action == "create_issue":
                    response = await client.post(f"{base}/repos/{repo}/issues",
                                                 json={"title": arguments["title"], "body": arguments.get("body", "")},
                                                 headers=headers)
                elif action == "list_pull_requests":
                    response = await client.get(f"{base}/repos/{repo}/pulls",
                                                params={"state": arguments.get("state", "open")}, headers=headers)
                elif action == "get_repo":
                    response = await client.get(f"{base}/repos/{repo}", headers=headers)
                else:
                    return ConnectorResult(False, error=f"unknown action {action!r}")
        except httpx.HTTPError as exc:
            return ConnectorResult(False, error=f"{type(exc).__name__}: {exc}")
        if not response.is_success:
            return ConnectorResult(False, status=response.status_code,
                                   error=f"GitHub HTTP {response.status_code}: {response.text[:200]}")
        return ConnectorResult(True, data=response.json(), status=response.status_code)


class SlackConnector(Connector):
    """Slack messaging via a bot token or an incoming webhook."""

    name = "slack"
    credential_ref = "credential://slack"
    default_base_url = "https://slack.com/api"

    def network_host(self, action: str, arguments: dict[str, Any]) -> str:
        """A webhook post goes to the webhook host; everything else to the API host."""
        webhook = str(arguments.get("webhook_url") or self.settings.get("webhook_url", ""))
        if webhook and action == "post_message":
            return webhook
        return str(self.settings.get("base_url") or self.default_base_url)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.actions = [
            ConnectorAction("post_message", "Post a message to a channel", "POST", "chat.postMessage",
                            Capability.NET_HTTP, RiskLevel.MEDIUM, ["channel", "text"],
                            resource_field="channel"),
            ConnectorAction("list_channels", "List public channels", "GET", "conversations.list",
                            Capability.NET_HTTP, RiskLevel.LOW, [], resource_field="webhook_url"),
        ]

    async def perform(self, action: str, arguments: dict[str, Any], *, approval_id: str = "",
                      actor: str = "model:main") -> ConnectorResult:
        webhook = arguments.get("webhook_url") or self.settings.get("webhook_url", "")
        if action == "post_message" and webhook and not self.credential_ref:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(webhook, json={"text": arguments.get("text", "")})
            return ConnectorResult(response.is_success, data={"status": response.status_code},
                                   status=response.status_code,
                                   error="" if response.is_success else f"HTTP {response.status_code}")
        headers = self.headers(purpose="slack", approval_id=approval_id, actor=actor)
        base = self.settings.get("base_url", "https://slack.com/api")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if action == "post_message":
                    response = await client.post(f"{base}/chat.postMessage",
                                                 json={"channel": arguments["channel"], "text": arguments["text"]},
                                                 headers=headers)
                elif action == "list_channels":
                    response = await client.get(f"{base}/conversations.list", headers=headers)
                else:
                    return ConnectorResult(False, error=f"unknown action {action!r}")
        except httpx.HTTPError as exc:
            return ConnectorResult(False, error=f"{type(exc).__name__}: {exc}")
        payload = response.json() if response.content else {}
        ok = response.is_success and payload.get("ok", True)
        return ConnectorResult(bool(ok), data=payload, status=response.status_code,
                               error="" if ok else str(payload.get("error", f"HTTP {response.status_code}")))


class EmailConnector(Connector):
    """SMTP email (send) with optional IMAP listing left to a plugin."""

    name = "email"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.actions = [
            ConnectorAction("send", "Send an email", method="SMTP", capability=Capability.NET_SOCKET,
                            risk=RiskLevel.HIGH, required_arguments=["to", "subject", "body"]),
        ]

    def network_host(self, action: str, arguments: dict[str, Any]) -> str:
        """SMTP sends go to the *configured* mail server, not to the recipient's domain."""
        return str(self.settings.get("smtp_host", ""))

    async def perform(self, action: str, arguments: dict[str, Any], *, approval_id: str = "",
                      actor: str = "model:main") -> ConnectorResult:
        if action != "send":
            return ConnectorResult(False, error=f"unknown action {action!r}")
        host = self.settings.get("smtp_host", "")
        if not host:
            return ConnectorResult(False, error="smtp_host is not configured")
        message = EmailMessage()
        message["From"] = self.settings.get("from_address", "natasha@localhost")
        message["To"] = arguments["to"]
        message["Subject"] = arguments["subject"]
        message.set_content(arguments["body"])
        try:
            port = int(self.settings.get("smtp_port", 587))
            with smtplib.SMTP(host, port, timeout=30) as client:
                if self.settings.get("starttls", True):
                    client.starttls()
                username = self.settings.get("username", "")
                if username and self.broker is not None and self.credential_ref:
                    with self.broker.issue(self.credential_ref, purpose="email", actor="system") as handle:
                        client.login(username, handle.value)
                client.send_message(message)
        except Exception as exc:
            return ConnectorResult(False, error=f"{type(exc).__name__}: {exc}")
        return ConnectorResult(True, data={"sent": True, "to": arguments["to"]})


def default_connectors() -> list[Connector]:
    return [RestConnector(), WebhookConnector(), GitHubConnector(), SlackConnector(), EmailConnector()]
