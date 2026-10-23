"""Connect the saved Nova agent to Atlassian using browser OAuth and OpenAI Vault."""

from __future__ import annotations

import argparse
import base64
import copy
import getpass
import hashlib
import json
import os
import secrets
import subprocess
import time
import warnings
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import app
import openai_http as api

RESOURCE = api.ATLASSIAN_MCP_URL
DISCOVERY = "https://mcp.atlassian.com/.well-known/oauth-protected-resource/v2/mcp"
SCOPES = ["read:me", "read:account", "offline_access", "read:jira:agent-interface", "search:jira:agent-interface"]
READ_TOOLS = ["getAccessibleAtlassianResources", "atlassianUserInfo", "getJiraIssue", "searchJiraIssuesUsingJql", "discover", "executeRead"]


def trusted_url(url: str, host: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != host or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("OAuth discovery returned an unexpected endpoint; setup stopped before sending credentials.")
    return url


def https_json(method: str, url: str, *, payload: dict | None = None, form: dict | None = None, authorization: str = "") -> dict:
    # Only Atlassian's published MCP and authorization hosts receive OAuth requests.
    host = urlsplit(url).hostname
    if host not in ("mcp.atlassian.com", "auth.atlassian.com"):
        raise ValueError("Unexpected OAuth destination.")
    trusted_url(url, host)
    config = 'header = "Accept: application/json"\n'
    if authorization:
        config += 'header = "Authorization: ' + api._config_value(authorization) + '"\n'
    if payload is not None or form is not None:
        content_type = "application/json" if payload is not None else "application/x-www-form-urlencoded"
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else urlencode(form)
        config += 'header = "Content-Type: ' + content_type + '"\n'
        config += 'data-binary = "' + api._config_value(body) + '"\n'
    result = subprocess.run([api._curl(), "--disable", "--config", "-", "--silent", "--show-error", "--fail-with-body", "--proto", "=https", "--connect-timeout", "10", "--max-time", "30", "-X", method, url], input=config, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=35, check=False)
    if result.returncode:
        # Do not print token responses, callback URLs, or request bodies on failure.
        try:
            error = json.loads(result.stdout).get("error", "request_failed")
            error = error if isinstance(error, str) else "request_failed"
        except (json.JSONDecodeError, AttributeError):
            error = "request_failed"
        raise api.OpenAIRequestError("Atlassian OAuth request failed: " + api._redact_runtime_secrets(error)[:150] + " (curl status " + str(result.returncode) + ").")
    response = json.loads(result.stdout)
    if not isinstance(response, dict):
        raise ValueError("Atlassian returned an unexpected OAuth response.")
    return response


def discover_oauth() -> tuple[dict, dict]:
    resource = https_json("GET", DISCOVERY)
    if resource.get("resource") != RESOURCE:
        raise ValueError("OAuth discovery did not match the configured MCP resource.")
    issuers = resource.get("authorization_servers", [])
    if not isinstance(issuers, list) or not issuers:
        raise ValueError("Atlassian did not advertise an OAuth authorization server.")
    issuer = trusted_url(issuers[0], "auth.atlassian.com")
    parsed = urlsplit(issuer)
    metadata_url = urlunsplit((parsed.scheme, parsed.netloc, "/.well-known/oauth-authorization-server" + parsed.path.rstrip("/"), "", ""))
    metadata = https_json("GET", metadata_url)
    if metadata.get("issuer") != issuer:
        raise ValueError("OAuth issuer metadata did not match its advertised issuer.")
    for field in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        trusted_url(metadata.get(field, ""), "auth.atlassian.com")
    if "S256" not in metadata.get("code_challenge_methods_supported", []):
        raise ValueError("The OAuth server did not advertise PKCE S256.")
    if "none" not in metadata.get("token_endpoint_auth_methods_supported", []):
        raise ValueError("The OAuth server did not advertise public-client authentication.")
    return resource, metadata


def callback_handler(state: str, result: dict):
    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # OAuth codes in request URLs must never enter terminal logs.

        def do_GET(self):
            parsed = urlsplit(self.path)
            fields = parse_qs(parsed.query)
            received_state = fields.get("state", [""])[0]
            valid = parsed.path == "/callback" and secrets.compare_digest(received_state, state)
            if valid and not result:
                if fields.get("error"):
                    result["error"] = "Atlassian sign-in was declined or blocked. Review the browser message."
                elif fields.get("code"):
                    result["code"] = fields["code"][0]
            body = b"Sign-in received. Return to the PowerShell window." if valid else b"Invalid sign-in callback. Return to the original sign-in tab."
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Callback


def authorize(metadata: dict, scopes: list[str], *, open_browser: bool = True) -> tuple[dict, dict]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    result = {}
    api.register_runtime_secrets(state, verifier)
    with HTTPServer(("127.0.0.1", 0), callback_handler(state, result)) as server:
        server.timeout = 1
        redirect_uri = "http://localhost:" + str(server.server_port) + "/callback"
        client = https_json("POST", metadata["registration_endpoint"], payload={
            "client_name": "Nova Guide Assistant OAuth setup", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none", "scope": " ".join(scopes),
        })
        if not client.get("client_id") or client.get("token_endpoint_auth_method", "none") != "none":
            raise ValueError("Atlassian did not register the requested public OAuth client.")
        if client.get("client_secret"):
            api.register_runtime_secrets(client["client_secret"])
        params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect_uri,
                  "scope": " ".join(scopes), "state": state, "code_challenge": challenge,
                  "code_challenge_method": "S256", "resource": RESOURCE}
        login_url = metadata["authorization_endpoint"] + "?" + urlencode(params)
        print("Sign in to Atlassian and review the requested access in your browser.", flush=True)
        print("If the browser does not open, copy this URL into your browser:\n" + login_url, flush=True)
        if open_browser:
            webbrowser.open(login_url, new=2)
        deadline = time.monotonic() + 600
        while not result and time.monotonic() < deadline:
            server.handle_request()
        if result.get("error"):
            raise api.OpenAIRequestError(result["error"])
        if not result.get("code"):
            raise api.OpenAIRequestError("OAuth sign-in timed out. Run setup again and complete browser consent.")
        api.register_runtime_secrets(result["code"])
        tokens = https_json("POST", metadata["token_endpoint"], form={"grant_type": "authorization_code",
            "client_id": client["client_id"], "code": result["code"], "redirect_uri": redirect_uri,
            "code_verifier": verifier, "resource": RESOURCE})
    api.register_runtime_secrets(tokens.get("access_token", ""), tokens.get("refresh_token", ""))
    if str(tokens.get("token_type", "")).lower() != "bearer" or not tokens.get("access_token"):
        raise ValueError("Atlassian did not return an OAuth Bearer access token.")
    return client, tokens


def credential_auth(metadata: dict, client: dict, tokens: dict) -> dict:
    auth = {"type": "mcp_oauth", "mcp_server_url": RESOURCE, "access_token": tokens["access_token"]}
    lifetime = tokens.get("expires_in")
    if isinstance(lifetime, (int, float)) and lifetime > 0:
        auth["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=lifetime)).isoformat().replace("+00:00", "Z")
    if tokens.get("refresh_token"):
        auth["refresh"] = {"client_id": client["client_id"], "refresh_token": tokens["refresh_token"],
                           "token_endpoint": metadata["token_endpoint"], "token_endpoint_auth": {"type": "none"}, "resource": RESOURCE}
        if tokens.get("scope"):
            auth["refresh"]["scope"] = tokens["scope"]
    return auth


def read_state() -> dict:
    state = json.loads(app.STATE_PATH.read_text(encoding="utf-8")) if app.STATE_PATH.is_file() else {}
    if not isinstance(state, dict):
        raise ValueError("nova_state.json must contain an object.")
    return state


def write_state(state: dict) -> None:
    # Only resource IDs and a session ID are checkpointed; OAuth secrets stay in Vault.
    temporary = app.STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(app.STATE_PATH)


def updated_tools(saved: dict, credential_id: str) -> list:
    tools = copy.deepcopy(saved.get("tools"))
    if not isinstance(tools, list):
        raise ValueError("The saved agent did not return a tools list.")
    matched = 0
    for tool in tools:
        transport = tool.get("transport", {})
        if tool.get("type") != "mcp" or transport.get("type") != "http" or transport.get("server_url") != RESOURCE:
            continue
        matched += 1
        transport.pop("authorization", None)
        transport["headers"] = {key: value for key, value in (transport.get("headers") or {}).items() if key.lower() != "authorization"}
        tool["credential_id"] = credential_id
        tool["connection_origin"] = "service"
        # Keep the initial OAuth rollout read-only at the agent tool allowlist too.
        previous = tool.get("allowed_tools")
        tool["allowed_tools"] = [name for name in READ_TOOLS if previous is None or name in previous]
        tool["required"] = True
    if matched != 1:
        raise ValueError("The selected agent must contain exactly one MCP connection to " + RESOURCE + ".")
    return tools


def output_has_error(value) -> bool:
    if isinstance(value, str):
        try:
            return output_has_error(json.loads(value))
        except json.JSONDecodeError:
            return False
    if isinstance(value, dict):
        if value.get("isError") or value.get("is_error") or value.get("error"):
            return True
        return any(output_has_error(item) for item in value.values())
    if isinstance(value, list):
        return any(output_has_error(item) for item in value)
    return False


def test_session(agent_id: str, vault_id: str, state: dict) -> bool:
    successful_calls = []
    found_session = ""
    def event(kind: str, payload: dict):
        nonlocal found_session
        found_session = found_session or api.session_id_from_event(payload) or ""
        if found_session and state["atlassian_oauth"].get("test_session_id") != found_session:
            state["atlassian_oauth"]["test_session_id"] = found_session
            write_state(state)
        actual = payload.get("type", kind)
        if actual == "app.session":
            print("Session created. Waiting for the hosted environment before sending the test question...", flush=True)
        elif actual == "app.environment":
            print("Hosted environment: " + payload.get("status", "unknown"), flush=True)
        elif actual == "agent.session.turn.output_text.delta":
            print(payload.get("delta", ""), end="", flush=True)
        elif actual == "agent.session.turn.item.done":
            item = payload.get("item", {})
            if item.get("type") == "mcp_call":
                ok = item.get("status") == "completed" and not item.get("error") and item.get("output") is not None and not output_has_error(item.get("output"))
                print("\nMCP tool " + item.get("name", "unknown") + ": " + ("completed successfully" if ok else "not confirmed successful"), flush=True)
                if ok and item.get("name") == "getAccessibleAtlassianResources":
                    successful_calls.append(item["name"])
    try:
        api.stream_initial_session(agent_id,
            "Use Atlassian MCP to call getAccessibleAtlassianResources. List the returned Jira sites and cloud IDs. Do not create or modify anything. Do not search the Nova guide for this Jira connection check.",
            event, app.guide_vector_store_id(), vault_ids=[vault_id], environment=app.session_environment())
    finally:
        print()
        if found_session:
            project = os.environ.get("OPENAI_PROJECT_ID", api.DEFAULT_PROJECT_ID)
            print("Platform session: https://platform.openai.com/agents/" + agent_id + "/sessions/" + found_session + "?project_id=" + project, flush=True)
    if successful_calls:
        print("PASS: the OpenAI-hosted agent completed the Atlassian MCP resource lookup. Ticket creation was not tested.")
        return True
    print("OAuth was configured, but a successful MCP resource lookup was not confirmed. Inspect the Platform session trace.")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", default=os.environ.get("OPENAI_AGENT_ID", ""), help="Existing agent containing the Atlassian MCP entry.")
    parser.add_argument("--resume", action="store_true", help="Reuse the checkpointed Vault credential and retry binding/testing without a new sign-in.")
    parser.add_argument("--no-browser", action="store_true", help="Print the sign-in URL instead of opening the default browser.")
    parser.add_argument("--run-app", action="store_true", help="Start the chat app in this process after the live connection test succeeds.")
    parser.add_argument("--environment-template-id", help="Existing OpenAI-hosted environment template to use for the test and local chat sessions.")
    args = parser.parse_args()
    state = read_state()
    checkpoint = state.get("atlassian_oauth", {})
    if not isinstance(checkpoint, dict):
        raise ValueError("The OAuth checkpoint in nova_state.json must be an object.")
    agent_id = args.agent_id or checkpoint.get("agent_id", "")
    if not agent_id.startswith("agent_"):
        parser.error("Supply --agent-id with the existing manual agent ID.")
    if args.environment_template_id:
        os.environ["OPENAI_ENVIRONMENT_TEMPLATE_ID"] = args.environment_template_id
        app.session_environment()  # Validate before changing the local checkpoint.
        state["environment_template_id"] = args.environment_template_id
        write_state(state)
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            os.environ["OPENAI_API_KEY"] = getpass.getpass("OpenAI project API key (hidden): ").strip()
    saved = api.request_json("GET", "/agents/" + quote(agent_id, safe=""))
    updated_tools(saved, "credential_validation_only")  # Validate the target before asking for OAuth consent.
    if args.resume:
        if checkpoint.get("agent_id") != agent_id or not checkpoint.get("vault_id") or not checkpoint.get("credential_id"):
            raise ValueError("No matching OAuth checkpoint is available to resume.")
        vault_id, credential_id = checkpoint["vault_id"], checkpoint["credential_id"]
        credential = api.request_json("GET", "/vaults/" + quote(vault_id, safe="") + "/credentials/" + quote(credential_id, safe=""))
        if credential.get("auth", {}).get("type") != "mcp_oauth" or credential["auth"].get("mcp_server_url") != RESOURCE:
            raise ValueError("The checkpoint does not reference an Atlassian OAuth credential.")
    else:
        print("This setup will link browser-approved, read-only Jira OAuth access to the selected agent in OpenAI Vault, then run one read-only connection test.", flush=True)
        resource, metadata = discover_oauth()
        if any(scope not in resource.get("scopes_supported", []) for scope in SCOPES):
            raise ValueError("The MCP server did not advertise all requested read-only scopes.")
        client, tokens = authorize(metadata, SCOPES, open_browser=not args.no_browser)
        if not tokens.get("refresh_token"):
            raise ValueError("Atlassian did not return a refresh token despite offline_access; setup stopped before storing a credential that cannot renew.")
        vault_id = checkpoint.get("vault_id")
        if not vault_id:
            vault = api.request_json("POST", "/vaults", {"name": "Nova Atlassian OAuth"}, extra_headers=["Idempotency-Key: " + uuid4().hex])
            vault_id = vault["id"]
        credential = api.request_json("POST", "/vaults/" + quote(vault_id, safe="") + "/credentials", {"name": "Nova Atlassian OAuth read-only", "auth": credential_auth(metadata, client, tokens)}, extra_headers=["Idempotency-Key: " + uuid4().hex])
        credential_id = credential["id"]
        state["atlassian_oauth"] = {"agent_id": agent_id, "vault_id": vault_id, "credential_id": credential_id}
        write_state(state)
    tools = updated_tools(saved, credential_id)
    api.request_json("POST", "/agents/" + quote(agent_id, safe=""), {"tools": tools})
    state["agent_id"] = agent_id
    state["vault_ids"] = [vault_id]
    write_state(state)
    print("OAuth credential saved in OpenAI Vault and linked to the existing agent.\nVault ID: " + vault_id + "\nCredential ID: " + credential_id, flush=True)
    print("Starting the OpenAI-hosted read-only MCP test...", flush=True)
    if not test_session(agent_id, vault_id, state):
        return 1
    if args.run_app:
        os.environ["OPENAI_AGENT_ID"] = agent_id
        os.environ["OPENAI_VAULT_IDS"] = vault_id
        os.environ.setdefault("HOST", "127.0.0.1")
        print("Open http://localhost:" + os.environ.get("PORT", "8080") + " and click New chat.", flush=True)
        app.main([])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled. No Jira tickets were changed.")
        raise SystemExit(130)
    except (api.OpenAIRequestError, OSError, ValueError, KeyError, getpass.GetPassWarning, subprocess.TimeoutExpired) as exc:
        print("Setup error: " + api._redact_runtime_secrets(str(exc))[:800])
        print("If a Vault credential was already checkpointed, use --resume with the same --agent-id to retry without signing in again.")
        raise SystemExit(1)
