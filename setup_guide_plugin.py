"""Install guide MCP through the hosted environment, keeping agent UI saves valid.

The plugin registers the same loopback HTTP guide server from its environment
template. The agent form then contains only service-origin MCP entries, so it
cannot add service-only request metadata to the guide's environment connection.
"""

from __future__ import annotations

import argparse
import base64
import copy
import getpass
import io
import json
import os
import sys
import zipfile
from urllib.parse import quote
from uuid import uuid4

import app
import guide_mcp
import migrate_guide_http as migration
import openai_http as api
import setup_guide_mcp as setup

TEMPLATE_NAME = "Nova guide MCP plugin"
PLUGIN_NAME = "nova-guide-search"
PLUGIN_DESCRIPTION = "Read-only search of the indexed Nova Deployment Guide."


def plugin_archive() -> bytes:
    manifest = {"name": PLUGIN_NAME, "version": "1.0.0", "description": PLUGIN_DESCRIPTION,
                "mcpServers": "./.mcp.json"}
    config = {"mcpServers": {setup.SERVER_LABEL: {"type": "http", "url": "http://127.0.0.1:8765/mcp"}}}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, value in ((".codex-plugin/plugin.json", manifest), (".mcp.json", config)):
            info = zipfile.ZipInfo(PLUGIN_NAME + "/" + path, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, json.dumps(value, indent=2) + "\n")
    return output.getvalue()


def template_body(store_id: str, project_id: str) -> dict:
    body = migration.template_body(store_id, project_id)
    body["name"] = TEMPLATE_NAME
    body["plugins"] = [{"type": "inline", "name": PLUGIN_NAME, "description": PLUGIN_DESCRIPTION,
                        "source": {"type": "base64", "media_type": "application/zip",
                                   "data": base64.b64encode(plugin_archive()).decode("ascii")}}]
    return body


def without_guide_entry(saved: dict, *, already_applied: bool) -> dict:
    candidate = migration.agent_fields(saved)
    tools = []
    found = 0
    for tool in candidate["tools"]:
        if tool.get("server_label") == setup.SERVER_LABEL or (
                tool.get("type") == "function" and tool.get("name") == "search_nova_guide"):
            found += 1
        else:
            # A second environment MCP would leave the same editor issue.
            if tool.get("type") == "mcp" and (tool.get("transport", {}).get("type") != "http"
                                                or tool.get("connection_origin") == "environment"):
                raise ValueError("Another environment MCP is present in the agent form; review it before claiming UI saving is repaired.")
            tools.append(tool)
    if found != 1 and not (already_applied and found == 0):
        raise ValueError("Expected one explicit guide entry, or an already-applied guide plugin.")
    candidate["tools"] = tools
    return candidate


def verify(candidate: dict, agent_id: str, template_id: str, vault_ids: list[str],
           state: dict, checkpoint: dict) -> None:
    overrides = {key: copy.deepcopy(candidate[key]) for key in
                 ("model", "reasoning", "text", "multi_agent", "tools") if key in candidate}
    for tool in overrides["tools"]:
        if tool.get("server_label") == migration.JIRA_LABEL:
            tool["allowed_tools"] = ["getAccessibleAtlassianResources"]
    overrides["instructions"] = (
        "Run only read-only connection checks. The hosted nova-guide-search plugin provides search_nova_guide. "
        "For a guide question call that MCP tool and cite the source passages it returns. "
        "For a Jira-sites question call getAccessibleAtlassianResources. "
        "Do not create or change any records. Report tool failures honestly."
    )
    print("Creating a read-only session to verify the environment plugin before changing the saved agent...", flush=True)
    session = api.request_json("POST", "/agents/sessions", {
        "agent_id": agent_id, "agent": overrides, "vault_ids": vault_ids,
        "environment": {"type": "openai_hosted", "environment_template_id": template_id},
    }, extra_headers=["Idempotency-Key: " + uuid4().hex])
    session_id = setup.validate_id(session.get("id"), "sess_")
    checkpoint.update(verification_session_id=session_id, verification_passed=False)
    setup.write_state(state)
    print("Verification session: https://platform.openai.com/agents/" + agent_id + "/sessions/" + session_id
          + "?project_id=" + checkpoint["project_id"], flush=True)

    def progress(kind: str, payload: dict):
        print("Hosted environment: " + str(payload.get("status", "pending")), flush=True)

    api._wait_for_hosted_environment(session, progress)
    guide_passed = False

    def guide_event(kind: str, payload: dict):
        nonlocal guide_passed
        actual = payload.get("type", kind)
        if actual == "agent.session.turn.output_text.delta":
            print(payload.get("delta", ""), end="", flush=True)
        elif actual == "agent.session.turn.item.done":
            item = payload.get("item", {})
            # Plugin MCP names can include a plugin namespace. No other guide
            # tool is configured in this pilot, and source passages are required.
            if item.get("type") == "mcp_call":
                name = str(item.get("name", ""))
                server = str(item.get("server_label", ""))
                ok = (name.endswith("search_nova_guide")
                      and item.get("status") == "completed" and not item.get("error")
                      and setup.matching_passages(item.get("output")))
                print("\nMCP " + server + "/" + name + ": " +
                      ("verified source passages" if ok else "guide success not confirmed for this call"), flush=True)
                guide_passed = guide_passed or ok

    api.stream_followup_session(session_id,
        "According to the Nova Deployment Guide, where should I raise a Nova deployment support request, "
        "and what access is required first? Search the guide and cite the relevant sections. Do not access Jira.",
        guide_event, "")
    print()
    if not guide_passed:
        raise ValueError("Plugin guide retrieval did not return verified passages. The saved agent was not changed.")
    if any(t.get("server_label") == migration.JIRA_LABEL for t in overrides["tools"]):
        if not migration.check_turn(session_id, "List my accessible Jira sites and cloud IDs. Do not create or modify anything.",
                                    migration.JIRA_LABEL, "getAccessibleAtlassianResources", guide=False):
            raise ValueError("Jira read verification failed. The saved agent was not changed.")
    checkpoint["verification_passed"] = True
    setup.write_state(state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="Show the proposal without API calls.")
    mode.add_argument("--apply", action="store_true", help="Verify the plugin, then remove the explicit guide entry from the agent form.")
    parser.add_argument("--resume", action="store_true", help="Reuse a checkpointed plugin template.")
    parser.add_argument("--run-app", action="store_true", help="Start the local bot after successful verification and saving.")
    args = parser.parse_args(argv)
    state = setup.read_state()
    guide = state.get("hosted_guide_mcp", {})
    agent_id = setup.validate_id(state.get("agent_id") or guide.get("agent_id"), "agent_")
    store_id = setup.validate_id(guide.get("vector_store_id") or state.get("vector_store_id"), "vs_")
    project_id = setup.validate_id(guide.get("project_id") or state.get("project_id"), "proj_")
    guide_vault = setup.validate_id(guide.get("vault_id"), "vault_")
    guide_credential = setup.validate_id(guide.get("credential_id"), "credential_")
    body = template_body(store_id, project_id)
    if args.plan:
        print(json.dumps({"agent_id": agent_id, "template_name": TEMPLATE_NAME,
                          "guide_registration": "MCP inside environment.plugins, removed from agent.tools only after verification",
                          "preserved": "Saved instructions, model, Jira credential and permissions, vector store, search Vault key",
                          "guide_vault_id": guide_vault, "guide_credential_id": guide_credential,
                          "plugin_files": [".codex-plugin/plugin.json", ".mcp.json"],
                          "hosted_files": [f["path"] for f in body["files"]],
                          "checks": "Guide source passages, Jira read call, API save round trip; browser UI save still requires verification"}, indent=2))
        return 0
    if os.environ.get("OPENAI_PROJECT_ID", project_id) != project_id:
        raise ValueError("OPENAI_PROJECT_ID differs from the existing guide project.")
    os.environ["OPENAI_PROJECT_ID"] = project_id
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        os.environ["OPENAI_API_KEY"] = setup.hidden_value("Existing OpenAI project/setup API key (hidden): ")
    endpoint = "/agents/" + quote(agent_id, safe="")
    saved = migration.agent_fields(api.request_json("GET", endpoint))
    checkpoint = state.get("guide_plugin_setup")
    if checkpoint and not args.resume:
        raise ValueError("Plugin setup is checkpointed. Reuse it with --resume.")
    candidate = without_guide_entry(saved, already_applied=bool(checkpoint and checkpoint.get("applied")))
    credential = api.request_json("GET", "/vaults/" + guide_vault + "/credentials/" + guide_credential)
    auth = credential.get("auth", {})
    if (auth.get("type") != "environment_variable" or auth.get("secret_name") != guide_mcp.TOKEN_VARIABLE
            or auth.get("networking", {}).get("type") != "limited"
            or auth.get("networking", {}).get("allowed_hosts") != ["api.openai.com"]):
        raise ValueError("Unexpected guide Vault credential; no agent changes made.")
    store = api.request_json("GET", "/vector_stores/" + quote(store_id, safe=""))
    if store.get("status") != "completed" or store.get("file_counts", {}).get("completed", 0) < 1:
        raise ValueError("The existing guide store does not have a fully indexed file.")
    vault_ids = list(dict.fromkeys([*state.get("vault_ids", []), *app.agent_vault_ids(), guide_vault]))
    for tool in candidate["tools"]:
        if tool.get("server_label") == migration.JIRA_LABEL:
            vault_ids.append(migration.credential_vault(tool["credential_id"], vault_ids))
    vault_ids = list(dict.fromkeys(vault_ids))
    config_hash = migration.fingerprint({"template": body, "candidate": candidate, "vault_ids": vault_ids})
    if checkpoint and (checkpoint.get("agent_id") != agent_id or checkpoint.get("configuration_sha256") != config_hash):
        raise ValueError("Plugin setup configuration changed. Preserve the checkpoint and review the changes before retrying.")
    if not checkpoint:
        checkpoint = {"agent_id": agent_id, "project_id": project_id, "configuration_sha256": config_hash,
                      "previous_agent": saved, "previous_environment_template_id": state.get("environment_template_id")}
        state["guide_plugin_setup"] = checkpoint
        setup.write_state(state)
    template_id = checkpoint.get("environment_template_id")
    if not template_id:
        print("Creating the hosted guide plugin template; reusing the indexed guide and Vault key...", flush=True)
        template = setup.checkpoint_request(state, checkpoint, "plugin_template", "/agents/environments/templates", body)
        template_id = setup.validate_id(template.get("id"), "envtmpl_")
        checkpoint["environment_template_id"] = template_id
        setup.write_state(state)
    else:
        print("Reusing plugin environment template: " + template_id, flush=True)
    verify(candidate, agent_id, template_id, vault_ids, state, checkpoint)
    latest = migration.agent_fields(api.request_json("GET", endpoint))
    if migration.fingerprint(latest) != migration.fingerprint(saved):
        raise ValueError("The saved agent changed during verification. No update was applied; preserve those edits before resuming.")
    print("Read checks passed. Saving the guide registration in the environment and preserving the agent settings...", flush=True)
    api.request_json("POST", endpoint, candidate, extra_headers=["Idempotency-Key: " + uuid4().hex])
    checkpoint["applied"] = True
    state.update(environment_template_id=template_id, vault_ids=vault_ids)
    guide.update(transport="plugin_http", environment_template_id=template_id)
    setup.write_state(state)
    readback = migration.agent_fields(api.request_json("GET", endpoint))
    if any(t.get("server_label") == setup.SERVER_LABEL for t in readback["tools"]):
        raise ValueError("The explicit guide entry is still present after saving; inspect the saved agent.")
    api.request_json("POST", endpoint, readback, extra_headers=["Idempotency-Key: " + uuid4().hex])
    checkpoint["api_roundtrip_passed"] = True
    setup.write_state(state)
    print("PASS: hosted plugin returned guide passages, Jira read check passed when configured, and API save round trip passed.")
    print("Environment template: " + TEMPLATE_NAME + " (" + template_id + ")")
    print("Guide API secret: " + guide_credential + " in " + guide_vault)
    print("Refresh the agent editor, verify a UI save, then start a NEW hosted session using this plugin template and guide API secret.")
    print("The guide MCP is supplied by the environment plugin; only Atlassian appears in the agent form. No Jira ticket was created.")
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
        print("\nPlugin setup cancelled. Reuse checkpointed resources with --resume.")
        raise SystemExit(130)
    except (api.OpenAIRequestError, OSError, ValueError, KeyError, getpass.GetPassWarning) as exc:
        print("Plugin setup error: " + api._redact_runtime_secrets(str(exc))[:900], file=sys.stderr)
        print("Keep nova_state.json; retry checkpointed setup with --resume.", file=sys.stderr)
        raise SystemExit(1)
