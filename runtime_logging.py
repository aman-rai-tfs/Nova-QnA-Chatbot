"""Console/file diagnostics containing metadata, never prompts or tool contents."""

from __future__ import annotations

import contextvars
import json
import logging
import math
import os
import re
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER = logging.getLogger("nova")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False
_context = contextvars.ContextVar("nova_log_context", default={})
_secrets: set[str] = set()
_secret_lock = threading.Lock()


def register_log_secrets(*values: str) -> None:
    with _secret_lock:
        _secrets.update(value for value in values if isinstance(value, str) and value)


def redact(value: str) -> str:
    with _secret_lock:
        secrets = set(_secrets)
    secrets.update(value for name, value in os.environ.items() if value and
                   re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD)$", name, re.I))
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9_./+=:-]+", r"\1 [REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
    value = re.sub(r"https?://[^\s/]*@", "https://[REDACTED]@", value)
    return value


class _MetadataFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging() -> None:
    """Called by the web app; importing this module has no file side effects."""
    raw_level = os.environ.get("NOVA_LOG_LEVEL", "INFO").upper().strip()
    level = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
             "ERROR": logging.ERROR}.get(raw_level, logging.INFO)
    for handler in LOGGER.handlers[:]:
        LOGGER.removeHandler(handler)
        handler.close()
    LOGGER.setLevel(level)
    formatter = _MetadataFormatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s",
                                   datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    LOGGER.addHandler(console)
    # Keep failures in a diagnostic sink from dumping raw LogRecord internals.
    logging.raiseExceptions = False
    log_file = os.environ.get("NOVA_LOG_FILE", "").strip()
    if log_file:
        try:
            path = Path(log_file).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024,
                                              backupCount=3, encoding="utf-8")
            file_handler.setFormatter(formatter)
            LOGGER.addHandler(file_handler)
            log_event("logging.file_enabled", file=str(path), max_bytes=5 * 1024 * 1024, backups=3)
        except OSError as exc:
            log_event("logging.file_unavailable", level="WARNING", error_type=type(exc).__name__)
    if raw_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        log_event("logging.invalid_level", level="WARNING", using="INFO")


def bind_context(**fields) -> None:
    _context.set({**_context.get(), **{key: value for key, value in fields.items() if value is not None}})


@contextmanager
def log_context(**fields):
    token = _context.set({**_context.get(), **{key: value for key, value in fields.items() if value is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def log_event(event: str, *, level: str = "INFO", **fields) -> None:
    """Only callers' explicit metadata fields are rendered. Never pass payloads."""
    number = getattr(logging, level, logging.INFO)
    if not LOGGER.isEnabledFor(number):
        return
    values = {**_context.get(), **fields}
    parts = [event]
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = redact(value)[:700]
        parts.append(key + "=" + json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str))
    LOGGER.log(number, " ".join(parts))


def error_fields(error) -> dict:
    """Summarize API diagnostics without printing a raw non-JSON response."""
    if isinstance(error, dict):
        return {"error_code": error.get("code"), "error_type": error.get("type")}
    message = str(error)
    if message.startswith("OpenAI request failed:"):
        # The existing browser diagnostic can include a non-JSON upstream body.
        # Keep the error classification in logs rather than echoing that body.
        details = {"error_type": type(error).__name__, "error": "OpenAI request failed; inspect API status and browser diagnostic"}
        for name in ("code", "param"):
            match = re.search(r"\b" + name + r"=([A-Za-z0-9_.\[\]-]+)", message)
            if match:
                details["error_" + name] = match.group(1)
        return details
    for marker in ("non-JSON response:", "non-JSON upload response:"):
        if marker in message:
            message = message.split(marker, 1)[0] + marker + " [body omitted]"
    return {"error_type": type(error).__name__, "error": redact(message)[:500]}


def usage_fields(usage) -> dict:
    if not isinstance(usage, dict):
        return {"usage_available": False}
    fields = {key: value for key in ("input_tokens", "output_tokens", "total_tokens")
              if isinstance((value := usage.get(key)), int) and not isinstance(value, bool)}
    for key, detail in (("input_tokens_details", "cached_tokens"),
                        ("output_tokens_details", "reasoning_tokens")):
        block = usage.get(key)
        if isinstance(block, dict) and isinstance(block.get(detail), int):
            fields[detail] = block[detail]
    return {"usage_available": bool(fields), **fields}


def tool_result_fields(output) -> dict:
    """Inspect bounded JSON for counts/error flags; never return source text."""
    fields: dict = {}
    if output is None:
        return {"result_available": False}
    encoded = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    fields.update(result_available=True, result_chars=len(encoded), result_bytes=len(encoded.encode("utf-8")))

    def visit(value, depth=0):
        if depth > 6:
            return
        if isinstance(value, str):
            if len(value) <= 1024 * 1024 and value.lstrip().startswith(("{", "[")):
                try:
                    visit(json.loads(value), depth + 1)
                except (ValueError, RecursionError):
                    pass
        elif isinstance(value, dict):
            if value.get("isError") or value.get("is_error") or value.get("error"):
                fields["tool_reported_error"] = True
            matches = value.get("matches")
            if isinstance(matches, list):
                fields["passages"] = len(matches)
                fields["passage_chars"] = sum(len(item.get("text", "")) for item in matches
                                               if isinstance(item, dict) and isinstance(item.get("text"), str))
                scores = [item["score"] for item in matches if isinstance(item, dict)
                          and isinstance(item.get("score"), (int, float)) and math.isfinite(item["score"])]
                if scores:
                    fields.update(score_min=round(min(scores), 4), score_max=round(max(scores), 4))
            tools = value.get("tools")
            if isinstance(tools, list):
                fields["tools_discovered"] = len(tools)
            # Only inspect known result envelopes, not arbitrary Jira/guide fields.
            for name in ("content", "structuredContent", "output", "result", "text"):
                if name in value:
                    visit(value[name], depth + 1)
        elif isinstance(value, list):
            for item in value[:100]:
                visit(item, depth + 1)

    visit(output)
    return fields


class RequestMetrics:
    """Summarize one browser request, including quiet waits between SSE events."""

    def __init__(self):
        self.started = time.monotonic()
        self.last_event = self.started
        self.first_text_ms = None
        self.events = Counter()
        self.parts: dict[tuple, int] = {}
        self.items: dict[str, dict] = {}
        self.turn_ids: set[str] = set()
        self.completed_turns: set[str] = set()
        self.tool_count = self.tool_failures = 0
        self.phase = "validating_request"
        self.context = dict(_context.get())
        self._seen_events: set[str] = set()
        self._announced_session = None
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.RLock()

    def start_progress(self) -> None:
        try:
            interval = float(os.environ.get("NOVA_LOG_PROGRESS_SECONDS", "15"))
            if not math.isfinite(interval) or interval < 0 or 0 < interval < 1:
                raise ValueError
        except ValueError:
            interval = 15
            log_event("logging.invalid_progress_interval", level="WARNING", using_seconds=interval)
        if not interval:
            return

        def progress():
            while not self._stop.wait(interval):
                with self._lock:
                    log_event("request.progress", **self.context, phase=self.phase,
                              elapsed_ms=round((time.monotonic() - self.started) * 1000),
                              since_last_event_ms=round((time.monotonic() - self.last_event) * 1000),
                              events=sum(self.events.values()), tools=self.tool_count,
                              output_chars=sum(self.parts.values()))
        self._thread = threading.Thread(target=progress, daemon=True, name="nova-log-progress")
        self._thread.start()

    def observe(self, event_type: str, payload: dict) -> None:
        # A diagnostic must never interrupt the browser's event stream.
        try:
            with self._lock:
                self._observe(event_type, payload)
        except Exception as exc:
            log_event("logging.event_unavailable", level="WARNING", error_type=type(exc).__name__)

    def _observe(self, event_type: str, payload: dict) -> None:
        actual = payload.get("type", event_type)
        event_id = payload.get("event_id")
        if isinstance(event_id, str):
            if event_id in self._seen_events:
                return
            self._seen_events.add(event_id)
        self.last_event = time.monotonic()
        self.events[actual] += 1
        turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
        session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
        session_id = payload.get("session_id") or session.get("id")
        turn_id = payload.get("turn_id") or turn.get("id")
        if isinstance(session_id, str):
            bind_context(session_id=session_id)
        # Root-agent usage must not be mixed with child turns or session totals.
        if isinstance(turn_id, str) and not turn.get("subagent_id"):
            bind_context(turn_id=turn_id)
            self.turn_ids.add(turn_id)
        self.context = dict(_context.get())
        if actual.endswith("output_text.delta") or actual.endswith("output_text.done"):
            text = payload.get("delta" if actual.endswith(".delta") else "text")
            if isinstance(text, str):
                if text and self.first_text_ms is None:
                    self.first_text_ms = round((time.monotonic() - self.started) * 1000)
                    log_event("response.first_text", elapsed_ms=self.first_text_ms)
                key = (payload.get("item_id"), payload.get("output_index"), payload.get("content_index"))
                self.parts[key] = self.parts.get(key, 0) + len(text) if actual.endswith(".delta") else len(text)
                self.phase = "streaming_response"
            return
        if actual == "app.session":
            if self._announced_session == session_id:
                return
            self._announced_session = session_id
            self.phase = "starting_environment"
            log_event("session.created")
        elif actual == "app.environment" or ".environment." in actual:
            status = payload.get("status") or actual.rsplit(".", 1)[-1]
            self.phase = "waiting_for_agent" if status == "connected" else "environment_" + str(status)
            log_event("environment.state", status=status)
        elif actual == "agent.session.requires_action":
            actions = session.get("required_actions", payload.get("required_actions", []))
            self.phase = "handling_required_action"
            log_event("session.requires_action", actions=len(actions) if isinstance(actions, list) else None)
        elif actual in {"agent.session.turn.item.added", "agent.session.turn.item.done"}:
            self._item(payload.get("item"), actual.endswith(".done"))
        elif actual in {"agent.session.turn.created", "agent.session.turn.in_progress"}:
            self.phase = "agent_running"
            log_event("turn.state", status=turn.get("status") or actual.rsplit(".", 1)[-1],
                      queued_ms=self._provider_ms(turn, "created_at", "started_at"))
        elif actual in {"agent.session.turn.completed", "agent.session.turn.failed", "agent.session.turn.cancelled"}:
            status = actual.rsplit(".", 1)[-1]
            self.phase = "turn_" + status
            if status == "completed" and isinstance(turn_id, str):
                self.completed_turns.add(turn_id)
            log_event("turn.finished", level="INFO" if status == "completed" else "ERROR", status=status,
                      provider_duration_ms=self._provider_ms(turn, "started_at", "completed_at"),
                      **usage_fields(payload.get("usage") or turn.get("usage")), **error_fields(turn.get("error") or {}))
        elif actual == "error" or actual.endswith((".failed", ".cancelled")):
            self.phase = "failed"
            log_event("session.error", level="ERROR", event_type=actual, **error_fields(payload.get("error") or {}))
        else:
            log_event("session.event", level="DEBUG", event_type=actual)

    @staticmethod
    def _provider_ms(value, start, end):
        left, right = value.get(start), value.get(end)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) and right >= left:
            return round((right - left) * 1000)
        return None

    def _item(self, item, done):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return
        item_id = item["id"]
        previous = self.items.get(item_id)
        if previous is None:
            previous = {"started": time.monotonic(), "item": {}, "done": False, "start_observed": not done}
            self.items[item_id] = previous
        merged = {**previous["item"], **item}
        previous["item"] = {key: merged[key] for key in ("id", "type", "name", "server_label", "status") if key in merged}
        kind = merged.get("type", "unknown")
        is_tool = isinstance(kind, str) and (kind.endswith("_call") or kind == "mcp_list_tools")
        if is_tool and not previous.get("counted"):
            previous["counted"] = True
            self.tool_count += 1
            self.phase = "tool_running"
            log_event("tool.started", item_id=item_id, tool_type=kind,
                      tool=merged.get("name"), server=merged.get("server_label"),
                      start_observed=previous["start_observed"])
        if not done or previous["done"]:
            if not done and not is_tool:
                self.phase = "agent_generating"
                log_event("output_item.started", level="DEBUG", item_id=item_id, item_type=kind)
            return
        previous["done"] = True
        elapsed = round((time.monotonic() - previous["started"]) * 1000)
        if is_tool:
            result = tool_result_fields(merged.get("output"))
            if isinstance(merged.get("tools"), list):
                result["tools_discovered"] = len(merged["tools"])
            failed = bool(merged.get("error") or result.get("tool_reported_error") or
                          merged.get("status") in {"failed", "cancelled", "incomplete"})
            self.tool_failures += int(failed)
            log_event("tool.arguments_ready" if kind == "function_call" else "tool.finished",
                      level="WARNING" if failed else "INFO", item_id=item_id,
                      tool_type=kind, tool=merged.get("name"), server=merged.get("server_label"),
                      status=merged.get("status", "unknown"), tool_reported_failure=failed,
                      observed_duration_ms=elapsed if previous["start_observed"] else None,
                      **result, **error_fields(merged.get("error") or {}))
            self.phase = "waiting_for_agent"
        else:
            log_event("output_item.finished", level="DEBUG", item_id=item_id, item_type=kind,
                      observed_duration_ms=elapsed)

    def finish(self, outcome: str, http_status=None) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        log_event("request.finished", level="INFO" if outcome == "completed" else "WARNING",
                  **self.context, outcome=outcome, http_status=http_status,
                  elapsed_ms=round((time.monotonic() - self.started) * 1000),
                  first_text_ms=self.first_text_ms, output_chars=sum(self.parts.values()),
                  events=sum(self.events.values()), tools=self.tool_count, tool_failures=self.tool_failures,
                  completed_turns=len(self.completed_turns), pending_items=sum(not item["done"] for item in self.items.values()))
