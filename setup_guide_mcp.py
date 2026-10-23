"""Install guide-search MCP in a reusable OpenAI-hosted environment.

All OpenAI HTTP requests use the existing curl client. Only resource IDs and
idempotency keys are checkpointed locally. Search credentials stay in Vault.
"""

from __future__ import annotations

import argparse
import base64
import copy
import getpass
import hashlib
import json
import os
import re
import sys
import warnings
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import app
import guide_mcp
import openai_http as api

SERVER_LABEL = "nova-guide-search"
TEMPLATE_NAME = "Nova guide MCP"
SERVER_PATH = "/workspace/nova-guide/guide_mcp.py"
PYTHON_PATH = "/workspace/nova-guide/.venv/bin/python"
DEFAULT_AGENT = "agent_791112d1d9c94517b1911ec1e9017f622f3f56e7468c4e6989"
RETRIEVAL_FAILURE_RULE = """## Guide retrieval failures

Use the fixed \"The guide does not specify\" response only after guide search
has completed successfully and the returned passages do not answer the question.
If search fails, times out, or returns a tool error, say that guide retrieval
failed and report the available error briefly. Do not claim that the guide lacks
the information, do not invent an answer, and do not repeatedly announce waiting.
"""


def read_state() -> dict:
    state = json.loads(app.STATE_PATH.read_text(encoding="utf-8")) if app.STATE_PATH.is_file() else {}
    if not isinstance(state, dict):
        raise ValueError("nova_state.json must contain an object.")
    return state


def write_state(state: dict) -> None:
    temporary = app.STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(app.STATE_PATH)


def hidden_value(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        value = getpass.getpass(prompt).strip()
    if not value or any(character in value for character in ("\r", "\n", "\0")):
        raise ValueError("A non-empty API key without control characters is required.")
    api.register_runtime_secrets(value)
    return value


def mcp_tool() -> dict:
    return {
        "type": "mcp", "server_label": SERVER_LABEL,
        "transport": {"type": "stdio", "command": PYTHON_PATH,
                      "args": [SERVER_PATH], "cwd": "/workspace/nova-guide",
                      "env_vars": [guide_mcp.TOKEN_VARIABLE, guide_mcp.STORE_VARIABLE,
                                   guide_mcp.PROJECT_VARIABLE, *guide_mcp.NETWORK_ENV_VARIABLES]},
        "allowed_tools": ["search_nova_guide"], "required": True,
    }


def replace_guide_tool(saved: dict) -> list:
    tools = saved.get("tools")
    if not isinstance(tools, list):
        raise ValueError("The saved agent did not return a tools list.")
    updated = []
    replaced = 0
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("The agent returned an invalid tool definition.")
        is_guide = (tool.get("type") == "function" and tool.get("name") == "search_nova_guide") or (
            tool.get("type") == "mcp" and tool.get("server_label") == SERVER_LABEL)
        if is_guide:
            replaced += 1
        else:
            updated.append(copy.deepcopy(tool))
    if replaced != 1:
        raise ValueError("Expected exactly one existing search_nova_guide function or Nova guide MCP entry.")
    updated.append(mcp_tool())
    return updated


def template_body(store_id: str, project_id: str) -> dict:
    source = Path(guide_mcp.__file__).read_bytes()
    return {
        "name": TEMPLATE_NAME,
        "files": [{"type": "inline", "path": SERVER_PATH,
                   "data": base64.b64encode(source).decode("ascii")}],
        "packages": {"system": ["curl"]},
        "setup_commands": [{"command": "python3 -m venv /workspace/nova-guide/.venv"},
                           {"command": PYTHON_PATH + " -m pip install --disable-pip-version-check 'mcp==1.26.0'"}],
        # Hosted stdio MCP currently requires enabled network access. The Vault
        # credential independently restricts secret substitution to api.openai.com.
        "network": {"access": "enabled"},
        "env": {guide_mcp.STORE_VARIABLE: store_id, guide_mcp.PROJECT_VARIABLE: project_id},
    }


def checkpoint_request(state: dict, checkpoint: dict, stage: str, endpoint: str, body: dict) -> dict:
    request_key = checkpoint.setdefault("request_keys", {}).setdefault(stage, uuid4().hex)
    write_state(state)  # Persist the idempotency key before a resource can be created.
    return api.request_json("POST", endpoint, body, extra_headers=["Idempotency-Key: " + request_key])


def validate_id(value: str, prefix: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]+", value):
        raise ValueError("Expected a valid " + prefix + " ID.")
    return value


def matching_passages(value) -> bool:
    if isinstance(value, str):
        try:
            return matching_passages(json.loads(value))
        except json.JSONDecodeError:
            return False
    if isinstance(value, list):
        return any(matching_passages(item) for item in value)
    if isinstance(value, dict):
        if value.get("isError") or value.get("is_error") or value.get("error"):
            return False
        matches = value.get("matches")
        if isinstance(matches, list) and any(isinstance(item, dict) and item.get("text") for item in matches):
            return True
        return any(matching_passages(item) for item in value.values())
    return False


def verify_session(agent_id: str, template_id: str, vault_ids: list[str], state: dict) -> bool:
    successful = []
    session_id = ""

    def event(kind: str, payload: dict) -> None:
        nonlocal session_id
        session_id = session_id or api.session_id_from_event(payload) or ""
        if session_id and state["hosted_guide_mcp"].get("verification_session_id") != session_id:
            state["hosted_guide_mcp"]["verification_session_id"] = session_id
            write_state(state)
        actual = payload.get("type", kind)
        if actual == "app.session":
            print("Session created. Waiting for its hosted environment...", flush=True)
        elif actual == "app.environment":
            print("Hosted environment: " + payload.get("status", "unknown"), flush=True)
        elif actual == "agent.session.turn.output_text.delta":
            print(payload.get("delta", ""), end="", flush=True)
        elif actual == "agent.session.turn.item.done":
            item = payload.get("item", {})
            if item.get("type") == "mcp_call" and item.get("server_label") == SERVER_LABEL:
                ok = (item.get("name") == "search_nova_guide" and item.get("status") == "completed"
                      and not item.get("error") and matching_passages(item.get("output")))
                print("\nGuide MCP: " + ("completed with source passages" if ok else "success not confirmed"), flush=True)
                if ok:
                    successful.append(item.get("id"))

    try:
        api.stream_initial_session(
            agent_id,
            "According to the Nova Deployment Guide, where should I raise a Nova deployment support request "
            "and what access is required first? Search the guide and cite its relevant sections. "
            "Do not access or modify Jira for this guide retrieval check.",
            event, "", vault_ids=vault_ids,
            environment={"type": "openai_hosted", "environment_template_id": template_id},
        )
    finally:
        print()
        if session_id:
            project_id = state["hosted_guide_mcp"]["project_id"]
            print("Platform session: https://platform.openai.com/agents/" + agent_id + "/sessions/" + session_id
                  + "?project_id=" + project_id, flush=True)
    if successful:
        print("PASS: the hosted guide MCP returned indexed source passages. Jira writes were not tested.")
        return True
    print("No successful guide MCP call with source passages was confirmed. Inspect the linked session trace.")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="Print the proposed configuration without API calls or changes.")
    mode.add_argument("--apply", action="store_true", help="Create the hosted setup and replace only the guide tool.")
    parser.add_argument("--agent-id", default=os.environ.get("OPENAI_AGENT_ID", ""))
    parser.add_argument("--vector-store-id", default=os.environ.get("OPENAI_VECTOR_STORE_ID", ""))
    parser.add_argument("--vault-id", help="Existing Vault for the guide credential; defaults to the existing Jira Vault when configured.")
    parser.add_argument("--resume", action="store_true", help="Reuse checkpointed resources; no new guide key is needed if its credential was saved.")
    parser.add_argument("--refresh-template", action="store_true", help="With --resume, install updated server code in a fresh template while keeping the saved credential.")
    parser.add_argument("--verify", action="store_true", help="Run one live, read-only guide MCP retrieval check after setup.")
    parser.add_argument("--run-app", action="store_true", help="Start the local chatbot after --verify succeeds.")
    args = parser.parse_args(argv)
    if args.run_app and not args.verify:
        parser.error("--run-app requires --verify.")
    if args.refresh_template and not args.resume:
        parser.error("--refresh-template requires --resume.")
    state = read_state()
    if state.get("hosted_guide_mcp", {}).get("transport") in ("http", "plugin_http"):
        raise ValueError("This agent uses HTTP guide MCP. Use migrate_guide_http.py rather than reverting it to stdio.")
    agent_id = validate_id(args.agent_id or state.get("agent_id") or DEFAULT_AGENT, "agent_")
    store_id = validate_id(args.vector_store_id or state.get("vector_store_id", ""), "vs_")
    project_id = validate_id(os.environ.get("OPENAI_PROJECT_ID", api.DEFAULT_PROJECT_ID), "proj_")
    body = template_body(store_id, project_id)
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
    if args.plan:
        print(json.dumps({"agent_id": agent_id, "vector_store_id": store_id, "project_id": project_id,
                          "template_name": TEMPLATE_NAME, "guide_tool": mcp_tool(),
                          "credential_type": "environment_variable", "secret_name": guide_mcp.TOKEN_VARIABLE,
                          "credential_allowed_hosts": ["api.openai.com"],
                          "network": body["network"], "preserve": "Existing instructions, model, and other tools including Atlassian MCP"}, indent=2))
        return 0
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        os.environ["OPENAI_API_KEY"] = hidden_value("OpenAI setup/project API key (hidden): ")
    print("Checking the saved agent and existing guide vector store...", flush=True)
    saved = api.request_json("GET", "/agents/" + quote(agent_id, safe=""))
    tools = replace_guide_tool(saved)
    store = api.request_json("GET", "/vector_stores/" + quote(store_id, safe=""))
    if store.get("status") != "completed":
        raise ValueError("The guide vector store is not fully indexed. Finish indexing before installing MCP.")
    if store.get("file_counts", {}).get("completed", 0) < 1:
        raise ValueError("The vector store does not contain a completed guide file.")
    checkpoint = state.get("hosted_guide_mcp")
    if checkpoint and not args.resume:
        raise ValueError("A hosted guide setup is already checkpointed. Use --resume to reuse it.")
    if checkpoint is not None and not isinstance(checkpoint, dict):
        raise ValueError("Invalid hosted_guide_mcp checkpoint.")
    if checkpoint:
        if any(checkpoint.get(key) != value for key, value in
               (("agent_id", agent_id), ("vector_store_id", store_id), ("project_id", project_id))):
            raise ValueError("The checkpoint targets different resources. Keep the existing setup and review the configuration before replacing it.")
        if checkpoint.get("configuration_sha256") != fingerprint and not args.refresh_template:
            raise ValueError("The guide server code changed. Use --resume --refresh-template to retain its Vault credential and prepare a new template.")
    else:
        checkpoint = {"agent_id": agent_id, "vector_store_id": store_id, "project_id": project_id,
                      "configuration_sha256": fingerprint,
                      "previous_environment_template_id": state.get("environment_template_id", "")}
        state["hosted_guide_mcp"] = checkpoint
        write_state(state)
    configured_vaults = app.agent_vault_ids()
    jira_checkpoint = state.get("atlassian_oauth", {})
    if not isinstance(jira_checkpoint, dict):
        raise ValueError("The existing Atlassian OAuth checkpoint must be an object.")
    vault_id = args.vault_id or checkpoint.get("vault_id") or jira_checkpoint.get("vault_id")
    if checkpoint.get("vault_id") and args.vault_id and checkpoint["vault_id"] != args.vault_id:
        raise ValueError("--vault-id does not match the saved guide setup.")
    if vault_id:
        validate_id(vault_id, "vault_")
        api.request_json("GET", "/vaults/" + quote(vault_id, safe=""))
    else:
        vault = checkpoint_request(state, checkpoint, "vault", "/vaults", {"name": "Nova guide search"})
        vault_id = validate_id(vault["id"], "vault_")
    checkpoint["vault_id"] = vault_id
    write_state(state)
    credential_id = checkpoint.get("credential_id")
    if credential_id:
        credential = api.request_json("GET", "/vaults/" + quote(vault_id, safe="")
                                      + "/credentials/" + quote(credential_id, safe=""))
        auth = credential.get("auth", {})
        if (auth.get("type") != "environment_variable" or auth.get("secret_name") != guide_mcp.TOKEN_VARIABLE
                or auth.get("networking", {}).get("type") != "limited"
                or auth.get("networking", {}).get("allowed_hosts") != ["api.openai.com"]):
            raise ValueError("The checkpoint does not reference the expected host-limited guide credential.")
    else:
        print("Provide a separate OpenAI key restricted to the vector-store read/search permissions needed in this project.", flush=True)
        print("It will be stored in Vault; the sandbox receives a placeholder, not the real key.", flush=True)
        search_key = os.environ.get("NOVA_GUIDE_SEARCH_API_KEY", "").strip() or hidden_value("Restricted guide-search API key (hidden): ")
        api.register_runtime_secrets(search_key)
        if search_key == os.environ.get("OPENAI_API_KEY", "").strip():
            raise ValueError("Use a separate restricted search key, rather than the setup key that can modify agents and Vaults.")
        # Validate search access before persisting a credential or changing the agent.
        print("Checking the restricted key's guide-search access...", flush=True)
        result = guide_mcp.search_vector_store("Nova deployment support access", store_id, project_id, search_key)
        if not result["matches"]:
            raise ValueError("Search returned no guide passages. Confirm the selected vector store contains the Nova guide.")
        print("Saving the host-limited search credential in Vault...", flush=True)
        credential = checkpoint_request(state, checkpoint, "credential", "/vaults/" + quote(vault_id, safe="") + "/credentials", {
            "name": "Nova guide search API access",
            "auth": {"type": "environment_variable", "secret_name": guide_mcp.TOKEN_VARIABLE,
                     "secret_value": search_key, "networking": {"type": "limited", "allowed_hosts": ["api.openai.com"]}},
        })
        credential_id = validate_id(credential["id"], "credential_")
        checkpoint["credential_id"] = credential_id
        write_state(state)
        del search_key
    template_id = checkpoint.get("environment_template_id")
    refresh_needed = checkpoint.get("configuration_sha256") != fingerprint
    if template_id and not refresh_needed:
        api.request_json("GET", "/agents/environments/templates/" + quote(validate_id(template_id, "envtmpl_"), safe=""))
    else:
        print("Creating the reusable Nova guide MCP environment template...", flush=True)
        # A new source version gets its own idempotency key, leaving the old
        # template intact. Interrupted refreshes can resume the same request.
        template_stage = "template:" + fingerprint if refresh_needed else "template"
        template = checkpoint_request(state, checkpoint, template_stage, "/agents/environments/templates", body)
        if template_id:
            checkpoint.setdefault("previous_templates", []).append(template_id)
        template_id = validate_id(template["id"], "envtmpl_")
        checkpoint["environment_template_id"] = template_id
        checkpoint["configuration_sha256"] = fingerprint
        write_state(state)
    # Fetch again so another saved edit made during setup is preserved.
    saved = api.request_json("GET", "/agents/" + quote(agent_id, safe=""))
    tools = replace_guide_tool(saved)
    instructions = saved.get("instructions", "")
    if not isinstance(instructions, str):
        raise ValueError("The saved agent instructions are not text; stopped before replacing them.")
    updated_instructions = instructions
    if "## Guide retrieval failures" not in instructions:
        updated_instructions = instructions.rstrip() + "\n\n" + RETRIEVAL_FAILURE_RULE
    if tools != saved.get("tools") or updated_instructions != instructions:
        print("Replacing only the saved agent's guide function with hosted MCP...", flush=True)
        api.request_json("POST", "/agents/" + quote(agent_id, safe=""), {"tools": tools, "instructions": updated_instructions},
                         extra_headers=["Idempotency-Key: " + uuid4().hex])
    vault_ids = list(dict.fromkeys([*state.get("vault_ids", []), *configured_vaults, vault_id]))
    state.update(agent_id=agent_id, environment_template_id=template_id, vault_ids=vault_ids)
    write_state(state)
    os.environ.update(OPENAI_AGENT_ID=agent_id, OPENAI_VECTOR_STORE_ID=store_id,
                      OPENAI_ENVIRONMENT_TEMPLATE_ID=template_id, OPENAI_VAULT_IDS=json.dumps(vault_ids))
    print("Hosted guide MCP configuration saved. Live retrieval is not confirmed until verification passes.")
    print("Environment template: " + TEMPLATE_NAME + " (" + template_id + ")")
    print("Guide credential Vault: " + vault_id)
    print("Guide credential: " + credential_id)
    print("For Platform sessions, select this template and make this Vault/API secret available to the session.")
    print("Existing sessions retain the old function; start a new session for the MCP tool.")
    if args.verify and not verify_session(agent_id, template_id, vault_ids, state):
        return 1
    if args.run_app:
        os.environ.setdefault("HOST", "127.0.0.1")
        app.main([])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled. Saved resource IDs can be reused with --resume.")
        raise SystemExit(130)
    except (api.OpenAIRequestError, guide_mcp.GuideSearchError, OSError, ValueError, KeyError, getpass.GetPassWarning) as exc:
        print("Setup error: " + api._redact_runtime_secrets(str(exc))[:800], file=sys.stderr)
        print("If resources were checkpointed, retry the same command with --resume.", file=sys.stderr)
        raise SystemExit(1)
