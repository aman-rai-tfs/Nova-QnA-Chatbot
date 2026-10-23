"""Read-only Atlassian MCP authentication/discovery check using curl, not OpenAI."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import re
import shutil
import subprocess
import warnings
from dataclasses import dataclass

ENDPOINT = "https://mcp.atlassian.com/v2/mcp"
MODERN_VERSION = "2026-07-28"
LEGACY_VERSION = "2025-11-25"
CLIENT = {"name": "nova-mcp-diagnostic", "version": "1.0.0"}
READ_ONLY_METHODS = {"initialize", "notifications/initialized", "tools/list"}


@dataclass(repr=False)
class Reply:
    status: int
    headers: dict[str, str]
    message: dict | None
    text: str
    curl_code: int = 0


def parse_reply(output: str, stderr: str = "", curl_code: int = 0) -> Reply:
    remaining = output.replace("\r\n", "\n")
    status, headers = 0, {}
    while remaining.startswith("HTTP/"):
        block, separator, remaining = remaining.partition("\n\n")
        if not separator:
            return Reply(0, {}, None, "Incomplete HTTP response headers.", curl_code)
        lines = block.splitlines()
        match = re.match(r"HTTP/\S+\s+(\d{3})", lines[0])
        status = int(match.group(1)) if match else 0
        headers = {key.lower().strip(): value.strip() for line in lines[1:]
                   if ":" in line for key, value in [line.split(":", 1)]}
        if not (100 <= status < 200):
            break
    message = None
    if "text/event-stream" in headers.get("content-type", ""):
        for block in remaining.split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
            try:
                candidate = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
                message = candidate
                break
    else:
        try:
            candidate = json.loads(remaining)
            message = candidate if isinstance(candidate, dict) else None
        except json.JSONDecodeError:
            pass
    if isinstance(message, dict) and isinstance(message.get("error"), dict):
        text = str(message["error"].get("message", "MCP error"))
    else:
        text = remaining.strip()[:1200] or stderr.strip() or "No response body."
    return Reply(status, headers, message, text, curl_code)


def config_value(value: str) -> str:
    if any(character in value for character in ("\r", "\n", "\0")):
        raise ValueError("A credential or header contains an invalid control character.")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def rpc(authorization: str, message: dict, *, version: str, session: str = "") -> Reply:
    method = message.get("method")
    if method not in READ_ONLY_METHODS:
        raise ValueError("This diagnostic only permits initialization and tools/list, never tool execution.")
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("curl is required and was not found in PATH.")
    headers = ["Authorization: " + authorization, "Content-Type: application/json",
               "Accept: application/json, text/event-stream", "MCP-Protocol-Version: " + version,
               "Mcp-Method: " + method]
    if session:
        headers.append("Mcp-Session-Id: " + session)
    # The token and request body travel via stdin. They are not command arguments or files.
    config = '\n'.join('header = "' + config_value(header) + '"' for header in headers)
    config += '\ndata-binary = "' + config_value(json.dumps(message, separators=(",", ":"))) + '"\n'
    result = subprocess.run(
        [curl, "--config", "-", "--include", "--suppress-connect-headers", "--no-buffer",
         "--silent", "--show-error", "--connect-timeout", "8", "--max-time", "20",
         "--proto", "=https", "--request", "POST", ENDPOINT],
        input=config, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=25, check=False,
    )
    return parse_reply(result.stdout, result.stderr, result.returncode)


def redact(value: str, secrets: list[str]) -> str:
    for secret in sorted((secret for secret in secrets if secret), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return value


def print_status(stage: str, reply: Reply, secrets: list[str]) -> None:
    print(f"  {stage}: HTTP {reply.status or 'none'}", flush=True)
    if reply.curl_code:
        print(f"    curl exit: {reply.curl_code}", flush=True)
    if not (200 <= reply.status < 300) or (reply.message and "error" in reply.message):
        print("    " + redact(reply.text, secrets)[:600], flush=True)


def tools_from(reply: Reply) -> list | None:
    if not 200 <= reply.status < 300 or not reply.message or "error" in reply.message:
        return None
    result = reply.message.get("result")
    return result.get("tools") if isinstance(result, dict) and isinstance(result.get("tools"), list) else None


def discover(authorization: str, secrets: list[str]) -> tuple[bool, int]:
    modern = rpc(authorization, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {
            "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
            "io.modelcontextprotocol/clientInfo": CLIENT,
            "io.modelcontextprotocol/clientCapabilities": {},
        }},
    }, version=MODERN_VERSION)
    print_status("tools/list (current protocol)", modern, secrets)
    tools = tools_from(modern)
    if tools is None:
        error = modern.message.get("error", {}) if modern.message else {}
        error = error if isinstance(error, dict) else {}
        # Preserve modern validation failures. Ordinary legacy HTTP 400 or an unknown
        # method can require the older initialize/initialized handshake instead.
        legacy_needed = (modern.status in (400, 404, 405) or error.get("code") == -32601)
        if not legacy_needed or error.get("code") in (-32020, -32021):
            return False, modern.status
        if error.get("code") == -32022:
            supported = error.get("data", {}).get("supported", []) if isinstance(error.get("data"), dict) else []
            if not any(isinstance(item, str) and item < MODERN_VERSION for item in supported):
                print("    Server rejected the requested protocol; this does not establish an authentication failure.")
                return False, modern.status
        print("  Checking the older initialize handshake...", flush=True)
        initial = rpc(authorization, {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {
            "protocolVersion": LEGACY_VERSION, "capabilities": {}, "clientInfo": CLIENT,
        }}, version=LEGACY_VERSION)
        print_status("initialize", initial, secrets)
        result = initial.message.get("result") if initial.message else None
        if not 200 <= initial.status < 300 or not isinstance(result, dict):
            return False, initial.status
        negotiated = result.get("protocolVersion")
        if negotiated not in ("2025-03-26", "2025-06-18", "2025-11-25"):
            print("    The server negotiated a protocol this diagnostic does not implement.")
            return False, initial.status
        session = initial.headers.get("mcp-session-id", "")
        if session:
            secrets.append(session)
        initialized = rpc(authorization, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                          version=negotiated, session=session)
        print_status("notifications/initialized", initialized, secrets)
        if not 200 <= initialized.status < 300 or (initialized.message and "error" in initialized.message):
            return False, initialized.status
        listed = rpc(authorization, {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                     version=negotiated, session=session)
        print_status("tools/list (negotiated protocol)", listed, secrets)
        tools = tools_from(listed)
        if tools is None:
            return False, listed.status
    names = [tool.get("name", "") for tool in tools if isinstance(tool, dict)]
    print(f"  PASS: tool discovery returned {len(names)} tools on the first page.", flush=True)
    print("  Tool names: " + redact(", ".join(str(name) for name in names[:30]), secrets), flush=True)
    if not names:
        print("  No tools were exposed; this can depend on scopes, permissions, and organization policy.")
    print("  Discovery does not prove access to a particular Jira project. No tools were executed.")
    return True, 200


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare-bearer", action="store_true", help="Test the same token as Basic and Bearer, without changing saved credentials.")
    args = parser.parse_args()
    print("Read-only test of " + ENDPOINT)
    print("Only initialization and tool discovery are permitted. No Jira tickets will be read or changed.")
    email = input("Atlassian account email: ").strip()
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        token = getpass.getpass("Existing Atlassian API token (hidden): ").strip()
    if not email or ":" in email or not token:
        raise ValueError("Enter the token owner's email address and an existing token.")
    config_value(email)
    config_value(token)
    basic = base64.b64encode((email + ":" + token).encode("utf-8")).decode("ascii")
    secrets = [email, token, basic]
    results = {}
    for name, authorization in [("Basic", "Basic " + basic)] + ([("Bearer", "Bearer " + token)] if args.compare_bearer else []):
        print("\n" + name + " authentication", flush=True)
        try:
            results[name] = discover(authorization, secrets)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            # Avoid printing subprocess command/input objects, which could contain secrets.
            print("  FAILED: " + ("Request timed out." if isinstance(exc, subprocess.TimeoutExpired) else redact(str(exc), secrets)[:600]))
            results[name] = (False, 0)
    print("\nRESULT", flush=True)
    if results.get("Basic", (False, 0))[0] and results.get("Bearer", (False, 0))[1] in (401, 403):
        print("Basic discovery succeeded and Bearer was rejected. The token's authentication format matters.")
    elif any(success for success, _ in results.values()):
        print("At least one direct discovery test succeeded from this computer. Compare that configuration with the OpenAI connection; the OpenAI-side cause is not yet established.")
    else:
        print("Direct discovery failed. Use the HTTP status and sanitized message above to identify the next check; this alone does not prove that the token is invalid.")
    print("You can share this diagnostic output; do not share the token or Authorization header.")
    return 0 if any(success for success, _ in results.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nDiagnostic cancelled.")
        raise SystemExit(130)
    except getpass.GetPassWarning:
        print("Run this diagnostic from an interactive PowerShell terminal so the token can be entered without echoing it.")
        raise SystemExit(1)
    except (OSError, RuntimeError, ValueError) as exc:
        print("Diagnostic could not start: " + type(exc).__name__ + ". Check the local prompts and curl installation.")
        raise SystemExit(1)
