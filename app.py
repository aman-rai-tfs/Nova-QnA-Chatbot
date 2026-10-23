"""NOVA-ready, dependency-free web chat for the saved Nova guide agent."""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from openai_http import DEFAULT_PROJECT_ID, OpenAIRequestError, session_id_from_event, stream_followup_session, stream_initial_session
from runtime_logging import RequestMetrics, bind_context, configure_logging, error_fields, log_context, log_event

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "nova_state.json"
MAX_REQUEST_BYTES = 16 * 1024
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_session_locks: dict[str, threading.Lock] = {}
_session_locks_guard = threading.Lock()


def agent_id() -> str:
    configured = os.environ.get("OPENAI_AGENT_ID", "").strip()
    if configured:
        return configured
    if STATE_PATH.is_file():
        try:
            value = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("agent_id", "")
            return value.strip() if isinstance(value, str) else ""
        except (OSError, json.JSONDecodeError):
            return ""
    return ""


def guide_vector_store_id() -> str:
    configured = os.environ.get("OPENAI_VECTOR_STORE_ID", "").strip()
    if configured:
        return configured
    if STATE_PATH.is_file():
        try:
            value = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("vector_store_id", "")
            return value.strip() if isinstance(value, str) else ""
        except (OSError, json.JSONDecodeError):
            return ""
    return ""


def agent_vault_ids() -> list[str]:
    """Attach only explicitly configured MCP credential vaults to new sessions."""
    configured = os.environ.get("OPENAI_VAULT_IDS")
    if configured is not None:
        value = configured.strip()
        try:
            ids = json.loads(value) if value.startswith("[") else [part.strip() for part in value.split(",") if part.strip()]
        except json.JSONDecodeError as exc:
            raise OpenAIRequestError("OPENAI_VAULT_IDS must be comma-separated vault IDs or a JSON array of IDs.") from exc
    else:
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.is_file() else {}
        except (OSError, json.JSONDecodeError) as exc:
            raise OpenAIRequestError("Cannot read MCP vault configuration from nova_state.json.") from exc
        if not isinstance(state, dict):
            raise OpenAIRequestError("nova_state.json must contain a JSON object.")
        ids = state.get("vault_ids", [])
    if not isinstance(ids, list) or any(
        not isinstance(item, str) or not re.fullmatch(r"vault_[A-Za-z0-9_-]+", item) for item in ids
    ):
        raise OpenAIRequestError("MCP vault configuration must contain vault_ IDs, not API keys or credential_ IDs.")
    return list(dict.fromkeys(ids))


def session_environment() -> dict:
    configured = os.environ.get("OPENAI_ENVIRONMENT_TEMPLATE_ID")
    if configured is None:
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.is_file() else {}
        except (OSError, json.JSONDecodeError) as exc:
            raise OpenAIRequestError("Cannot read the environment template configuration.") from exc
        configured = state.get("environment_template_id", "")
    if not isinstance(configured, str):
        raise OpenAIRequestError("The environment template ID must be a string.")
    configured = configured.strip()
    if not configured:
        return {"type": "none"}
    if not re.fullmatch(r"envtmpl_[A-Za-z0-9_-]+", configured):
        raise OpenAIRequestError("OPENAI_ENVIRONMENT_TEMPLATE_ID must be an envtmpl_ ID.")
    return {"type": "openai_hosted", "environment_template_id": configured}


def session_lock(session_id: str) -> threading.Lock:
    with _session_locks_guard:
        return _session_locks.setdefault(session_id, threading.Lock())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        # The request line can contain query parameters; log only the URL path.
        log_event("http.access", method=self.command, path=urlsplit(self.path).path,
                  status=args[1] if len(args) > 1 else None)

    def do_GET(self) -> None:
        if self.path == "/api/health":
            try:
                configured_vaults = agent_vault_ids()
                environment = session_environment()
            except OpenAIRequestError as exc:
                self._json_response(503, {"ok": False, "error": str(exc)})
                return
            self._json_response(200, {
                "ok": True,
                "agent_configured": bool(agent_id()),
                "agent_id": agent_id(),
                "guide_search_configured": bool(guide_vector_store_id()),
                "mcp_vault_count": len(configured_vaults),
                "environment_type": environment["type"],
                "project_id": os.environ.get("OPENAI_PROJECT_ID", DEFAULT_PROJECT_ID),
            })
            return
        if self.path not in ("/", "/index.html"):
            self._json_response(404, {"error": "Not found"})
            return
        try:
            content = (ROOT / "index.html").read_bytes()
        except OSError:
            self._json_response(500, {"error": "Chat UI is missing from the application package."})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        with log_context(request_id=uuid4().hex[:12]):
            self._request_metrics = RequestMetrics()
            self._request_outcome = "rejected"
            self._http_status = None
            log_event("request.received", method=self.command, path=urlsplit(self.path).path)
            try:
                self._handle_post()
            except (BrokenPipeError, ConnectionResetError):
                self._request_outcome = "client_disconnected"
                log_event("client.disconnected", level="WARNING")
            except Exception as exc:
                self._request_outcome = "failed"
                log_event("request.unexpected_error", level="ERROR", **error_fields(exc))
                # Avoid BaseHTTPRequestHandler printing an unsanitized traceback.
                if self._http_status is None:
                    self._json_response(500, {"error": "An unexpected server error occurred. Check the request ID in the server logs."})
                elif self._http_status == 200:
                    try:
                        event = {"event_type": "error", "event": {"type": "error", "error": {
                            "message": "An unexpected server error occurred. Request ID: " + self._request_metrics.context["request_id"]}}}
                        self.wfile.write(("event: agent\ndata: " + json.dumps(event) + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                    except OSError:
                        pass
            finally:
                self._request_metrics.finish(self._request_outcome, self._http_status)
                del self._request_metrics

    def _handle_post(self) -> None:
        if self.path != "/api/chat":
            self._json_response(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json_response(400, {"error": "Invalid Content-Length."})
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._json_response(413, {"error": "Send a message smaller than 16 KB."})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json_response(400, {"error": "Request body must be valid JSON."})
            return
        if not isinstance(body, dict):
            self._json_response(400, {"error": "Request body must be a JSON object."})
            return
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            self._json_response(400, {"error": "Enter a message first."})
            return
        if len(message) > 8000:
            self._json_response(413, {"error": "Keep each message under 8,000 characters."})
            return
        current_agent = agent_id()
        if not current_agent:
            self._json_response(503, {"error": "No saved agent is configured. Run provision.py first or set OPENAI_AGENT_ID."})
            return
        current_vector_store = guide_vector_store_id()
        if not current_vector_store:
            self._json_response(503, {"error": "No guide vector store is configured. Run provision.py first or set OPENAI_VECTOR_STORE_ID."})
            return
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            self._json_response(503, {"error": "OPENAI_API_KEY is not configured in the application runtime."})
            return
        try:
            current_vaults = agent_vault_ids()
            current_environment = session_environment()
        except OpenAIRequestError as exc:
            self._json_response(503, {"error": str(exc)})
            return

        session_id = body.get("session_id")
        if session_id is not None and (not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id)):
            self._json_response(400, {"error": "Invalid session ID."})
            return

        bind_context(session_id=session_id)
        self._request_metrics.context = {**self._request_metrics.context, **({"session_id": session_id} if session_id else {})}
        self._request_metrics.phase = "continuing_session" if session_id else "creating_session"
        log_event("request.validated", message_chars=len(message.strip()), request_bytes=length,
                  session_mode="existing" if session_id else "new", agent_id=current_agent,
                  vector_store_id=current_vector_store, vault_count=len(current_vaults),
                  environment_type=current_environment["type"],
                  environment_template_id=current_environment.get("environment_template_id"))
        self._request_metrics.start_progress()

        self._http_status = 200
        self.send_response(200)
        self.send_header("X-Request-ID", self._request_metrics.context["request_id"])
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def relay(event_type: str, payload: dict) -> None:
            self._request_metrics.observe(event_type, payload)
            envelope = {"event_type": event_type, "event": payload}
            resolved_session = session_id_from_event(payload) or session_id
            if resolved_session:
                envelope["session_id"] = resolved_session
            self.wfile.write(("event: agent\ndata: " + json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        try:
            if session_id:
                lock = session_lock(session_id)
                if not lock.acquire(blocking=False):
                    self._request_outcome = "session_busy"
                    log_event("session.busy", level="WARNING")
                    relay("error", {"type": "error", "error": {"message": "A response is already in progress for this session."}})
                    return
                try:
                    stream_followup_session(session_id, message.strip(), relay, current_vector_store)
                finally:
                    lock.release()
            else:
                new_session = stream_initial_session(
                    current_agent, message.strip(), relay, current_vector_store, vault_ids=current_vaults, environment=current_environment
                )
                # Emit an explicit session association in case the upstream lifecycle event omitted it.
                relay("app.session", {"type": "app.session", "session_id": new_session})
            self.wfile.write(b"event: done\ndata: {}\n\n")
            self.wfile.flush()
            self._request_outcome = "completed"
        except (OpenAIRequestError, OSError, BrokenPipeError) as exc:
            self._request_outcome = "client_disconnected" if isinstance(exc, (BrokenPipeError, ConnectionResetError)) else "failed"
            log_event("request.failed", level="ERROR", **error_fields(exc))
            try:
                error = {"type": "error", "error": {"message": str(exc)}}
                relay("error", error)
            except (OSError, BrokenPipeError):
                pass

    def _json_response(self, status: int, payload: dict) -> None:
        self._http_status = status
        if status >= 400:
            log_event("request.rejected", level="WARNING", status=status,
                      reason=payload.get("error") if isinstance(payload.get("error"), str) else "request_failed")
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        metrics = getattr(self, "_request_metrics", None)
        if metrics:
            self.send_header("X-Request-ID", metrics.context["request_id"])
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atlassian-mcp", action="store_true", help="Retired inline-Basic mode; use setup_atlassian_oauth.py, then run app.py normally.")
    args = parser.parse_args(argv)
    if args.atlassian_mcp:
        parser.error("The API rejected inline MCP Basic authorization. Run python .\\setup_atlassian_oauth.py to configure OAuth in OpenAI Vault, then start python .\\app.py normally.")
    host = os.environ.get("HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("PORT", "8080"))
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be between 1 and 65535.")
        server = ThreadingHTTPServer((host, port), Handler)
    except (ValueError, OSError) as exc:
        log_event("server.start_failed", level="ERROR", host=host, **error_fields(exc))
        raise SystemExit(1) from None
    log_event("server.started", listen_url=f"http://{host}:{port}", browser_url=f"http://localhost:{port}",
              project_id=os.environ.get("OPENAI_PROJECT_ID", DEFAULT_PROJECT_ID),
              agent_id=agent_id(), api_key_configured=bool(os.environ.get("OPENAI_API_KEY", "").strip()),
              log_level=os.environ.get("NOVA_LOG_LEVEL", "INFO"))
    try:
        configured_environment = session_environment()
        log_event("server.configuration", environment_type=configured_environment["type"],
                  environment_template_id=configured_environment.get("environment_template_id"),
                  vault_count=len(agent_vault_ids()), vector_store_id=guide_vector_store_id())
    except OpenAIRequestError as exc:
        log_event("server.configuration_error", level="ERROR", **error_fields(exc))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_event("server.stopping", reason="keyboard_interrupt")
    finally:
        server.server_close()
        log_event("server.stopped")


if __name__ == "__main__":
    main()

