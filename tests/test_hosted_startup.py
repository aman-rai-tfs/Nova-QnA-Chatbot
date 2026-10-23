"""Hosted sessions submit their first message only after provisioning."""

import unittest
from unittest.mock import patch

import openai_http as api


class HostedStartupTests(unittest.TestCase):
    def session(self):
        return {"id": "sess_test", "status": "idle", "environment": {"type": "openai_hosted", "id": "environment_test"}}

    def test_create_without_input_then_wait_then_send_exactly_one_message(self):
        actions = []
        session = self.session()
        statuses = iter(["pending", "ready", "connected"])
        def request(method, endpoint, payload=None, **kwargs):
            actions.append((method, endpoint, payload))
            if method == "POST":
                self.assertNotIn("input", payload)
                self.assertNotIn("stream", payload)
                self.assertNotIn("agent", payload)
                self.assertEqual(payload["vault_ids"], ["vault_oauth"])
                return session
            if endpoint.startswith("/agents/environments/"):
                return {"status": next(statuses)}
            return session
        def followup(session_id, message, on_event, vector_store):
            actions.append(("SEND", session_id, message))
        with patch.object(api, "request_json", side_effect=request), patch.object(api, "stream_followup_session", side_effect=followup) as send, patch.object(api.time, "sleep"):
            result = api.stream_initial_session("agent_saved", "read my Jira sites", lambda *_: None, "vs_test", vault_ids=["vault_oauth"], environment={"type": "openai_hosted", "environment_template_id": "envtmpl_test"})
        self.assertEqual(result, "sess_test")
        send.assert_called_once()
        self.assertEqual(actions[-1], ("SEND", "sess_test", "read my Jira sites"))
        self.assertEqual(len([action for action in actions if action[0] == "GET" and action[1].startswith("/agents/environments/")]), 3)

    def test_environment_failure_prevents_user_input(self):
        session = self.session()
        with patch.object(api, "request_json", side_effect=[session, {"status": "failed"}]), patch.object(api, "stream_followup_session") as send:
            with self.assertRaisesRegex(api.OpenAIRequestError, "could not start"):
                api.stream_initial_session("agent_saved", "hello", lambda *_: None, "vs_test", environment={"type": "openai_hosted"})
        send.assert_not_called()

    def test_timeout_prevents_user_input(self):
        with patch.object(api, "request_json", return_value={"status": "pending"}), patch.object(api.time, "monotonic", side_effect=[0, 1]):
            with self.assertRaisesRegex(api.OpenAIRequestError, "No user input was submitted"):
                api._wait_for_hosted_environment(self.session(), lambda *_: None, timeout=0)

    def test_curl_stream_suppresses_proxy_headers_and_disables_curlrc(self):
        with patch.object(api, "_curl", return_value="curl"):
            args = api._request_args("GET", "https://api.openai.com/v1/agents/sessions/sess_test/events", stream=True)
        self.assertEqual(args[1], "--disable")
        self.assertIn("--suppress-connect-headers", args)


if __name__ == "__main__":
    unittest.main()
