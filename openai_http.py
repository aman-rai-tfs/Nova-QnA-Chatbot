"""Small curl.exe client for the OpenAI Agents API; no third-party packages."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import quote
from uuid import uuid4

BASE_URL = "https://api.openai.com/v1"
DEFAULT_PROJECT_ID = "proj_dXDjYYmzfErZmSza7mHcNP0y"
AGENTS_BETA_HEADER = "OpenAI-Beta: agents=v1"


class OpenAIRequestError(RuntimeError):
    """An OpenAI HTTP or curl error safe to show to the app caller."""


class _TurnCompleted(Exception):
    """Stop a live event stream immediately after the root turn completes."""


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
    args = [_curl(), "--config", "-", "--silent", "--show-error", "--fail-with-body"]
    if stream:
        args.extend(["--no-buffer", "-N", "-H", "Accept: text/event-stream"])
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
    if extra_headers:
        for header in extra_headers:
            args.extend(["-H", header])
    temp_name: str | None = None
    if payload is not None:
        args.extend(["-H", "Content-Type: application/json"])
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False) as body:
            json.dump(payload, body, ensure_ascii=False)
            temp_name = body.name
        args.extend(["--data-binary", f"@{temp_name}"])
    try:
        result = subprocess.run(args, input=config, capture_output=True, text=True, encoding="utf-8", errors="replace")
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
    if result.returncode:
        raise OpenAIRequestError(_error_text(result.stdout, result.stderr, result.returncode))
    if allow_empty_response and not result.stdout.strip():
        # The Agents session events endpoint acknowledges accepted events with HTTP 202,
        # which may have no response body. curl's exit status already verifies success.
        return {}
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenAIRequestError(f"OpenAI returned a non-JSON response: {result.stdout[:800]}") from exc
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
    body = stdout.strip()
    if body:
        try:
            parsed = json.loads(body)
            error = parsed.get("error", parsed)
            if isinstance(error, dict):
                message = error.get("message") or error.get("code") or json.dumps(error, ensure_ascii=False)
                return f"OpenAI request failed: {message}"
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
    return {"query": query, "matches": matches}


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


def _read_event_stream(stdout, on_event: Callable[[str, dict], None]) -> None:
    event_name = "message"
    data: list[str] = []
    for raw_line in stdout:
        line = raw_line.rstrip("\r\n")
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not data and line.startswith("{"):
            # With --fail-with-body, curl can return a JSON API error instead of SSE.
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                on_event("error", payload)
        elif line == "" and data:
            raw = "\n".join(data)
            if raw != "[DONE]":
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {"type": "error", "error": {"message": raw}}
                if isinstance(payload, dict):
                    on_event(event_name, payload)
            event_name = "message"
            data.clear()
    if data:
        raw = "\n".join(data)
        if raw != "[DONE]":
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"type": "error", "error": {"message": raw}}
            if isinstance(payload, dict):
                on_event(event_name, payload)


def stream_initial_session(
    agent_id: str,
    message: str,
    on_event: Callable[[str, dict], None],
    vector_store_id: str,
) -> str:
    config = _config_stdin()
    body = {
        "agent_id": agent_id,
        "environment": {"type": "none"},
        "input": message,
        "stream": True,
    }
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False) as body_file:
        json.dump(body, body_file, ensure_ascii=False)
        body_path = body_file.name
    args = _request_args("POST", BASE_URL + "/agents/sessions", stream=True)
    args.extend(["-H", "Content-Type: application/json", "--data-binary", f"@{body_path}"])
    proc = None
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
        return found_session
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        raise
    finally:
        Path(body_path).unlink(missing_ok=True)


def stream_followup_session(
    session_id: str,
    message: str,
    on_event: Callable[[str, dict], None],
    vector_store_id: str,
) -> None:
    config = _config_stdin()
    encoded_session_id = quote(session_id, safe="")
    endpoint = f"/agents/sessions/{encoded_session_id}/events?stream=true"
    args = _request_args("GET", BASE_URL + endpoint, stream=True)
    # Include response headers so the server can wait until the SSE subscription is open.
    args.insert(1, "--include")
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(config)
    proc.stdin.close()
    status_code: int | None = None
    header_mode = True
    try:
        for line in proc.stdout:
            if header_mode:
                stripped = line.rstrip("\r\n")
                if stripped.startswith("HTTP/"):
                    try:
                        status_code = int(stripped.split()[1])
                    except (IndexError, ValueError):
                        status_code = None
                elif stripped == "" and status_code is not None:
                    if not 200 <= status_code < 300:
                        body = proc.stdout.read()
                        raise OpenAIRequestError(_error_text(body, "", status_code))
                    header_mode = False
                    # The SSE subscription is now open; post input to begin this turn.
                    request_json(
                        "POST",
                        f"/agents/sessions/{encoded_session_id}/events",
                        {"events": [{"type": "agent.session.input.message", "input": [{"role": "user", "content": [{"type": "input_text", "text": message}]}]}]},
                        extra_headers=["Idempotency-Key: " + uuid4().hex],
                        allow_empty_response=True,
                    )
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
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)

