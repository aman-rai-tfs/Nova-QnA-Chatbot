"""Offline regressions for Vault-only MCP auth and the OAuth setup helper."""

import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import openai_http as api
import setup_atlassian_oauth as oauth


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"OPENAI_API_KEY": "fake-openai-key"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(api._extra_runtime_secrets.clear)

    def saved_agent(self):
        return {"tools": [{"type": "function", "name": "search_nova_guide", "parameters": {}},
            {"type": "mcp", "server_label": "atlassian-openai-platform", "transport": {"type": "http", "server_url": oauth.RESOURCE, "authorization": "old", "headers": {"AUTHORIZATION": "old", "X-Context": "keep"}}, "credential_id": "credential_old", "allowed_tools": None, "required": False}]}

    def test_vault_binding_preserves_guide_and_never_contains_authorization(self):
        saved = self.saved_agent()
        original = copy.deepcopy(saved)
        tools = oauth.updated_tools(saved, "credential_oauth")
        self.assertEqual(saved, original)
        self.assertEqual(tools[0], saved["tools"][0])
        self.assertEqual(tools[1]["credential_id"], "credential_oauth")
        self.assertEqual(tools[1]["transport"], {"type": "http", "server_url": oauth.RESOURCE, "headers": {"X-Context": "keep"}})
        self.assertTrue(tools[1]["required"])
        self.assertNotIn("createJiraIssue", tools[1]["allowed_tools"])
        self.assertNotIn("executeWrite", tools[1]["allowed_tools"])

    def test_existing_allowlist_cannot_be_broadened(self):
        saved = self.saved_agent()
        saved["tools"][1]["allowed_tools"] = ["getAccessibleAtlassianResources"]
        self.assertEqual(oauth.updated_tools(saved, "credential_oauth")[1]["allowed_tools"], ["getAccessibleAtlassianResources"])

    def test_credential_has_refresh_and_expiry(self):
        auth = oauth.credential_auth({"token_endpoint": "https://auth.atlassian.com/oauth/token"}, {"client_id": "client_test"}, {"access_token": "fake-access", "refresh_token": "fake-refresh", "expires_in": 3600, "scope": "read:me offline_access"})
        self.assertEqual(auth["type"], "mcp_oauth")
        self.assertEqual(auth["refresh"]["token_endpoint_auth"], {"type": "none"})
        self.assertEqual(auth["refresh"]["resource"], oauth.RESOURCE)
        self.assertTrue(auth["expires_at"].endswith("Z"))

    def test_discovery_validates_issuer_and_https_endpoints(self):
        issuer = "https://auth.atlassian.com/issuer"
        resource = {"resource": oauth.RESOURCE, "authorization_servers": [issuer]}
        metadata = {"issuer": issuer, "authorization_endpoint": "https://auth.atlassian.com/authorize", "token_endpoint": "https://auth.atlassian.com/oauth/token", "registration_endpoint": "https://auth.atlassian.com/issuer/dcr/register", "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"]}
        with patch.object(oauth, "https_json", side_effect=[resource, metadata]) as request:
            self.assertEqual(oauth.discover_oauth(), (resource, metadata))
        self.assertEqual(request.call_args_list[1].args[1], "https://auth.atlassian.com/.well-known/oauth-authorization-server/issuer")
        for url in ("http://auth.atlassian.com/token", "https://evil.example/token", "https://auth.atlassian.com@evil.example/token"):
            with self.assertRaises(ValueError):
                oauth.trusted_url(url, "auth.atlassian.com")

    def test_callback_rejects_wrong_state_and_accepts_only_the_original_flow(self):
        result = {}
        handler_class = oauth.callback_handler("expected-state", result)
        handler = object.__new__(handler_class)
        handler.send_response = lambda *_: None
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        handler.path = "/callback?state=wrong&code=stolen"
        handler.do_GET()
        self.assertEqual(result, {})
        handler.path = "/callback?state=expected-state&code=valid-code"
        handler.do_GET()
        self.assertEqual(result, {"code": "valid-code"})

    def test_authorization_uses_pkce_and_exchanges_only_the_received_code(self):
        metadata = {"registration_endpoint": "https://auth.atlassian.com/issuer/dcr/register", "authorization_endpoint": "https://auth.atlassian.com/authorize", "token_endpoint": "https://auth.atlassian.com/oauth/token"}
        class Server:
            server_port = 12345
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def handle_request(self): pass
        def handler(_state, result):
            result["code"] = "fake-code"
            return object
        tokens = {"token_type": "Bearer", "access_token": "fake-access", "refresh_token": "fake-refresh"}
        with patch.object(oauth, "callback_handler", side_effect=handler), patch.object(oauth, "HTTPServer", return_value=Server()), patch.object(oauth, "https_json", side_effect=[{"client_id": "client_test", "token_endpoint_auth_method": "none"}, tokens]) as request, patch("builtins.print"):
            client, result = oauth.authorize(metadata, oauth.SCOPES, open_browser=False)
        self.assertEqual(result, tokens)
        self.assertEqual(client["client_id"], "client_test")
        exchange = request.call_args_list[1].kwargs["form"]
        self.assertEqual(exchange["code"], "fake-code")
        self.assertTrue(exchange["code_verifier"])
        self.assertEqual(exchange["resource"], oauth.RESOURCE)

    def test_vault_payload_is_sent_only_via_stdin(self):
        api.register_runtime_secrets("fake-access", "fake-refresh")
        response = type("Response", (), {"returncode": 0, "stdout": '{"id":"credential_test"}', "stderr": ""})()
        with patch.object(api, "_curl", return_value="curl"), patch.object(api.subprocess, "run", return_value=response) as request:
            api.request_json("POST", "/vaults/vault_test/credentials", {"auth": {"type": "mcp_oauth", "access_token": "fake-access", "refresh": {"refresh_token": "fake-refresh"}}})
        self.assertNotIn("fake-access", " ".join(request.call_args.args[0]))
        self.assertIn("fake-access", request.call_args.kwargs["input"])
        self.assertNotIn("--data-binary", request.call_args.args[0])
        error = api._error_text('{"error":{"message":"fake-access fake-refresh fake-openai-key"}}', "", 22)
        self.assertNotIn("fake-access", error)
        self.assertNotIn("fake-refresh", error)

    def test_retired_flag_fails_before_prompting_for_any_secrets(self):
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
            app.main(["--atlassian-mcp"])
        self.assertEqual(error.exception.code, 2)

    def test_explicit_tool_errors_are_not_reported_as_success(self):
        self.assertTrue(oauth.output_has_error('{"content":[{"type":"text","text":"{\\"error\\":true}"}]}'))
        self.assertTrue(oauth.output_has_error({"isError": True}))
        self.assertFalse(oauth.output_has_error([{"id": "cloud_test", "name": "Jira"}]))

    def test_only_ids_are_checkpointed_and_existing_edits_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "STATE_PATH", Path(directory) / "nova_state.json"):
            state = {"vector_store_id": "vs_test", "custom": "keep", "atlassian_oauth": {"agent_id": "agent_test", "vault_id": "vault_test", "credential_id": "credential_test"}}
            oauth.write_state(state)
            self.assertEqual(oauth.read_state(), state)
            self.assertFalse(app.STATE_PATH.with_suffix(".json.tmp").exists())

    def test_hosted_template_is_read_from_state_and_environment_can_override(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "STATE_PATH", Path(directory) / "nova_state.json"):
            oauth.write_state({"environment_template_id": "envtmpl_working"})
            self.assertEqual(app.session_environment(), {"type": "openai_hosted", "environment_template_id": "envtmpl_working"})
            os.environ["OPENAI_ENVIRONMENT_TEMPLATE_ID"] = "envtmpl_override"
            self.assertEqual(app.session_environment()["environment_template_id"], "envtmpl_override")
            os.environ["OPENAI_ENVIRONMENT_TEMPLATE_ID"] = "wrong-id"
            with self.assertRaises(api.OpenAIRequestError):
                app.session_environment()

    def test_live_test_requires_actual_successful_mcp_item(self):
        state = {"atlassian_oauth": {}}
        def stream(_agent, _message, on_event, _vector_store, **kwargs):
            self.assertEqual(kwargs["vault_ids"], ["vault_test"])
            on_event("event", {"type": "agent.session.turn.item.done", "session_id": "sess_test", "item": {"type": "mcp_call", "name": "getAccessibleAtlassianResources", "status": "completed", "error": None, "output": [{"id": "cloud_test"}]}})
            return "sess_test"
        with patch.object(api, "stream_initial_session", side_effect=stream), patch.object(oauth, "write_state"), patch("builtins.print"):
            self.assertTrue(oauth.test_session("agent_test", "vault_test", state))
        with patch.object(api, "stream_initial_session", return_value="sess_no_tool"), patch("builtins.print"):
            self.assertFalse(oauth.test_session("agent_test", "vault_test", state))


if __name__ == "__main__":
    unittest.main()
