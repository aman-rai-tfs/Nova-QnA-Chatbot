"""Offline regressions for session credentials and real API error reporting."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import openai_http


class ErrorReportingTests(unittest.TestCase):
    def test_pretty_printed_http_error_is_preserved(self):
        response = json.dumps({"error": {
            "message": "The MCP credential is not available in the attached vaults.",
            "code": "invalid_request_error", "param": "vault_ids",
        }}, indent=2)
        with self.assertRaisesRegex(openai_http.OpenAIRequestError, "MCP credential.*param=vault_ids"):
            openai_http._read_event_stream(io.StringIO(response), lambda *_: None)

    def test_compact_json_error_is_preserved(self):
        with self.assertRaisesRegex(openai_http.OpenAIRequestError, "bad credential"):
            openai_http._read_event_stream(io.StringIO('{"error":{"message":"bad credential"}}'), lambda *_: None)

    def test_regular_and_multiline_sse_are_unchanged(self):
        events = []
        response = ': keepalive\nid: evt_1\nretry: 1000\nevent: update\ndata: {\ndata: "type": "update", "text": "hello"}\n\ndata: [DONE]\n\n'
        openai_http._read_event_stream(io.StringIO(response), lambda kind, data: events.append((kind, data)))
        self.assertEqual(events, [("update", {"type": "update", "text": "hello"})])


class VaultConfigTests(unittest.TestCase):
    def test_explicit_ids_are_deduplicated(self):
        with patch.dict(os.environ, {"OPENAI_VAULT_IDS": "vault_one, vault_two,vault_one"}):
            self.assertEqual(app.agent_vault_ids(), ["vault_one", "vault_two"])

    def test_json_array_is_supported(self):
        with patch.dict(os.environ, {"OPENAI_VAULT_IDS": '["vault_one"]'}):
            self.assertEqual(app.agent_vault_ids(), ["vault_one"])

    def test_credential_id_cannot_be_mistaken_for_vault_id(self):
        with patch.dict(os.environ, {"OPENAI_VAULT_IDS": "credential_not_a_vault"}):
            with self.assertRaisesRegex(openai_http.OpenAIRequestError, "vault_ IDs"):
                app.agent_vault_ids()

    def test_state_fallback_and_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "nova_state.json"
            state_path.write_text('{"vault_ids":["vault_from_state"]}', encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch.object(app, "STATE_PATH", state_path):
                self.assertEqual(app.agent_vault_ids(), ["vault_from_state"])
                os.environ["OPENAI_VAULT_IDS"] = ""
                self.assertEqual(app.agent_vault_ids(), [])

    def test_session_request_attaches_vault_without_replacing_saved_tools(self):
        class FakeProcess:
            def __init__(self):
                class CapturingInput(io.StringIO):
                    def close(self):
                        submitted.append(json.loads(json.loads(self.getvalue().split('data-binary = ', 1)[1].strip())))
                        super().close()
                self.stdin = CapturingInput()
                self.stdout = io.StringIO('event: agent.session.turn.completed\ndata: {"type":"agent.session.turn.completed","session_id":"sess_test"}\n\n')
                self.stderr = io.StringIO()
            def poll(self): return 0
            def wait(self, timeout=None): return 0

        submitted = []
        def launch(args, **kwargs):
            self.assertNotIn("--data-binary", args)
            return FakeProcess()

        with patch.object(openai_http, "_config_stdin", return_value=""), patch.object(openai_http, "_curl", return_value="curl"), patch.object(openai_http.subprocess, "Popen", side_effect=launch):
            result = openai_http.stream_initial_session("agent_saved", "hello", lambda *_: None, "vs_test", vault_ids=["vault_explicit"])
        self.assertEqual(result, "sess_test")
        self.assertEqual(submitted[0]["vault_ids"], ["vault_explicit"])
        self.assertEqual(submitted[0]["agent_id"], "agent_saved")
        self.assertEqual(submitted[0]["environment"], {"type": "none"})
        self.assertNotIn("agent", submitted[0])


if __name__ == "__main__":
    unittest.main()
