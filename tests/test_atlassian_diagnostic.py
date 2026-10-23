import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import diagnose_atlassian_mcp as diagnostic


def reply(status=200, result=None, error=None, headers=None):
    message = {"jsonrpc": "2.0", "id": 1}
    if error:
        message["error"] = error
    else:
        message["result"] = result if result is not None else {"tools": [{"name": "discover"}]}
    return diagnostic.Reply(status, headers or {}, message, error.get("message", "") if error else "")


class DiagnosticTests(unittest.TestCase):
    def test_pretty_printed_http_json_error(self):
        response = 'HTTP/2 401\r\nContent-Type: application/json\r\n\r\n' + json.dumps({"error": {"message": "Invalid credential"}}, indent=2)
        parsed = diagnostic.parse_reply(response)
        self.assertEqual(parsed.status, 401)
        self.assertEqual(parsed.text, "Invalid credential")

    def test_sse_progress_is_ignored_until_rpc_result(self):
        response = 'HTTP/2 200\nContent-Type: text/event-stream\n\nevent: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\nevent: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"discover"}]}}\n\n'
        self.assertEqual(diagnostic.tools_from(diagnostic.parse_reply(response)), [{"name": "discover"}])

    def test_interim_http_headers_are_skipped(self):
        response = 'HTTP/1.1 100 Continue\n\nHTTP/1.1 200 OK\nContent-Type: application/json\n\n{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
        self.assertEqual(diagnostic.parse_reply(response).status, 200)

    def test_unauthorized_does_not_get_misdiagnosed_as_protocol_failure(self):
        with patch.object(diagnostic, "rpc", return_value=reply(401, error={"message": "Unauthorized"})) as rpc, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(diagnostic.discover("Basic placeholder", []), (False, 401))
            self.assertEqual(rpc.call_count, 1)

    def test_legacy_handshake_preserves_negotiated_version_and_session(self):
        replies = [reply(400, error={"message": "Initialize first"}),
                   reply(result={"protocolVersion": "2025-06-18"}, headers={"mcp-session-id": "private-session"}),
                   diagnostic.Reply(202, {}, None, ""), reply()]
        with patch.object(diagnostic, "rpc", side_effect=replies) as rpc, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(diagnostic.discover("Basic placeholder", []), (True, 200))
            self.assertEqual([call.args[1]["method"] for call in rpc.call_args_list],
                             ["tools/list", "initialize", "notifications/initialized", "tools/list"])
            self.assertEqual(rpc.call_args_list[-1].kwargs, {"version": "2025-06-18", "session": "private-session"})

    def test_modern_header_validation_error_is_not_hidden_by_fallback(self):
        with patch.object(diagnostic, "rpc", return_value=reply(400, error={"code": -32020, "message": "Header mismatch"})) as rpc, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(diagnostic.discover("Basic placeholder", []), (False, 400))
            self.assertEqual(rpc.call_count, 1)

    def test_secrets_are_redacted_from_server_errors(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            diagnostic.print_status("test", diagnostic.Reply(403, {}, None, "email=person@example.org token=test-secret basic=encoded-secret"),
                                    ["person@example.org", "test-secret", "encoded-secret"])
        self.assertNotIn("test-secret", output.getvalue())
        self.assertNotIn("person@example.org", output.getvalue())
        self.assertNotIn("encoded-secret", output.getvalue())

    def test_credentials_are_only_sent_on_stdin(self):
        result = subprocess.CompletedProcess([], 0, 'HTTP/2 200\nContent-Type: application/json\n\n{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}', '')
        with patch.object(diagnostic.shutil, "which", return_value="curl"), patch.object(diagnostic.subprocess, "run", return_value=result) as run:
            diagnostic.rpc("Bearer test-secret", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, version=diagnostic.MODERN_VERSION)
        self.assertNotIn("test-secret", " ".join(run.call_args.args[0]))
        self.assertIn("test-secret", run.call_args.kwargs["input"])
        self.assertNotIn("--location", run.call_args.args[0])

    def test_tool_execution_is_blocked(self):
        with self.assertRaisesRegex(ValueError, "never tool execution"):
            diagnostic.rpc("Bearer placeholder", {"method": "tools/call", "params": {"name": "createJiraIssue"}}, version=diagnostic.MODERN_VERSION)


if __name__ == "__main__":
    unittest.main()
