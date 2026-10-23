"""Migrate hosted guide MCP to HTTP, checking retrieval before saving the agent.

Reuses the indexed guide and existing Vault search credential. A supplied JSON
draft preserves unsaved Platform edits. Live checks expose only read tools;
they cannot create or change Jira records.
"""

from __future__ import annotations

import argparse
import base64
import copy
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import uuid4

import app
import guide_mcp
import openai_http as api
import setup_guide_mcp as setup
from setup_atlassian_oauth import output_has_error

TEMPLATE_NAME = "Nova guide MCP HTTP"
JIRA_LABEL = "atlassian-openai-platform"
READ_TOOLS = ["getAccessibleAtlassianResources", "atlassianUserInfo", "getJiraIssue",
              "searchJiraIssuesUsingJql", "discover", "executeRead"]
FIELDS = ("name", "model", "instructions", "tools", "reasoning", "text", "multi_agent", "service_tier")
READ_ONLY_PARAGRAPH = (
    "The current Jira connection is read-only. You may prepare a ticket draft, but do not create, "
    "update, transition, comment on, or delete Jira records, and do not claim that a write succeeded."
)
CREATE_POLICY = """## Jira ticket creation permissions

You may create a new Jira issue only after the user explicitly approves its complete draft.
First use read tools to verify the site, project, available issue type, required fields,
and any parent epic. A board alone is not a project or permission grant. Ask for missing
information rather than inventing values. Show the exact site, project key, issue type,
summary, description, parent (if any), and all other fields that will be submitted.
Wait for explicit approval of that draft. If any field changes, request approval again.
Use only createJiraIssue for the approved creation. Do not create test tickets.
Do not edit, transition, comment on, delete, or otherwise modify existing Jira records.
After a successful call, report the returned issue key and URL. An unclear result or
timeout does not establish success: check for the issue before retrying to avoid duplicates.
Available tool names do not prove write permission. Report authentication or permission
errors honestly; never claim a ticket was created unless Jira confirms it.
"""


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def http_tool() -> dict:
    return {"type": "mcp", "server_label": setup.SERVER_LABEL,
            "transport": {"type": "http", "server_url": "http://127.0.0.1:8765/mcp"},
            "connection_origin": "environment",
            "allowed_tools": ["search_nova_guide"], "required": True}


def template_body(store_id: str, project_id: str) -> dict:
    body = setup.template_body(store_id, project_id)
    body["name"] = TEMPLATE_NAME
    launcher = Path(__file__).with_name("start_guide_http.py")
    body["files"].append({"type": "inline", "path": "/workspace/nova-guide/start_guide_http.py",
                          "data": base64.b64encode(launcher.read_bytes()).decode("ascii")})
    body["setup_commands"].append({"command": setup.PYTHON_PATH + " /workspace/nova-guide/start_guide_http.py"})
    return body


def agent_fields(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Agent configuration must be a JSON object.")
    result = {key: copy.deepcopy(value[key]) for key in FIELDS if key in value}
    if not isinstance(result.get("tools"), list) or not isinstance(result.get("instructions"), str):
        raise ValueError("Agent configuration needs a tools list and text instructions.")
    # Only reference IDs belong in the recovery file. Reject inline secrets.
    for tool in result["tools"]:
        if not isinstance(tool, dict):
            raise ValueError("Invalid agent tool.")
        if tool.get("type") == "mcp":
            transport = tool.get("transport", {})
            if (transport.get("authorization") or transport.get("headers")
                    or transport.get("env") or tool.get("authorization")):
                raise ValueError("Remove inline credentials/headers/env from the draft; use Vault credential IDs.")
            # GET responses and the Platform editor may supply an empty map.
            # It is accepted on writes only for service-origin HTTP MCPs.
            if transport.get("type") != "http" or tool.get("connection_origin") == "environment":
                if tool.get("request_metadata"):
                    raise ValueError("Nonempty MCP request_metadata requires service-origin HTTP; review the draft.")
                tool.pop("request_metadata", None)
            if transport.get("type") == "stdio":
                tool.pop("connection_origin", None)
    return result


def candidate_agent(source: dict, enable_create: bool) -> dict:
    candidate = agent_fields(source)
    found = 0
    jira = 0
    for index, tool in enumerate(candidate["tools"]):
        if tool.get("server_label") == setup.SERVER_LABEL or (
                tool.get("type") == "function" and tool.get("name") == "search_nova_guide"):
            candidate["tools"][index] = http_tool()
            found += 1
        elif tool.get("server_label") == JIRA_LABEL:
            jira += 1
            if tool.get("transport", {}).get("server_url") != api.ATLASSIAN_MCP_URL:
                raise ValueError("Unexpected Atlassian MCP URL; stopped before using its credential.")
            setup.validate_id(tool.get("credential_id"), "credential_")
            if enable_create:
                tool["allowed_tools"] = [*READ_TOOLS, "createJiraIssue"]
            tool["connection_origin"] = "service"
            tool["required"] = True
    if found != 1 or jira > 1:
        raise ValueError("Expected exactly one guide tool and at most one Atlassian tool.")
    if enable_create:
        if jira != 1:
            raise ValueError("Ticket creation requires the existing Atlassian MCP connection.")
        instructions = candidate["instructions"]
        if READ_ONLY_PARAGRAPH in instructions:
            instructions = instructions.replace(READ_ONLY_PARAGRAPH, CREATE_POLICY.strip())
        elif "## Jira ticket creation permissions" not in instructions:
            raise ValueError("Read-only policy was not recognized. Review instructions before enabling creation.")
        instructions = instructions.replace("Before creating or updating a ticket,", "Before creating a new ticket,")
        instructions = instructions.replace(
            "Show the user a draft of the ticket or proposed changes and wait for their explicit confirmation before creating or updating anything.",
            "Show the complete new-ticket draft and wait for explicit confirmation before creating it. Do not update existing tickets.")
        candidate["instructions"] = instructions
    return candidate


def page_items(endpoint: str):
    after = None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        page = api.request_json("GET", endpoint + "?" + urlencode(params))
        data = page.get("data")
        if not isinstance(data, list):
            raise ValueError("Unexpected resource-list response while locating the credential Vault.")
        yield from data
        if not page.get("has_more"):
            return
        cursor = page.get("last_id") or (data[-1].get("id") if data else None)
        if not cursor or cursor == after:
            raise ValueError("Credential listing did not advance; no agent changes made.")
        after = cursor


def credential_vault(credential_id: str, known_vaults: list[str]) -> str:
    # Read metadata only. Credential retrieval never returns the secret value.
    vault_ids = list(dict.fromkeys(known_vaults))
    for vault in page_items("/vaults"):
        vault_id = setup.validate_id(vault.get("id"), "vault_")
        if vault_id not in vault_ids:
            vault_ids.append(vault_id)
    for vault_id in vault_ids:
        for credential in page_items("/vaults/" + quote(vault_id, safe="") + "/credentials"):
            if credential.get("id") == credential_id:
                return vault_id
    raise ValueError("The draft credential was not found in an accessible Vault. No saved agent changes made.")


def check_turn(session_id: str, prompt: str, label: str, tool_name: str, *, guide: bool) -> bool:
    passed = False

    def event(kind: str, payload: dict) -> None:
        nonlocal passed
        actual = payload.get("type", kind)
        if actual == "agent.session.turn.output_text.delta":
            print(payload.get("delta", ""), end="", flush=True)
        if actual == "agent.session.turn.item.done":
            item = payload.get("item", {})
            if item.get("type") == "mcp_call" and item.get("server_label") == label:
                ok = (item.get("name") == tool_name and item.get("status") == "completed"
                      and not item.get("error") and item.get("output") is not None
                      and not output_has_error(item.get("output")))
                if guide:
                    ok = ok and setup.matching_passages(item.get("output"))
                print("\nMCP " + label + "/" + str(item.get("name")) + ": " +
                      ("verified result" if ok else "success not confirmed"), flush=True)
                passed = passed or ok

    api.stream_followup_session(session_id, prompt, event, "")
    print()
    return passed


def verify(candidate: dict, agent_id: str, template_id: str, vault_ids: list[str],
           state: dict, checkpoint: dict) -> None:
    # Session overrides leave the saved agent untouched. No write tools are
    # exposed during either check, regardless of what its instructions say.
    overrides = {key: copy.deepcopy(candidate[key]) for key in
                 ("model", "reasoning", "text", "multi_agent", "tools") if key in candidate}
    for tool in overrides["tools"]:
        if tool.get("server_label") == JIRA_LABEL:
            tool["allowed_tools"] = ["getAccessibleAtlassianResources"]
    overrides["instructions"] = (
        "This is a read-only connection check. For a guide question call search_nova_guide and cite returned passages. "
        "For a Jira-sites question call getAccessibleAtlassianResources and report its result. "
        "Do not create or change anything. Report tool failures honestly; never substitute a claimed result."
    )
    print("Creating a read-only verification session with the HTTP guide configuration...", flush=True)
    session = api.request_json("POST", "/agents/sessions", {
        "agent_id": agent_id, "agent": overrides, "vault_ids": vault_ids,
        "environment": {"type": "openai_hosted", "environment_template_id": template_id},
    }, extra_headers=["Idempotency-Key: " + uuid4().hex])
    session_id = setup.validate_id(session.get("id"), "sess_")
    checkpoint["verification_session_id"] = session_id
    checkpoint["verification_passed"] = False
    setup.write_state(state)
    print("Read-only verification session: https://platform.openai.com/agents/" + agent_id +
          "/sessions/" + session_id + "?project_id=" + checkpoint["project_id"], flush=True)

    def progress(kind: str, payload: dict):
        print("Hosted environment: " + str(payload.get("status", "pending")), flush=True)

    api._wait_for_hosted_environment(session, progress)
    if not check_turn(session_id,
                      "According to the Nova Deployment Guide, where should I raise a Nova deployment support request "
                      "and what access is required first? Search the guide and cite the relevant sections. Do not access Jira.",
                      setup.SERVER_LABEL, "search_nova_guide", guide=True):
        raise ValueError("HTTP guide retrieval was not verified. The saved agent and selected template were not changed.")
    if any(tool.get("server_label") == JIRA_LABEL for tool in overrides["tools"]):
        if not check_turn(session_id, "List my accessible Jira sites and cloud IDs. Do not create or modify anything.",
                          JIRA_LABEL, "getAccessibleAtlassianResources", guide=False):
            raise ValueError("The draft Jira credential did not pass the read check. No saved agent changes made. "
                             "The new draft is preserved; inspect the verification session for the authentication error.")
    checkpoint["verification_passed"] = True
    setup.write_state(state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="Review local configuration; no API calls.")
    mode.add_argument("--apply", action="store_true", help="Verify read access, then update the saved agent.")
    parser.add_argument("--draft-json", type=Path, help="JSON object copied from the Platform's unsaved agent configuration.")
    parser.add_argument("--enable-jira-create", action="store_true", help="Allow createJiraIssue only after explicit draft confirmation.")
    parser.add_argument("--resume", action="store_true", help="Reuse a checkpointed HTTP template.")
    parser.add_argument("--run-app", action="store_true", help="Run the local app after a successful migration.")
    args = parser.parse_args(argv)
    state = setup.read_state()
    guide = state.get("hosted_guide_mcp", {})
    agent_id = setup.validate_id(state.get("agent_id") or guide.get("agent_id"), "agent_")
    store_id = setup.validate_id(guide.get("vector_store_id") or state.get("vector_store_id"), "vs_")
    project_id = setup.validate_id(guide.get("project_id") or state.get("project_id"), "proj_")
    guide_vault = setup.validate_id(guide.get("vault_id"), "vault_")
    guide_credential = setup.validate_id(guide.get("credential_id"), "credential_")
    draft = agent_fields(json.loads(args.draft_json.read_text(encoding="utf-8-sig"))) if args.draft_json else None
    body = template_body(store_id, project_id)
    if args.plan:
        print(json.dumps({"agent_id": agent_id, "template_name": TEMPLATE_NAME, "guide_tool": http_tool(),
                          "guide_vault_id": guide_vault, "guide_credential_id": guide_credential,
                          "candidate": candidate_agent(draft, args.enable_jira_create) if draft else "Merge latest saved agent at apply time",
                          "verification": "Guide source passages and Jira read call before saving; no Jira writes",
                          "reuse": "Existing vector store and Vault search key"}, ensure_ascii=False, indent=2))
        return 0
    configured_project = os.environ.get("OPENAI_PROJECT_ID", project_id)
    if configured_project != project_id:
        raise ValueError("OPENAI_PROJECT_ID differs from the saved guide project; correct it before migration.")
    os.environ["OPENAI_PROJECT_ID"] = project_id
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        os.environ["OPENAI_API_KEY"] = setup.hidden_value("Existing OpenAI project/setup API key (hidden): ")
    endpoint = "/agents/" + quote(agent_id, safe="")
    saved = agent_fields(api.request_json("GET", endpoint))
    candidate = candidate_agent(draft or saved, args.enable_jira_create)
    store = api.request_json("GET", "/vector_stores/" + quote(store_id, safe=""))
    if store.get("status") != "completed" or store.get("file_counts", {}).get("completed", 0) < 1:
        raise ValueError("The existing guide store does not have a fully indexed file.")
    credential = api.request_json("GET", "/vaults/" + guide_vault + "/credentials/" + guide_credential)
    auth = credential.get("auth", {})
    if (auth.get("type") != "environment_variable" or auth.get("secret_name") != guide_mcp.TOKEN_VARIABLE
            or auth.get("networking", {}).get("type") != "limited"
            or auth.get("networking", {}).get("allowed_hosts") != ["api.openai.com"]):
        raise ValueError("Unexpected guide Vault credential configuration; no changes made.")
    vault_ids = list(dict.fromkeys([*state.get("vault_ids", []), *app.agent_vault_ids(), guide_vault]))
    for tool in candidate["tools"]:
        if tool.get("server_label") == JIRA_LABEL:
            vault_ids.append(credential_vault(tool["credential_id"], vault_ids))
    vault_ids = list(dict.fromkeys(vault_ids))
    migration = state.get("guide_http_migration")
    if migration and not args.resume:
        raise ValueError("An HTTP migration is checkpointed. Retry with --resume to reuse it.")
    configuration = fingerprint({"template": body, "candidate": candidate, "vault_ids": vault_ids})
    if migration:
        # Repair exactly the failed v1 request shape. The template's files,
        # Vaults, model, instructions, Jira credential and permissions must match.
        # Reuse its template rather than creating duplicate resources.
        legacy_candidate = copy.deepcopy(candidate)
        for tool in legacy_candidate["tools"]:
            if tool.get("server_label") == setup.SERVER_LABEL:
                tool["request_metadata"] = {}
        legacy_configuration = fingerprint({"template": body, "candidate": legacy_candidate, "vault_ids": vault_ids})
        if (migration.get("agent_id") == agent_id
                and migration.get("configuration_sha256") == legacy_configuration
                and agent_fields(migration.get("candidate_agent")) == candidate
                and not migration.get("applied") and not migration.get("verification_session_id")):
            migration["previous_configuration_sha256"] = migration["configuration_sha256"]
            migration["configuration_sha256"] = configuration
            migration["candidate_agent"] = copy.deepcopy(candidate)
            setup.write_state(state)
            print("Repaired the rejected metadata field in the checkpoint; reusing the existing HTTP template.", flush=True)
        if migration.get("agent_id") != agent_id or migration.get("configuration_sha256") != configuration:
            raise ValueError("The migration draft or code changed. Review the existing guide_http_migration checkpoint before replacing it.")
    if not migration:
        migration = {"agent_id": agent_id, "project_id": project_id, "configuration_sha256": configuration,
                     "previous_agent": saved, "candidate_agent": candidate,
                     "previous_environment_template_id": state.get("environment_template_id")}
        state["guide_http_migration"] = migration
        setup.write_state(state)
    template_id = migration.get("environment_template_id")
    if not template_id:
        print("Creating the HTTP environment template; reusing the existing Vault search key...", flush=True)
        template = setup.checkpoint_request(state, migration, "http_template", "/agents/environments/templates", body)
        template_id = setup.validate_id(template.get("id"), "envtmpl_")
        migration["environment_template_id"] = template_id
        setup.write_state(state)
    else:
        print("Reusing HTTP environment template: " + template_id, flush=True)
    # Every apply/resume performs fresh checks. A previous PASS is not reused.
    verify(candidate, agent_id, template_id, vault_ids, state, migration)
    latest = agent_fields(api.request_json("GET", endpoint))
    if fingerprint(latest) != fingerprint(saved):
        raise ValueError("The saved agent changed during verification. No update applied; review concurrent edits before resuming.")
    print("Read checks passed. Saving HTTP guide MCP and the reviewed Jira draft...", flush=True)
    api.request_json("POST", endpoint, candidate, extra_headers=["Idempotency-Key: " + uuid4().hex])
    # Normalize response defaults before saving: environment-origin MCP cannot
    # include request_metadata, even when a GET response contains an empty map.
    readback = agent_fields(api.request_json("GET", endpoint))
    readback_guide = next((t for t in readback["tools"] if t.get("server_label") == setup.SERVER_LABEL), {})
    if (readback_guide.get("transport", {}).get("type") != "http"
            or readback_guide.get("connection_origin") != "environment"):
        raise ValueError("Agent save returned an unexpected guide transport. Inspect the saved configuration before starting a chat.")
    migration["applied"] = True
    state.update(environment_template_id=template_id, vault_ids=vault_ids)
    guide.update(transport="http", environment_template_id=template_id)
    setup.write_state(state)
    api.request_json("POST", endpoint, readback, extra_headers=["Idempotency-Key: " + uuid4().hex])
    migration["api_roundtrip_passed"] = True
    setup.write_state(state)
    print("PASS: HTTP guide returned source passages; Jira read check passed when configured; API save round trip passed.")
    print("Environment template: " + TEMPLATE_NAME + " (" + template_id + ")")
    print("Guide API secret: " + guide_credential + " in " + guide_vault)
    print("Refresh the old Platform draft only now. Start a NEW hosted session with this template and the guide API secret.")
    print("Platform UI Save may still add unsupported metadata; it is not verified by this API check.")
    print("Jira creation permission has not been tested; no ticket was created.")
    if args.run_app:
        os.environ.update(OPENAI_AGENT_ID=agent_id, OPENAI_VECTOR_STORE_ID=store_id,
                          OPENAI_ENVIRONMENT_TEMPLATE_ID=template_id, OPENAI_VAULT_IDS=json.dumps(vault_ids))
        os.environ.setdefault("HOST", "127.0.0.1")
        app.main([])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nMigration cancelled. Retry the same command with --resume if resources were checkpointed.")
        raise SystemExit(130)
    except (api.OpenAIRequestError, OSError, ValueError, KeyError, getpass.GetPassWarning) as exc:
        print("Migration error: " + api._redact_runtime_secrets(str(exc))[:900], file=sys.stderr)
        print("Keep the unsaved draft. Checkpointed resources can be reused with --resume.", file=sys.stderr)
        raise SystemExit(1)
