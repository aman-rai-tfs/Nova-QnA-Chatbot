# Guide search inside an OpenAI-hosted session

## Platform editor route: register the guide MCP through its environment plugin

On 23 October 2026, a real edit and Save in the Platform editor reproduced
`mcp request_metadata requires a service-origin HTTP server`, even after guide
MCP was changed to environment-origin HTTP. The generated form included the
unsupported empty metadata map. The temporary name edit was restored. An API
save workaround therefore did not satisfy the requirement for colleagues to
edit and save the agent directly in the Platform UI.

The documented environment-plugin mechanism allows the MCP connection to be
registered in the hosted template instead of the agent form. The prepared
`setup_guide_plugin.py` uses this mechanism. Its live retrieval and browser-save
checks must pass before treating this route as verified.

The template **Nova guide MCP plugin** contains the same two Python files,
dependencies, proxy settings, guide store IDs, and startup command as the working
HTTP environment. It also contains a small inline ZIP with:

```text
nova-guide-search/
  .codex-plugin/plugin.json
  .mcp.json
```

The manifest registers `.mcp.json`, whose single server points to the guide's
loopback HTTP endpoint. No secret or document text is included in the plugin.
OpenAI extracts and registers it for each fresh environment. The existing
restricted search key remains in Vault and is attached as the guide API secret.

Run from the existing repository, retaining `nova_state.json`:

```powershell
Set-Location "$env:USERPROFILE\Documents\Nova-QnA-Chatbot"
python .\setup_guide_plugin.py --plan
python .\setup_guide_plugin.py --apply --run-app
```

The apply command uses the existing OpenAI project key or a hidden prompt. It
first creates a session with temporary overrides that expose no Jira write
tools. It requires source passages from the plugin's guide MCP and a successful
Jira-sites read before changing the saved agent. It then removes only the
explicit guide tool entry, preserving the current saved instructions, model,
Jira credential, and Jira tool permissions. All other agent settings remain
intact. The helper strips response-only null concurrency limits from API saves.

Only after PASS, refresh the Platform agent page and verify an edit/save/reload.
The agent form should show **Atlassian** as its explicit MCP connection. The
guide MCP remains available through **Nova guide MCP plugin** when starting a
new hosted session. Attach **Nova guide search API access** in API secrets.
Colleagues should select the exact template ID printed by setup. They do not
need the local web app or a running laptop to use the hosted session.

The local app reads the new default template and all relevant Vault IDs from
`nova_state.json`; click **New chat** after restarting it. Explicit environment
overrides still take precedence. Old sessions retain their previous setup.

If setup fails after checkpointing resources, retry with `--resume`. A failed
read check leaves the saved agent's guide entry intact. Previous agent settings
and the template ID are retained in ignored state for recovery. Do not rerun
the older stdio/explicit HTTP setup after applying the plugin route.

Reference: [Plugins in Agents API environments](https://developers.openai.com/api/docs/guides/agents-api/tools/plugins).

## Explicit HTTP entry (intermediate migration; UI Save still fails)

The Platform editor can add `request_metadata: {}` when saving a stdio MCP.
The API rejects that combination with `mcp request_metadata requires an HTTP
server`. Its fuller validation error also requires **service-origin** HTTP:
environment-origin HTTP rejects this field even when it is an empty map.
The migration below changes guide MCP to HTTP inside each sandbox and omits
unsupported metadata from session requests, agent updates, and read/save
round trips. This does not establish that the Platform editor's Save button
will preserve the corrected shape: it may reinsert the unsupported field.
It needs a one-time local setup command; subsequent guide and Jira requests
still run through the bot or Platform session.

The guide URL is `http://127.0.0.1:8765/mcp`, with
`connection_origin: environment`. It refers to the session's sandbox, not your
laptop. The server binds only to loopback. Atlassian keeps its remote HTTPS URL
and `connection_origin: service`. Do not switch these origins in the editor.

The new template **Nova guide MCP HTTP** contains `guide_mcp.py` and
`start_guide_http.py` as inline base64 files. It installs curl, creates a Python
virtual environment, installs `mcp==1.26.0`, then starts the HTTP server in the
background and checks its health before setup finishes. The server inherits
the Vault placeholder and the sandbox's proxy/certificate variables. It uses
the same vector store, search implementation, eight-result maximum, and
host-limited Vault search key. No new key or guide upload is needed.

Keep your unsaved Platform draft open until this command passes. Put the
generated agent JSON object (the content of curl's `-d`, without curl/shell
escaping) in ignored `work/agent-unsaved-draft.json`. Keep credential IDs;
remove actual key/token values. This preserves the draft's name, model,
instructions and credential selection. The personal draft supplied in this
chat has already been saved locally at that path; it is excluded from GitHub.

Stop the local app with **Ctrl+C**, then run:

```powershell
Set-Location "$env:USERPROFILE\Documents\Nova-QnA-Chatbot"
git pull origin main
python .\migrate_guide_http.py --plan --draft-json .\work\agent-unsaved-draft.json --enable-jira-create
python .\migrate_guide_http.py --apply --draft-json .\work\agent-unsaved-draft.json --enable-jira-create --run-app
```

The apply command reads your existing project key from `OPENAI_API_KEY`, or
prompts for it without displaying it. It reuses the guide credential in Vault.
It locates the draft Jira credential's Vault from resource metadata and includes
both Vaults for API-created sessions. This requires Vault list/read permissions.
It does not read or print stored secret values.

Before changing the saved agent it creates a session with temporary agent
overrides. Only guide search and Jira-site read tools are exposed during the
checks. It requires a completed guide search containing source passages and,
when Jira is configured, a completed live Jira-sites read. The saved agent and
local default template stay unchanged if either check fails. New templates and
verification sessions may have been created and their IDs are checkpointed.

`--enable-jira-create` changes the copied draft's Jira policy to allow only
new-issue creation after explicit approval of a complete draft. The allowlist
contains Jira read tools plus `createJiraIssue`; Confluence writes, Jira edits,
transitions/comments, and generic write/destructive tools are excluded. This
flag does not grant Atlassian permissions or prove that the token has write
scope. Actual Jira creation is not executed by the migration.

After read checks pass, the helper saves the agent and checks an API
retrieve/save round trip, stripping unsupported empty metadata from response
defaults. It records the old agent definition
and template ID in ignored `nova_state.json` for recovery. Existing sessions
keep their original configuration. The original stdio helper refuses to
overwrite an applied HTTP setup.

If interrupted after a resource was checkpointed, retry the same apply command
with `--resume`. The existing HTTP template is reused; a new read-only
verification session is run. Keep the draft and script version unchanged.

For the first migration version's `request_metadata requires a service-origin
HTTP server` error, the corrected helper recognizes that exact checkpoint
configuration and removes only the rejected metadata field. Use `--resume`;
do not delete `nova_state.json`, the saved draft, or the created template.

Only after PASS:

1. Refresh the old agent draft page to load the preserved, migrated definition.
2. Check **Save agent definition** once. This is browser verification, not a
   guaranteed result. If the same metadata error returns, preserve the draft
   and use the corrected API helper for saves. A public, authenticated
   service-origin HTTP deployment would require separate hosting; changing the
   loopback connection's origin to `service` will not work.
3. Start a **new OpenAI-hosted** session with **Nova guide MCP HTTP**, matching
   the template ID printed by migration. Attach **Nova guide search API access**
   in API secrets and retain the saved Atlassian connection.
4. Ask a guide question, then **Which Jira projects can I access?** Verify both
   completed tool calls in the trace. In the local app click **New chat**.
5. To prepare a ticket, describe the request to the bot. Review its complete
   draft and confirm only when you want that real ticket created.

The background server's lifetime and Vault configuration must pass the live
hosted retrieval check. Preparation of these files alone is not a deployment
or a passing Platform UI save check.

## Original stdio setup (existing installations)

This setup gives new sessions a `search_nova_guide` MCP tool that runs inside
their OpenAI-hosted sandbox. It searches the existing OpenAI vector store. It
does not upload the DOCX again, create another vector store, or use GitHub as a
document source.

## Why the earlier results differed

| Session | What executed guide search | Observed result |
| --- | --- | --- |
| Earlier local chatbot, no sandbox | The running Python app received function calls and returned vector-store passages | Guide answers were available |
| Later Platform session, hosted sandbox, original function tool | No application handler was attached to that separately created session | The session waited for a function result |
| Hosted session with this new MCP setup | The sandbox starts the guide MCP process, which returns passages directly | Requires a successful live verification after setup |

The sandbox did not disable guide search. The missing function handler caused
the waiting. Separately, the earlier no-sandbox Jira attempt failed with a
generic internal error, while a hosted Platform session completed Jira calls.
That does not establish that Atlassian MCP inherently requires a sandbox:
service-origin HTTP MCP is documented to work without one. This guide MCP uses
stdio and therefore does require an execution environment.

## Files and where they run

- `guide_mcp.py`: the read-only search implementation and its MCP stdio wrapper.
  It is included in a reusable hosted template at
  `/workspace/nova-guide/guide_mcp.py`. Its only API operation is a fixed
  vector-store search request using curl.
- `setup_guide_mcp.py`: runs once from your local repository. It provisions the
  template and Vault credential, replaces only the existing guide function with
  the guide MCP entry, and can run a live retrieval check. The laptop needs only
  Python and curl; `mcp==1.26.0` is installed in the hosted sandbox.
- `app.py` and `index.html`: remain the local/future NOVA chatbot UI. They are not
  needed to handle guide calls from a correctly configured Platform session.

The agent's model, instructions, and other tools, including Atlassian MCP, are
preserved. Setup appends a guide-retrieval failure rule so a failed tool is not
misrepresented as missing information in the guide. The existing `env` template is preserved; setup creates a separate
template named **Nova guide MCP**. New sessions must select that template.

## Credentials

Two different keys have different purposes:

1. **Setup key**: your OpenAI project API key, read from `OPENAI_API_KEY` or a
   hidden prompt. It needs agent read/write, environment-template read/write,
   Vault read/write, and vector-store read access. Live verification also needs
   session access and inference permissions.
2. **Search key**: create a separate restricted key in the same OpenAI project
   with the vector-store read/search permissions required to search the guide.
   It must not have agent or Vault administration permissions. The setup checks
   whether this key can search the existing store before changing the agent.

Enter keys only into the hidden terminal prompts. Never paste them into chat,
agent instructions, source files, GitHub, or command-line arguments. The helper
does not reuse the setup key as the sandbox search credential.

The search key is stored as a Vault `environment_variable` credential named
`NOVA_GUIDE_SEARCH_TOKEN`, with secret substitution restricted to
`api.openai.com`. The sandbox receives a placeholder. Curl passes that
placeholder unchanged in its Authorization header; OpenAI's network proxy
inserts the real key outside the sandbox.

The guide credential is added to the existing Jira OAuth Vault when available,
which avoids replacing the Jira connection. Otherwise setup creates a guide
Vault. Every hosted session must have the guide credential's Vault attached.
The local app reads the resulting `vault_ids` and template ID from
`nova_state.json`. Explicit environment-variable overrides take precedence.

Hosted stdio MCP requires `network.access: enabled` under the current API. The
new template uses that setting; the credential's separate allowed-host policy
limits where the search secret can be substituted. No secret is included in the
template's files, setup commands, or environment values.

## Setup in PowerShell

Use project **DEV-CRG-dev-asd-microtransformation**
(`proj_dXDjYYmzfErZmSza7mHcNP0y`) when creating the restricted search key on the
OpenAI Platform. Keep the existing `nova_state.json`; it identifies the already
indexed guide and the saved agent.

In that project's **API Keys** page, create a separate secret key named
**Nova guide MCP search**. Choose restricted permissions and grant only the
vector-store access needed to read/search the indexed guide. Keep agent,
session, and Vault administration disabled for this search key. Copy it once
into the setup script's restricted-key prompt below; do not put it in an agent
message. The script checks search access and reports an HTTP status if the key's
permissions are insufficient.

From the repository, first review the proposed configuration without making API
calls or changing any resource:

```powershell
Set-Location "$env:USERPROFILE\Documents\Nova-QnA-Chatbot"
python .\setup_guide_mcp.py --plan
```

Then provision and verify the hosted guide tool:

```powershell
python .\setup_guide_mcp.py --apply --verify
```

If `OPENAI_API_KEY` is not set, enter the setup key at its hidden prompt. Enter
the separate restricted search key when prompted. Only resource IDs and
idempotency keys are checkpointed in ignored `nova_state.json`.

Setup prints the new template and Vault IDs plus a Platform session link.
Configuration saved is not equivalent to a passing retrieval check. Success is:

```text
Guide MCP: completed with source passages
PASS: the hosted guide MCP returned indexed source passages.
```

If a later stage fails, reuse the saved resources without recreating the
credential or signing in to Atlassian again:

```powershell
python .\setup_guide_mcp.py --apply --resume --verify
```

### Refresh after updating the hosted server code

If setup has already saved a template and the server code changes, retain the
existing Vault credential and prepare a fresh template with:

```powershell
python .\setup_guide_mcp.py --apply --resume --refresh-template --verify
```

The previous template is preserved, and its ID is recorded in the checkpoint.
This does not require another restricted search key or Atlassian sign-in.
Select the newly printed template ID for new Platform sessions; older templates
with the same display name may still appear in the picker.

The first hosted retrieval attempt reached the guide MCP server but its curl
request failed with status 7. The stdio process originally inherited only its
three guide variables. The revised MCP configuration also inherits the
sandbox's proxy and certificate environment variables, and connection errors
report their presence plus a sanitized curl diagnostic. This is a networking
repair to verify live, rather than evidence that the document lacks the answer.

If the process stopped before the credential was checkpointed, the helper may
ask for the same restricted search key again. Checkpointed resource-creation
requests reuse their saved idempotency keys.

To run the local chatbot after a successful hosted verification:

```powershell
python .\setup_guide_mcp.py --apply --resume --verify --run-app
```

This starts the app in the same process with the new template and all configured
Vault IDs. Open `http://localhost:8080` and click **New chat**. An already-running
server or old chat does not acquire the new tool configuration automatically.

## Start a session directly on the Platform

1. Open **Nova agent - manual setup** in the same project.
2. Start a **new** session with **OpenAI-hosted** selected.
3. Select **Nova guide MCP**, rather than the older **env** template.
4. Ensure the printed guide Vault is attached. If the saved Jira connection
   does not attach it automatically, select **Nova guide search API access** in
   the session's API secrets picker. The guide token is an API-request secret,
   not an MCP Bearer/OAuth credential.
5. Ask naturally: **Where should I raise a Nova deployment support request, and
   what access is required first? Cite the guide sections.**
6. Inspect the trace for a completed `nova-guide-search` / `search_nova_guide`
   MCP call with returned source passages, followed by the cited answer.
7. Ask **Which Jira projects can I access?** to verify the existing Atlassian
   connection still provides live Jira data. Jira access remains read-only.

The old session waiting for an application function retains its old
configuration. Start a new session after this migration rather than waiting for
the old function to turn into an MCP call.

## References

- [Agents API MCP connections](https://developers.openai.com/api/docs/guides/agents-api/tools/mcp)
- [Hosted environments](https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted)
- [Vault secrets for sandbox API requests](https://developers.openai.com/api/docs/guides/agents-api/tools/vaults)
- [Function handlers](https://developers.openai.com/api/docs/guides/agents-api/tools/functions)
