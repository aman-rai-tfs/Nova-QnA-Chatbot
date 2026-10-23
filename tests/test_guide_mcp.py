"""Offline coverage for hosted guide networking, secret handling, and refresh."""

import contextlib
import copy
import io
import json
import os
import subprocess
import unittest
from unittest.mock import patch

import guide_mcp
import setup_guide_mcp as setup


class GuideSearchTests(unittest.TestCase):
    def test_stdio_inherits_proxy_and_trust_configuration(self):
        variables = setup.mcp_tool()["transport"]["env_vars"]
        for name in (*guide_mcp.NETWORK_ENV_VARIABLES, guide_mcp.TOKEN_VARIABLE,
                     guide_mcp.STORE_VARIABLE, guide_mcp.PROJECT_VARIABLE):
            self.assertIn(name, variables)

    def test_search_returns_passages_and_sends_credentials_only_on_stdin(self):
        response = {"data": [{"file_id": "file_guide", "filename": "guide.docx", "score": 0.8,
                              "content": [{"type": "text", "text": "Section 1.3: CPE support access."}]}]}
        result = subprocess.CompletedProcess([], 0, json.dumps(response) + "\n200", "")
        with patch.object(guide_mcp.shutil, "which", return_value="curl"), patch.object(
                guide_mcp.subprocess, "run", return_value=result) as run:
            output = guide_mcp.search_vector_store("support", "vs_guide", "proj_nova", "secret-token")
        self.assertIn("Section 1.3", output["matches"][0]["text"])
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[1], "--disable")
        self.assertNotIn("secret-token", " ".join(arguments))
        self.assertIn("secret-token", run.call_args.kwargs["input"])
        self.assertEqual(arguments[-1], "https://api.openai.com/v1/vector_stores/vs_guide/search")

    def test_connection_error_reports_presence_without_proxy_credentials(self):
        result = subprocess.CompletedProcess([], 7, "\n000",
                                             "curl: Failed to connect https://user:proxy-pass@proxy.example secret-token")
        with patch.dict(os.environ, {"HTTPS_PROXY": "https://user:proxy-pass@proxy.example"}, clear=True), patch.object(
                guide_mcp.shutil, "which", return_value="curl"), patch.object(
                guide_mcp.subprocess, "run", return_value=result):
            with self.assertRaises(guide_mcp.GuideSearchError) as raised:
                guide_mcp.search_vector_store("support", "vs_guide", "proj_nova", "secret-token")
        message = str(raised.exception)
        self.assertIn("curl status 7", message)
        self.assertIn("HTTPS_PROXY", message)
        self.assertNotIn("proxy-pass", message)
        self.assertNotIn("secret-token", message)

    def test_http_failure_does_not_echo_the_response_body(self):
        result = subprocess.CompletedProcess([], 0, 'sensitive response body\n401', "")
        with patch.object(guide_mcp.shutil, "which", return_value="curl"), patch.object(
                guide_mcp.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(guide_mcp.GuideSearchError, "HTTP 401") as raised:
                guide_mcp.search_vector_store("support", "vs_guide", "proj_nova", "secret-token")
        self.assertNotIn("sensitive response", str(raised.exception))


class GuideSetupTests(unittest.TestCase):
    def test_tool_replacement_preserves_the_jira_configuration(self):
        jira = {"type": "mcp", "server_label": "atlassian-openai-platform",
                "transport": {"type": "http", "server_url": "https://mcp.atlassian.com/v2/mcp"},
                "credential_id": "credential_jira", "allowed_tools": ["executeRead"], "required": True}
        original = {"tools": [{"type": "function", "name": "search_nova_guide"}, jira]}
        updated = setup.replace_guide_tool(original)
        self.assertEqual(updated[0], jira)
        self.assertEqual(original["tools"][0]["type"], "function")
        self.assertEqual(updated[1]["server_label"], setup.SERVER_LABEL)

    def test_refresh_reuses_the_credential_and_preserves_original_instructions(self):
        state = {"agent_id": "agent_nova", "vector_store_id": "vs_guide", "vault_ids": ["vault_jira"],
                 "environment_template_id": "envtmpl_old", "hosted_guide_mcp": {
                     "agent_id": "agent_nova", "vector_store_id": "vs_guide", "project_id": "proj_nova",
                     "configuration_sha256": "old-source", "vault_id": "vault_jira",
                     "credential_id": "credential_guide", "environment_template_id": "envtmpl_old"}}
        saved = {"instructions": "Keep these user-edited instructions.",
                 "tools": [{"type": "function", "name": "search_nova_guide"}]}
        calls = []

        def request(method, endpoint, body=None, **kwargs):
            calls.append((method, endpoint, copy.deepcopy(body)))
            if method == "GET" and endpoint == "/agents/agent_nova":
                return copy.deepcopy(saved)
            if method == "GET" and endpoint == "/vector_stores/vs_guide":
                return {"status": "completed", "file_counts": {"completed": 1}}
            if method == "GET" and endpoint == "/vaults/vault_jira":
                return {"id": "vault_jira"}
            if method == "GET" and endpoint.endswith("/credentials/credential_guide"):
                return {"auth": {"type": "environment_variable", "secret_name": guide_mcp.TOKEN_VARIABLE,
                                 "networking": {"type": "limited", "allowed_hosts": ["api.openai.com"]}}}
            if method == "POST" and endpoint == "/agents/environments/templates":
                return {"id": "envtmpl_new"}
            if method == "POST" and endpoint == "/agents/agent_nova":
                return {"id": "agent_nova"}
            raise AssertionError((method, endpoint))

        with patch.dict(os.environ, {"OPENAI_API_KEY": "setup-key", "OPENAI_PROJECT_ID": "proj_nova"}, clear=True), patch.object(
                setup, "read_state", return_value=state), patch.object(setup, "write_state"), patch.object(
                setup.app, "agent_vault_ids", return_value=["vault_jira"]), patch.object(
                setup.api, "request_json", side_effect=request), patch.object(
                setup, "hidden_value", side_effect=AssertionError("A refresh must not ask for a new key")), contextlib.redirect_stdout(io.StringIO()):
            code = setup.main(["--apply", "--resume", "--refresh-template"])
        self.assertEqual(code, 0)
        self.assertEqual(state["environment_template_id"], "envtmpl_new")
        self.assertEqual(state["hosted_guide_mcp"]["credential_id"], "credential_guide")
        self.assertEqual(state["hosted_guide_mcp"]["previous_templates"], ["envtmpl_old"])
        self.assertFalse(any(method == "POST" and "/vaults" in endpoint for method, endpoint, _ in calls))
        update = next(body for method, endpoint, body in calls if method == "POST" and endpoint == "/agents/agent_nova")
        self.assertTrue(update["instructions"].startswith(saved["instructions"]))
        self.assertIn(setup.RETRIEVAL_FAILURE_RULE, update["instructions"])


if __name__ == "__main__":
    unittest.main()
