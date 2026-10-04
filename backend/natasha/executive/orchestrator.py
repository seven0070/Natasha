"""The executive loop: one turn of Natasha's life."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from ..core import ApprovalRequired, NatashaError, ValidationError, VerificationFailed, new_id
from ..core.clock import iso
from ..core.risk import RiskLevel, max_risk
from ..events import EventKind, EventLog, get_event_log
from ..security.injection import ContentTrust, ExternalContent
from ..security.policy import Capability
from ..tools.base import ToolContext
from .context import ContextBuilder, PreparedContext

#: How many model/tool round trips one turn may take before Natasha stops and answers honestly.
MAX_TOOL_ROUNDS = 6
#: Tools whose results are always treated as external content.
UNTRUSTED_TOOLS = {"http_fetch", "read_document", "recall", "mcp"}

#: The offline placeholder provider. It is honest about what it is, and so is the executive.
OFFLINE_PLACEHOLDER_PROVIDER = "echo"
OFFLINE_PLACEHOLDER_NOTICE = (
    "**No model provider is configured yet** - this reply came from the offline echo placeholder, "
    "which only repeats what it received. Point Natasha at a local model (Ollama, llama.cpp, LM Studio, "
    "vLLM) or add a cloud credential in Settings to get real answers."
)


def _response_text(response: Any) -> str:
    """Read the assistant text from any adapter's response shape."""
    for attribute in ("content", "text", "output_text"):
        value = getattr(response, attribute, None)
        if isinstance(value, str):
            return value
    return ""


def _response_usage(response: Any) -> dict[str, Any]:
    """Normalise token/cost information across adapters."""
    usage = getattr(response, "usage", None)
    if usage is not None:
        return usage.to_dict() if hasattr(usage, "to_dict") else dict(usage)
    return {"input_tokens": int(getattr(response, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(response, "output_tokens", 0) or 0),
            "cost_usd": float(getattr(response, "cost_usd", 0.0) or 0.0),
            "latency_ms": float(getattr(response, "latency_ms", 0.0) or 0.0)}


@dataclass
class Turn:
    """One conversation turn."""

    id: str = field(default_factory=lambda: new_id("trn"))
    message: str = ""
    conversation_id: str = ""
    actor: str = "owner"
    mission_id: str = ""
    attachments: list[ExternalContent] = field(default_factory=list)
    created_at: str = field(default_factory=iso)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnResult:
    """What the turn produced, including the honest bits."""

    turn_id: str
    reply: str = ""
    model: str = ""
    provider: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    verifications: list[dict[str, Any]] = field(default_factory=list)
    approvals_requested: list[dict[str, Any]] = field(default_factory=list)
    memory_writes: list[str] = field(default_factory=list)
    mission_id: str = ""
    risk: RiskLevel = RiskLevel.LOW
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    truncated: bool = False
    offline_placeholder: bool = False
    rounds: int = 0
    duration_ms: float = 0.0
    citations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"turn_id": self.turn_id, "reply": self.reply, "model": self.model, "provider": self.provider,
                "tool_calls": self.tool_calls, "verifications": self.verifications,
                "approvals_requested": self.approvals_requested, "memory_writes": self.memory_writes,
                "mission_id": self.mission_id, "risk": self.risk.name, "usage": self.usage,
                "error": self.error, "truncated": self.truncated,
                "offline_placeholder": self.offline_placeholder, "rounds": self.rounds,
                "duration_ms": round(self.duration_ms, 2), "citations": self.citations}


class Executive:
    """Runs turns: context, model, tools, verification, repair, answer."""

    def __init__(
        self,
        *,
        brain: Any = None,
        tools: Any = None,
        memory: Any = None,
        world: Any = None,
        working: Any = None,
        missions: Any = None,
        supervisor: Any = None,
        verification: Any = None,
        recovery: Any = None,
        approvals: Any = None,
        log: EventLog | None = None,
        identity: Any = None,
        affect: Any = None,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        self.brain = brain
        self.tools = tools
        self.memory = memory
        self.world = world
        self.working = working
        self.missions = missions
        self.supervisor = supervisor
        self.verification = verification
        self.recovery = recovery
        self.approvals = approvals
        self.identity = identity
        self.affect = affect
        self.log = log or get_event_log()
        self.context_builder = ContextBuilder(memory=memory, world=world, working=working)
        self.max_tool_rounds = max_tool_rounds
        self.conversations: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ helpers
    def tool_context(self, turn: Turn) -> ToolContext:
        return ToolContext(
            actor=turn.actor, mission_id=turn.mission_id, trace_id=turn.id, task=turn.message[:200],
            policy=getattr(self.tools, "policy", None), memory=self.memory, brain=self.brain,
            world=self.world, broker=getattr(self.tools, "broker", None), approvals=self.approvals,
            workspace=None, extra={"conversation_id": turn.conversation_id, "turn_id": turn.id},
        )

    def _history(self, conversation_id: str, *, limit: int = 12) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.conversations.get(conversation_id, []))[-limit:]

    def _remember_turn(self, conversation_id: str, user_message: str, reply: str) -> None:
        with self._lock:
            history = self.conversations.setdefault(conversation_id, [])
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": reply})
            self.conversations[conversation_id] = history[-40:]

    def _llm_messages(self, context: PreparedContext) -> list[Any]:
        """Convert the prepared context into the brain's message objects."""
        from ..brain.adapters.base import ChatMessage

        messages: list[ChatMessage] = []
        built = context.to_model_messages()
        for item in built:
            images = item.get("images") or []
            messages.append(ChatMessage(role=item["role"], content=item.get("content", ""), images=images))
        return messages

    # ------------------------------------------------------------------ the turn
    async def run_turn(self, turn: Turn, *, stream: bool = False) -> TurnResult:
        """Execute one turn to completion."""
        import time

        started = time.perf_counter()
        result = TurnResult(turn_id=turn.id, mission_id=turn.mission_id)
        self.log.append(EventKind.MESSAGE, {"action": "user_message", "conversation_id": turn.conversation_id,
                                            "chars": len(turn.message),
                                            "attachments": [block.source for block in turn.attachments]},
                        actor=turn.actor, source="executive", trace_id=turn.id, risk=RiskLevel.LOW)
        try:
            result = await self._run_turn_inner(turn, result)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.reply = result.reply or "I could not complete that request. " + result.error
            self.log.append(EventKind.FAILURE, {"turn_id": turn.id, "error": result.error[:400]},
                            actor=turn.actor, source="executive", trace_id=turn.id, risk=RiskLevel.MEDIUM)
        result.duration_ms = (time.perf_counter() - started) * 1000
        self._remember_turn(turn.conversation_id or turn.id, turn.message, result.reply)
        if turn.mission_id:
            self.log.append(EventKind.MISSION, {"action": "turn_linked", "turn_id": turn.id},
                            actor=turn.actor, source="executive", mission_id=turn.mission_id, trace_id=turn.id)
        return result

    async def _run_turn_inner(self, turn: Turn, result: TurnResult) -> TurnResult:
        if self.brain is None:
            raise NatashaError("no brain is attached to the executive")

        history = self._history(turn.conversation_id) if turn.conversation_id else []
        available = self.tools.names() if self.tools is not None else []
        context = self.context_builder.build(
            turn.message, history=history, attachments=turn.attachments, mission_id=turn.mission_id,
            tools_available=available,
        )
        result.citations = [{"source": hit["memory"]["provenance"].get("source", "memory"),
                             "kind": hit["memory"]["kind"], "id": hit["memory"]["id"]}
                            for hit in context.memory_hits[:5]]

        tool_schemas = None
        if self.tools is not None:
            tool_schemas = self.tools.llm_tools()

        rounds = 0
        reply = ""
        while True:
            rounds += 1
            request_kwargs: dict[str, Any] = {
                "messages": self._llm_messages(context),
                "task": "chat",
                "actor": turn.actor,
                "trace_id": turn.id,
                "mission_id": turn.mission_id,
            }
            if tool_schemas:
                request_kwargs["tools"] = tool_schemas
            response = await self._complete(**request_kwargs)
            reply = _response_text(response)
            result.model = str(getattr(response, "model", "") or "")
            result.provider = str(getattr(response, "provider", "") or "")
            result.usage = _response_usage(response)
            calls = list(getattr(response, "tool_calls", []) or [])
            if not calls:
                break
            if rounds > self.max_tool_rounds:
                # The budget is spent: do not run further tools, and say so in the answer.
                result.truncated = True
                result.reply = (reply.strip() + "\n\n" if reply.strip() else "") + \
                    "(I stopped here: this turn reached its tool-step budget. Ask me to continue.)"
                result.rounds = rounds
                self.working_push(turn, result)
                return result

            # The model asked for tools: run each one through the choke point.
            tool_messages: list[dict[str, Any]] = []
            for call in calls:
                record = await self._execute_tool_call(turn, call, result)
                result.tool_calls.append(record)
                tool_messages.append({"role": "tool", "name": record["tool"],
                                      "content": _tool_message(record)})
            context.messages.append({"role": "assistant", "content": reply})
            context.messages.extend(tool_messages)

        result.rounds = rounds
        result.reply = self._final_text(reply, result)
        self.working_push(turn, result)
        return result

    def _final_text(self, reply: str, result: TurnResult) -> str:
        """Never let an offline placeholder read like a real answer."""
        text = (reply or "").strip()
        if not text:
            text = "I have nothing to add."
        if result.provider == OFFLINE_PLACEHOLDER_PROVIDER:
            result.offline_placeholder = True
            text = (OFFLINE_PLACEHOLDER_NOTICE + "\n\n" + text)
        return text

    async def _complete(self, **kwargs: Any) -> Any:
        """One routed model call, with the recovery engine as a safety net."""
        from ..brain import CompletionRequest

        request = CompletionRequest(**{key: value for key, value in kwargs.items()
                                       if key in CompletionRequest.__dataclass_fields__})

        async def call() -> Any:
            return await self.brain.complete(request)

        if self.recovery is None:
            return await call()
        outcome = await self.recovery.attempt(f"turn:{request.trace_id or 'chat'}", call,
                                              mission_id=request.mission_id, trace_id=request.trace_id,
                                              actor=request.actor,
                                              alternative=lambda: self.brain.complete(request))
        if not outcome.recovered:
            raise NatashaError(outcome.error or "model call failed after repairs")
        return outcome.result

    async def _execute_tool_call(self, turn: Turn, call: Any, result: TurnResult) -> dict[str, Any]:
        name = getattr(call, "name", "") or (call.get("name") if isinstance(call, dict) else "")
        arguments = getattr(call, "arguments", None)
        if arguments is None and isinstance(call, dict):
            arguments = call.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
        record: dict[str, Any] = {"tool": name, "arguments": arguments, "ok": False, "error": ""}
        if self.tools is None:
            record["error"] = "no tool registry attached"
            return record
        approval_id = self._matching_approval(name, arguments, actor=turn.actor)
        tool_result = await self.tools.execute(name, arguments, context=self.tool_context(turn),
                                              approval_id=approval_id)
        record["ok"] = bool(getattr(tool_result, "ok", False))
        output = getattr(tool_result, "output", None)
        record["error"] = getattr(tool_result, "error", "")
        record["redacted"] = bool(getattr(tool_result, "redacted", False))
        record["artifacts"] = list(getattr(tool_result, "artifacts", []) or [])

        # MEMORY UPDATE: a turn that stores a memory says so, so the console (and a verifier) can
        # see the write instead of having to trust the model's summary of it.
        if record["ok"] and isinstance(output, dict) and output.get("memory_id"):
            result.memory_writes.append(str(output["memory_id"]))
        if not record["ok"] and "approval" in record["error"].lower():
            metadata = getattr(tool_result, "metadata", {}) or {}
            record["approval_request_id"] = metadata.get("approval_request_id", "")
            result.approvals_requested.append({"tool": name, "arguments": arguments,
                                               "request_id": record["approval_request_id"],
                                               "reason": record["error"]})
        # Tool output is data with a trust level; the model sees it fenced as such.
        if record["ok"]:
            unsafe = name.split("__")[0] in UNTRUSTED_TOOLS
            trust = ContentTrust.EXTERNAL if unsafe else ContentTrust.TOOL_OUTPUT
            block = ExternalContent(text=json.dumps(output, default=str)[:20000],
                                    source=f"tool:{name}", trust=trust)
            record["suspicious"] = block.suspicious
            record["findings"] = [item["pattern"] for item in (block.report.findings if block.report else [])]
            record["content"] = block.render(max_chars=8000)
        if self.affect is not None:
            try:
                self.affect.note_event("tool_failed" if not record["ok"] else "tool_succeeded",
                                       detail=name)
            except Exception:
                pass
        return record

    def _matching_approval(self, tool_name: str, arguments: dict[str, Any], *, actor: str) -> str:
        """Reuse an approval the owner already granted for *exactly* this action.

        The request fingerprint covers operation, arguments and resource, so an approval given for one
        command can never authorise a different one - and it is consumed on first use by the registry.
        """
        if self.approvals is None or self.tools is None:
            return ""
        try:
            target = self.tools.approval_target(tool_name, arguments)
        except Exception:
            return ""
        for request in self.approvals.approved_requests(operation=target["operation"], actor=actor):
            if request.matches(target["operation"], target.get("arguments"), resource=target.get("resource", "")):
                return request.id
        return ""

    async def resolve_approval(self, request_id: str, *, decision: str = "approve", note: str = "",
                               actor: str = "owner") -> dict[str, Any]:
        """Record the owner's decision, and resume whatever was waiting for it.

        This is the only path that turns a pending request into work: approval comes from the owner
        actor, the decision is audited, and a mission step that was blocked is re-armed and resumed.
        """
        if self.approvals is None:
            raise NatashaError("no approval engine is attached to the executive")
        if decision not in ("approve", "deny"):
            raise ValidationError("decision must be 'approve' or 'deny'")
        if decision == "approve":
            self.approvals.approve(request_id, decided_by=actor, note=note)
        else:
            self.approvals.deny(request_id, decided_by=actor, note=note)
        request = self.approvals.get_request(request_id)
        status = self.approvals.status(request_id)
        resumed: dict[str, Any] | None = None
        if decision == "approve" and request.mission_id and self.missions is not None:
            rearm = self.missions.rearm(request.mission_id, actor=actor, step_id=request.step_id)
            if rearm["rearmed"]:
                result = await self.missions.resume(request.mission_id, actor=actor)
                resumed = {"mission_id": request.mission_id, "state": result.state.value, "ok": result.ok,
                           "rearmed": rearm["rearmed"], "error": result.error}
            else:
                resumed = {"mission_id": request.mission_id, "state": "not_rearmed",
                           "refused": rearm["refused"]}
        return {"request_id": request_id, "status": status.value, "operation": request.operation,
                "mission": resumed}

    def working_push(self, turn: Turn, result: TurnResult) -> None:
        if self.working is None:
            return
        try:
            if result.tool_calls:
                summary = ", ".join(call["tool"] for call in result.tool_calls[:5])
                self.working.push(f"Ran tools: {summary}", role="observation", mission_id=turn.mission_id)
            if result.error:
                self.working.push(f"Turn error: {result.error[:200]}", role="note", mission_id=turn.mission_id)
        except Exception:
            pass

    # ------------------------------------------------------------------ streaming
    async def stream_turn(self, turn: Turn) -> AsyncIterator[dict[str, Any]]:
        """Stream a turn: model tokens as they arrive, tool activity as it happens.

        Real streaming: the brain's stream() is used when it can serve the request; if no provider can
        stream, the non-streaming call runs and the client is told that the answer arrives in one
        piece (``stream_unavailable``). Nothing is faked as incremental.
        """
        import time

        started = time.perf_counter()
        result = TurnResult(turn_id=turn.id, mission_id=turn.mission_id)
        yield {"type": "turn_started", "turn_id": turn.id}
        self.log.append(EventKind.MESSAGE, {"action": "user_message", "conversation_id": turn.conversation_id,
                                            "chars": len(turn.message), "streamed": True},
                        actor=turn.actor, source="executive", trace_id=turn.id, risk=RiskLevel.LOW)
        try:
            if self.brain is None:
                raise NatashaError("no brain is attached to the executive")
            history = self._history(turn.conversation_id) if turn.conversation_id else []
            available = self.tools.names() if self.tools is not None else []
            context = self.context_builder.build(turn.message, history=history,
                                                 attachments=turn.attachments,
                                                 mission_id=turn.mission_id, tools_available=available)
            tool_schemas = self.tools.llm_tools() if self.tools is not None else None
            rounds = 0
            reply = ""
            while True:
                rounds += 1
                request_kwargs: dict[str, Any] = {
                    "messages": self._llm_messages(context), "task": "chat", "actor": turn.actor,
                    "trace_id": turn.id, "mission_id": turn.mission_id,
                }
                if tool_schemas:
                    request_kwargs["tools"] = tool_schemas
                chunks: list[str] = []
                calls: list[Any] = []
                streamed = False
                try:
                    async for chunk in self._stream(**request_kwargs):
                        delta = getattr(chunk, "delta", "") or ""
                        if delta:
                            streamed = True
                            chunks.append(delta)
                            yield {"type": "token", "text": delta}
                        calls.extend(list(getattr(chunk, "tool_calls", []) or []))
                        result.model = str(getattr(chunk, "model", "") or result.model)
                        result.provider = str(getattr(chunk, "provider", "") or result.provider)
                except Exception as exc:
                    if chunks:
                        raise
                    # No provider could stream: fall back to one non-streamed completion, and say so.
                    yield {"type": "stream_unavailable",
                           "error": f"{type(exc).__name__}: {exc}",
                           "note": "the reply will arrive in one piece"}
                    response = await self._complete(**request_kwargs)
                    text_piece = _response_text(response)
                    result.model = str(getattr(response, "model", "") or result.model)
                    result.provider = str(getattr(response, "provider", "") or result.provider)
                    result.usage = _response_usage(response) or result.usage
                    calls = list(getattr(response, "tool_calls", []) or [])
                    if text_piece:
                        chunks.append(text_piece)
                        yield {"type": "token", "text": text_piece}
                reply = "".join(chunks)
                if not calls:
                    break
                if rounds > self.max_tool_rounds:
                    result.truncated = True
                    result.reply = self._final_text(
                        reply + (" " if reply else "") +
                        "(I stopped here: this turn reached its tool-step budget.)", result)
                    result.rounds = rounds
                    yield {"type": "turn_finished", "result": result.to_dict()}
                    self._remember_turn(turn.conversation_id or turn.id, turn.message, result.reply)
                    return
                tool_messages: list[dict[str, Any]] = []
                for call in calls:
                    record = await self._execute_tool_call(turn, call, result)
                    result.tool_calls.append(record)
                    yield {"type": "tool", "tool": record["tool"], "ok": record["ok"],
                           "error": record["error"],
                           "approval_request_id": record.get("approval_request_id", ""),
                           "suspicious": record.get("suspicious", False)}
                    tool_messages.append({"role": "tool", "name": record["tool"],
                                          "content": _tool_message(record)})
                context.messages.append({"role": "assistant", "content": reply})
                context.messages.extend(tool_messages)
                yield {"type": "round", "round": rounds, "tools": [call["tool"] for call in result.tool_calls]}
            result.rounds = rounds
            result.reply = self._final_text(reply, result)
            yield {"type": "turn_finished", "result": result.to_dict()}
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.reply = result.reply or ("I could not complete that request. " + result.error)
            self.log.append(EventKind.FAILURE, {"turn_id": turn.id, "error": result.error[:400]},
                            actor=turn.actor, source="executive", trace_id=turn.id, risk=RiskLevel.MEDIUM)
            yield {"type": "error", "error": result.error, "reply": result.reply}
            yield {"type": "turn_finished", "result": result.to_dict()}
        finally:
            result.duration_ms = (time.perf_counter() - started) * 1000
            self._remember_turn(turn.conversation_id or turn.id, turn.message, result.reply)

    async def _stream(self, **kwargs: Any) -> AsyncIterator[Any]:
        """Streaming completion (no repair ladder: a partial answer cannot be replayed safely)."""
        from ..brain import CompletionRequest

        request = CompletionRequest(**{key: value for key, value in kwargs.items()
                                       if key in CompletionRequest.__dataclass_fields__})
        async for chunk in self.brain.stream(request):
            yield chunk

    # ------------------------------------------------------------------ missions
    async def start_mission(self, objective: str, *, title: str = "", criteria: list[str] | None = None,
                            verification_plan: list[str] | None = None, scope: list[str] | None = None,
                            actor: str = "owner", **kwargs: Any) -> dict[str, Any]:
        """Create and run a mission from a conversation."""
        if self.missions is None:
            raise NatashaError("no mission engine is attached to the executive")
        mission = self.missions.create(objective, title=title, success_criteria=criteria,
                                       verification_plan=verification_plan, scope=scope,
                                       created_by=actor, **kwargs)
        result = await self.missions.run(mission.id, actor=actor)
        return result.to_dict()


def _tool_message(record: dict[str, Any]) -> str:
    """What the model sees as the result of a tool call - fenced when untrusted."""
    if record.get("ok"):
        return record.get("content") or json.dumps(record.get("output"), default=str)[:4000]
    if record.get("approval_request_id"):
        return (f"ACTION NOT PERFORMED: this needs the owner's explicit approval "
                f"(request {record['approval_request_id']}). Tell the owner what you want to do and why.")
    return f"TOOL FAILED: {record.get('error', 'unknown error')}"


def _chunk_text(text: str, *, size: int = 120) -> list[str]:
    return [text[index:index + size] for index in range(0, len(text), size)] or [""]


_EXECUTIVE: Executive | None = None
_LOCK = threading.Lock()


def get_executive(**kwargs: Any) -> Executive:
    global _EXECUTIVE
    with _LOCK:
        if _EXECUTIVE is None:
            _EXECUTIVE = Executive(**kwargs)
        return _EXECUTIVE


def reset_executive() -> None:
    global _EXECUTIVE
    with _LOCK:
        _EXECUTIVE = None
