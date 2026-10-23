# Guide search inside an OpenAI-hosted session

This setup gives new sessions a `search_nova_guide` MCP tool that runs inside
their OpenAI-hosted sandbox. It searches the existing OpenAI vector store. It
does not upload the DOCX again, create another vector store, or use GitHub as a
document source.

Live guide retrieval passed on 23 October 2026 in session
`sess_0672152a62823835006adb061326ec819c892fc288af05bb0d`. The MCP returned indexed
source passages and the agent answered with section references. The verified
template is `envtmpl_9a354d8d8c264027825dd0ed1843bcbdbae6c6f3db5642079b`.
This verification did not test Jira writes or deploy the chatbot on NOVA.

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
report their presence plus a sanitized curl diagnostic. Live retrieval passed
after this networking repair. A failed retrieval is an operational error and
does not establish that the document lacks the requested information.

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
