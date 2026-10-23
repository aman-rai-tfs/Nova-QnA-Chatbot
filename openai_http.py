"""Small curl.exe client for the OpenAI Agents API; no third-party packages."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote
from uuid import uuid4
from runtime_logging import bind_context, error_fields, log_event, register_log_secrets, tool_result_fields

BASE_URL = "https://api.openai.com/v1"
DEFAULT_PROJECT_ID = "proj_dXDjYYmzfErZmSza7mHcNP0y"
AGENTS_BETA_HEADER = "OpenAI-Beta: agents=v1"
ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v2/mcp"
_extra_runtime_secrets: list[str] = []


class OpenAIRequestError(RuntimeError):
    """An OpenAI HTTP or curl error safe to show to the app caller."""


class _TurnCompleted(Exception):
    """Stop a live event stream immediately after the root turn completes."""


def atlassian_basic_authorization() -> str | None:
    """Personal Atlassian tokens use Basic auth; never save this in an agent."""
    email = os.environ.get("ATLASSIAN_EMAIL", "").strip()
    token = os.environ.get("ATLASSIAN_API_TOKEN", "").strip()
    if not email and not token:
        return None
    if not email or not token:
        raise OpenAIRequestError("Configure both ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN, or run app.py --atlassian-mcp.")
    if ":" in email or any(char in email + token for char in ("\r", "\n", "\0")):
        raise OpenAIRequestError("The Atlassian email or token contains an invalid character.")
    return "Basic " + base64.b64encode((email + ":" + token).encode("utf-8")).decode("ascii")


def _redact_runtime_secrets(value: str) -> str:
    secrets = [os.environ.get("OPENAI_API_KEY", "").strip(), os.environ.get("ATLASSIAN_API_TOKEN", "").strip(), *_extra_runtime_secrets]
    try:
        authorization = atlassian_basic_authorization()
    except OpenAIRequestError:
        authorization = None
    if authorization:
        secrets.extend([authorization, authorization.removeprefix("Basic ")])
    for secret in sorted((secret for secret in secrets if secret), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return value


def register_runtime_secrets(*values: str) -> None:
    """Redact short-lived OAuth secrets from diagnostics without writing them."""
    _extra_runtime_secrets.extend(value for value in values if isinstance(value, str) and value)
    register_log_secrets(*values)


def _safe_event(value):
    if isinstance(value, str):
        return _redact_runtime_secrets(value)
    if isinstance(value, dict):
        return {key: _safe_event(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_event(item) for item in value]
    return value


def _curl() -> str:
    binary = shutil.which("curl.exe") or shutil.which("curl")
    if not binary:
        raise OpenAIRequestError("curl is required but was not found in PATH.")
    return binary


def _config_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


def _config_stdin() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise OpenAIRequestError("OPENAI_API_KEY is not configured in this runtime.")
    project = os.environ.get("OPENAI_PROJECT_ID", DEFAULT_PROJECT_ID).strip()
    if not project:
        raise OpenAIRequestError("OPENAI_PROJECT_ID must not be empty.")
    return (
        f'header = "Authorization: Bearer {_config_value(key)}"\n'
        f'header = "OpenAI-Project: {_config_value(project)}"\n'
        f'header = "{AGENTS_BETA_HEADER}"\n'
    )


def _request_args(method: str, url: str, *, stream: bool = False) -> list[str]:
    args = [_curl(), "--disable", "--config", "-", "--silent", "--show-error", "--fail-with-body", "--proto", "=https"]
    if stream:
        args.extend(["--no-buffer", "-N", "--suppress-connect-headers", "-H", "Accept: text/event-stream"])
    args.extend(["-X", method, url])
    return args


def request_json(
    method: str,
    endpoint: str,
    payload: dict | None = None,
    *,
    extra_headers: list[str] | None = None,
    allow_empty_response: bool = False,
) -> dict:
    config = _config_stdin()
    args = _request_args(method, BASE_URL + endpoint)
    status_marker = "\n__NOVA_HTTP_STATUS__:"
    args.extend(["--write-out", status_marker + "%{http_code}"])
    if extra_headers:
        for header in extra_headers:
            args.extend(["-H", header])
    if payload is not None:
        args.extend(["-H", "Content-Type: application/json"])
        config += 'data-binary = "' + _config_value(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) + '"\n'
    started = time.monotonic()
    log_level = "DEBUG" if method == "GET" else "INFO"
    path = endpoint.split("?", 1)[0]
    log_event("api.request_started", level=log_level, method=method, endpoint=path)
    try:
        result = subprocess.run(args, input=config, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        log_event("api.request_failed", level="ERROR", method=method, endpoint=path,
                  duration_ms=round((time.monotonic() - started) * 1000), error_type=type(exc).__name__)
        raise
    response = result.stdout
    body, marker, status = response.rpartition(status_marker)
    http_status = int(status.strip()) if marker and status.strip().isdigit() else None
    if http_status is not None:
        response = body
    details = {"method": method, "endpoint": path, "http_status": http_status,
               "duration_ms": round((time.monotonic() - started) * 1000),
               "response_bytes": len(response.encode("utf-8")), "curl_exit": result.returncode}
    if result.returncode:
        api_error = {}
        try:
            parsed_error = json.loads(response)
            if isinstance(parsed_error, dict) and isinstance(parsed_error.get("error"), dict):
                api_error = parsed_error["error"]
        except (ValueError, RecursionError):
            pass
        log_event("api.request_failed", level="ERROR", **details, **error_fields(api_error))
        raise OpenAIRequestError(_error_text(response, result.stderr, result.returncode))
    log_event("api.request_finished", level=log_level, **details)
    if allow_empty_response and not response.strip():
        # The Agents session events endpoint acknowledges accepted events with HTTP 202,
        # which may have no response body. curl's exit status already verifies success.
        return {}
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError as exc:
        log_event("api.invalid_response", level="ERROR", method=method, endpoint=path, http_status=http_status)
        raise OpenAIRequestError("OpenAI returned a non-JSON response: " + _redact_runtime_secrets(response[:800])) from exc
    if not isinstance(parsed, dict):
        raise OpenAIRequestError("OpenAI returned an unexpected response shape.")
    return parsed


def upload_file(path: Path) -> dict:
    config = _config_stdin()
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    args = _request_args("POST", BASE_URL + "/files")
    args.extend([
        "-F", "purpose=assistants",
        "-F", f"file=@{path};type={mime}",
    ])
    result = subprocess.run(args, input=config, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise OpenAIRequestError(_error_text(result.stdout, result.stderr, result.returncode))
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenAIRequestError(f"OpenAI returned a non-JSON upload response: {result.stdout[:800]}") from exc
    if not isinstance(parsed, dict):
        raise OpenAIRequestError("OpenAI returned an unexpected file upload response.")
    return parsed


def _error_text(stdout: str, stderr: str, code: int) -> str:
    stdout = _redact_runtime_secrets(stdout)
    stderr = _redact_runtime_secrets(stderr)
    body = stdout.strip()
    if body:
        try:
            parsed = json.loads(body)
            error = parsed.get("error", parsed)
            if isinstance(error, dict):
                message = error.get("message") or error.get("code") or json.dumps(error, ensure_ascii=False)
                details = [f"{name}={error[name]}" for name in ("code", "param") if error.get(name)]
                suffix = f" ({', '.join(details)})" if details else ""
                return f"OpenAI request failed: {message}{suffix}"
        except json.JSONDecodeError:
            return f"OpenAI request failed: {body[:1000]}"
    return f"curl exited with status {code}: {stderr.strip()[:1000]}"


def session_id_from_event(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("session_id")
    if isinstance(value, str) and value:
        return value
    for key in ("session", "agent_session"):
        nested = payload.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("id"), str):
            return nested["id"]
    if payload.get("object") == "agent.session" and isinstance(payload.get("id"), str):
        return payload["id"]
    return None


def search_nova_guide(vector_store_id: str, query: str) -> dict:
    """Search the guide vector store for text to return through the Agents function tool."""
    started = time.monotonic()
    log_event("guide.search_started", vector_store_id=vector_store_id, query_chars=len(query), max_results=8)
    result = request_json(
        "POST",
        f"/vector_stores/{quote(vector_store_id, safe='')}/search",
        {"query": query, "max_num_results": 8},
    )
    matches = []
    for item in result.get("data", []):
        passages = []
        for block in item.get("content", []):
            if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].strip():
                passages.append(block["text"].strip())
        text = "\n".join(passages)
        if text:
            matches.append({
                "file_id": item.get("file_id"),
                "score": item.get("score"),
                "text": text[:12000],
            })
    output = {"query": query, "matches": matches}
    log_event("guide.search_finished", duration_ms=round((time.monotonic() - started) * 1000),
              **tool_result_fields(output))
    return output


def handle_required_actions(payload: dict, vector_store_id: str) -> None:
    """Run pending guide-search function calls and submit their results to the session."""
    session_id = session_id_from_event(payload)
    session = payload.get("session")
    actions = session.get("required_actions", []) if isinstance(session, dict) else payload.get("required_actions", [])
    if not session_id or not isinstance(actions, list):
        raise OpenAIRequestError("The session requested tool work without a usable session ID or action list.")

    for action in actions:
        if not isinstance(action, dict) or action.get("type") != "function_call":
            raise OpenAIRequestError("The session requested an unsupported action.")
        turn_id, call_id = action.get("turn_id"), action.get("call_id")
        if not isinstance(turn_id, str) or not isinstance(call_id, str):
            raise OpenAIRequestError("The session returned a function call without its turn or call ID.")
        started = time.monotonic()
        log_event("function.executing", session_id=session_id, turn_id=turn_id,
                  call_id=call_id, tool=action.get("name"))
        outcome: dict
        if action.get("name") != "search_nova_guide":
            outcome = {"success": False, "error": f"Unsupported function: {action.get('name', '')}"}
        else:
            arguments = action.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            query = arguments.get("query") if isinstance(arguments, dict) else None
            if not isinstance(query, str) or not query.strip():
                outcome = {"success": False, "error": "The guide search query was missing or invalid."}
            else:
                try:
                    outcome = {"success": True, "output": json.dumps(search_nova_guide(vector_store_id, query.strip()), ensure_ascii=False)}
                except OpenAIRequestError as exc:
                    outcome = {"success": False, "error": str(exc)}
        request_json(
            "POST",
            f"/agents/sessions/{quote(session_id, safe='')}/events",
            {"events": [{
                "type": "agent.session.input.tool_result",
                "turn_id": turn_id,
                "call_id": call_id,
                **outcome,
            }]},
            extra_headers=["Idempotency-Key: " + uuid4().hex],
            allow_empty_response=True,
        )
        log_event("function.result_submitted", level="INFO" if outcome.get("success") else "WARNING",
                  session_id=session_id, turn_id=turn_id, call_id=call_id, tool=action.get("name"),
                  success=outcome.get("success", False), duration_ms=round((time.monotonic() - started) * 1000))


def _read_event_stream(stdout, on_event: Callable[[str, dict], None]) -> None:
    event_name = "message"
    data: list[str] = []
    error_body: list[str] = []
    for raw_line in stdout:
        line = raw_line.rstrip("\r\n")
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line.startswith(("id:", "retry:")):
            continue
        elif line == "" and data:
            raw = "\n".join(data)
            if raw != "[DONE]":
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {"type": "error", "error": {"message": raw}}
                if isinstance(payload, dict):
                    on_event(event_name, _safe_event(payload))
            event_name = "message"
            data.clear()
        elif line.strip() and not data:
            # HTTP failures can contain a pretty-printed JSON body, not SSE.
            # Preserve the complete body so the actual API error reaches the UI.
            error_body.append(raw_line)
    if error_body:
        raise OpenAIRequestError(_error_text("".join(error_body), "", 22))
    if data:
        raw = "\n".join(data)
        if raw != "[DONE]":
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"type": "error", "error": {"message": raw}}
            if isinstance(payload, dict):
                on_event(event_name, _safe_event(payload))


def _wait_for_hosted_environment(session: dict, on_event: Callable[[str, dict], None], *, timeout: float = 300) -> None:
    """Wait for provisioning before submitting the first model turn."""
    session_id = session["id"]
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    log_event("environment.wait_started", session_id=session_id, timeout_seconds=timeout)
    last_status = None
    while True:
        if session.get("status") == "failed":
            raise OpenAIRequestError("The hosted session failed before input: " + _redact_runtime_secrets(str(session.get("error") or "no detail")))
        environment = session.get("environment") or {}
        environment_id = environment.get("id") if isinstance(environment, dict) else None
        if environment_id:
            info = request_json("GET", "/agents/environments/" + quote(environment_id, safe=""))
            status = info.get("status", "pending")
        else:
            status = "pending"
        if status != last_status:
            on_event("app.environment", {"type": "app.environment", "session_id": session_id, "status": status})
            last_status = status
        if status == "connected":
            log_event("environment.ready", session_id=session_id, environment_id=environment_id,
                      duration_ms=round((time.monotonic() - started) * 1000))
            return
        if status in ("failed", "expired"):
            raise OpenAIRequestError("The hosted environment could not start (" + status + "). Session: " + session_id)
        if time.monotonic() >= deadline:
            raise OpenAIRequestError("Timed out waiting for the hosted environment. No user input was submitted. Session: " + session_id)
        time.sleep(2)
        session = request_json("GET", "/agents/sessions/" + quote(session_id, safe=""))


def stream_initial_session(
    agent_id: str,
    message: str,
    on_event: Callable[[str, dict], None],
    vector_store_id: str,
    *,
    vault_ids: list[str] | None = None,
    environment: dict | None = None,
) -> str:
    log_event("session.creating", agent_id=agent_id, environment_type=(environment or {}).get("type", "none"),
              environment_template_id=(environment or {}).get("environment_template_id"), vault_count=len(vault_ids or []))
    if environment and environment.get("type") == "openai_hosted":
        # Match the Platform UI: create an idle session, wait for provisioning,
        # subscribe to events, then submit the initial user message separately.
        body = {"agent_id": agent_id, "environment": environment}
        if vault_ids:
            body["vault_ids"] = vault_ids
        session = request_json("POST", "/agents/sessions", body, extra_headers=["Idempotency-Key: " + uuid4().hex])
        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id.startswith("sess_"):
            raise OpenAIRequestError("OpenAI did not return a hosted session ID.")
        bind_context(session_id=session_id)
        on_event("app.session", {"type": "app.session", "session_id": session_id})
        _wait_for_hosted_environment(session, on_event)
        stream_followup_session(session_id, message, on_event, vector_store_id)
        return session_id
    config = _config_stdin()
    body = {
        "agent_id": agent_id,
        "environment": environment if environment is not None else {"type": "none"},
        "input": message,
        "stream": True,
    }
    if vault_ids:
        body["vault_ids"] = vault_ids
    # MCP credentials are supplied by attached Vault IDs, never inline agent fields.
    config += 'data-binary = "' + _config_value(json.dumps(body, ensure_ascii=False, separators=(",", ":"))) + '"\n'
    args = _request_args("POST", BASE_URL + "/agents/sessions", stream=True)
    args.extend(["-H", "Content-Type: application/json"])
    proc = None
    started = time.monotonic()
    log_event("stream.connecting", method="POST", endpoint="/agents/sessions")
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(config)
        proc.stdin.close()
        found_session = ""
        completed = False

        def capture(event_type: str, payload: dict) -> None:
            nonlocal found_session, completed
            found_session = found_session or session_id_from_event(payload) or ""
            on_event(event_type, payload)
            actual_type = payload.get("type", event_type)
            if actual_type in {"agent.session.turn.failed", "agent.session.turn.cancelled", "agent.session.failed", "agent.session.environment.failed", "error"}:
                detail = payload.get("error", {}).get("message", "") if isinstance(payload.get("error"), dict) else ""
                raise OpenAIRequestError(f"Agent session failed ({actual_type}): {detail}".rstrip())
            if actual_type == "agent.session.requires_action":
                handle_required_actions(payload, vector_store_id)
            if actual_type == "agent.session.turn.completed":
                completed = True
                raise _TurnCompleted()

        try:
            _read_event_stream(proc.stdout, capture)
        except _TurnCompleted:
            pass
        if proc.poll() is None and completed:
            proc.terminate()
        stderr = proc.stderr.read() if proc.stderr else ""
        code = proc.wait()
        if code and not completed:
            raise OpenAIRequestError(_error_text("", stderr, code))
        if not found_session:
            raise OpenAIRequestError("The session stream ended without a session ID. Check the OpenAI response and try again.")
        if not completed:
            raise OpenAIRequestError("The session stream closed before the agent turn completed. Retrieve the session before retrying.")
        log_event("stream.finished", session_id=found_session, turn_completed=True,
                  observed_duration_ms=round((time.monotonic() - started) * 1000))
        return found_session
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        raise


def stream_followup_session(
    session_id: str,
    message: str,
    on_event: Callable[[str, dict], None],
    vector_store_id: str,
) -> None:
    bind_context(session_id=session_id)
    config = _config_stdin()
    encoded_session_id = quote(session_id, safe="")
    endpoint = f"/agents/sessions/{encoded_session_id}/events?stream=true"
    args = _request_args("GET", BASE_URL + endpoint, stream=True)
    # Include response headers so the server can wait until the SSE subscription is open.
    args.insert(2, "--include")  # Keep --disable first so curl ignores ~/.curlrc.
    started = time.monotonic()
    log_event("stream.connecting", method="GET", session_id=session_id)
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(config)
    proc.stdin.close()
    status_code: int | None = None
    openai_request_id = None
    header_mode = True
    try:
        for line in proc.stdout:
            if header_mode:
                stripped = line.rstrip("\r\n")
                if stripped.startswith("HTTP/"):
                    openai_request_id = None
                    try:
                        status_code = int(stripped.split()[1])
                    except (IndexError, ValueError):
                        status_code = None
                elif stripped.lower().startswith("x-request-id:"):
                    openai_request_id = stripped.split(":", 1)[1].strip()
                elif stripped == "" and status_code is not None:
                    if 100 <= status_code < 200:
                        status_code = None
                        continue
                    if not 200 <= status_code < 300:
                        log_event("stream.rejected", level="ERROR", http_status=status_code,
                                  openai_request_id=openai_request_id)
                        body = proc.stdout.read()
                        raise OpenAIRequestError(_error_text(body, "", status_code))
                    header_mode = False
                    log_event("stream.connected", http_status=status_code, openai_request_id=openai_request_id,
                              connect_ms=round((time.monotonic() - started) * 1000))
                    # The SSE subscription is now open; post input to begin this turn.
                    log_event("input.submitting", message_chars=len(message))
                    request_json(
                        "POST",
                        f"/agents/sessions/{encoded_session_id}/events",
                        {"events": [{"type": "agent.session.input.message", "input": [{"role": "user", "content": [{"type": "input_text", "text": message}]}]}]},
                        extra_headers=["Idempotency-Key: " + uuid4().hex],
                        allow_empty_response=True,
                    )
                    log_event("input.accepted")
                    break
                continue
        if header_mode:
            stderr = proc.stderr.read() if proc.stderr else ""
            code = proc.wait()
            raise OpenAIRequestError(_error_text("", stderr, code or status_code or 1))

        completed = False

        def capture(event_type: str, payload: dict) -> None:
            nonlocal completed
            on_event(event_type, payload)
            actual_type = payload.get("type", event_type)
            if actual_type in {"agent.session.turn.failed", "agent.session.turn.cancelled", "agent.session.failed", "agent.session.environment.failed", "error"}:
                detail = payload.get("error", {}).get("message", "") if isinstance(payload.get("error"), dict) else ""
                raise OpenAIRequestError(f"Agent session failed ({actual_type}): {detail}".rstrip())
            if actual_type == "agent.session.requires_action":
                handle_required_actions(payload, vector_store_id)
            if actual_type == "agent.session.turn.completed":
                completed = True
                raise _TurnCompleted()

        try:
            # The header has already been consumed; stdout retains any buffered event bytes.
            _read_event_stream(proc.stdout, capture)
        except _TurnCompleted:
            pass
        if proc.poll() is None and completed:
            proc.terminate()
        stderr = proc.stderr.read() if proc.stderr else ""
        code = proc.wait()
        if code and not completed:
            raise OpenAIRequestError(_error_text("", stderr, code))
        if not completed:
            raise OpenAIRequestError("The session event stream closed before the agent turn completed.")
        log_event("stream.finished", turn_completed=True,
                  observed_duration_ms=round((time.monotonic() - started) * 1000))
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)

