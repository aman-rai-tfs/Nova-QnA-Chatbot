# Nova Deployment Guide Assistant

A small, runnable web chat app grounded only in the Nova Deployment Guide. It saves a reusable agent and guide vector store in the selected OpenAI project, then serves a browser UI that streams Agents API session events. The API key stays on the server and is never sent to the browser.

## What this repository contains

- `provision.py` uploads and indexes the source DOCX, creates the reusable **Nova Deployment Guide Assistant**, then starts a session with an initial user message and prints the streamed events.
- `app.py` is a dependency-free Python HTTP app for chat, health checks, and streamed session events.
- `openai_http.py` calls the OpenAI HTTP API through `curl` / `curl.exe`; it contains no credentials.
- `runtime_logging.py` provides console logs, request correlation, event/tool statistics, timing, and optional rotating log files.
- `guide_mcp.py` and `start_guide_http.py` provide guide search over HTTP inside an OpenAI-hosted sandbox. `setup_guide_plugin.py` packages its MCP connection into the environment, avoiding the Platform editor's unsupported metadata on explicit environment MCP entries. `migrate_guide_http.py` provisions the explicit HTTP entry; `setup_guide_mcp.py` provisions legacy stdio. See [hosted guide MCP setup](HOSTED_GUIDE_MCP.md) for commands and credential configuration.
- `index.html` is the browser chat UI.
- `Dockerfile` packages the runtime for a container-based platform such as NOVA.

The deployment guide itself is not part of this repository. Provision it from the approved source file path. Do not put it in GitHub unless its owner has separately approved that destination.

## Create the OpenAI agent and vector store once

Requirements: Python 3.10+, `curl` in `PATH`, and an OpenAI project API key with access to project `proj_dXDjYYmzfErZmSza7mHcNP0y`. For a restricted key, provisioning needs file and vector-store read/write access plus agent read/write and response write access. The running chat app needs vector-store read, agent read/write, and response write access.

From this repository in PowerShell, enter the key without echoing it, then run provisioning. The default document path is the guide in your Downloads folder.

```powershell
$secureKey = Read-Host "OpenAI project API key" -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try { $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer); Remove-Variable secureKey, keyPointer }

python .\provision.py
```

If the DOCX is elsewhere, provide its path:

```powershell
python .\provision.py --guide "C:\path\to\Nova_Deployment_Guide_Updated 1.docx"
```

Provisioning creates a vector store, uploads and indexes the DOCX, creates the reusable agent with a `search_nova_guide` function, then creates a session using the returned agent ID. The Agents API agent tool list does not accept the Responses API `file_search` tool directly. Instead, the application handles the function call by searching the indexed vector store and returning matching guide passages to the agent. Its initial user message is “What does the guide say about getting started with a Nova deployment? Cite the relevant section.” Override it with `--initial-question "..."`.

Provisioning writes resource IDs to ignored local file `nova_state.json`, checkpoints the vector store and indexed file before creating the agent, and reuses an indexed copy from a previous interrupted run when it can identify one safely. The file contains no API key. Keep it out of source control.

## Run the web chat locally

In the same PowerShell window where `OPENAI_API_KEY` is set:

```powershell
python .\app.py
```

Open [http://localhost:8080](http://localhost:8080). The first message creates a new session from `OPENAI_AGENT_ID` (or the local ID in `nova_state.json`); follow-up messages continue that browser’s session. A new chat starts another session. The browser stores only the session ID in local storage.

If running the web app elsewhere, set `OPENAI_API_KEY`, `OPENAI_PROJECT_ID`, `OPENAI_AGENT_ID`, and `OPENAI_VECTOR_STORE_ID` in that process’s environment. The project ID defaults to the Nova project above. `PORT` defaults to `8080`; `HOST` defaults to `0.0.0.0`. The health endpoint is `GET /api/health`.

## See backend progress in PowerShell

The app prints timestamped logs to PowerShell by default. Stop the old process
with **Ctrl+C**, update the code, then start it again in the window containing
your existing `OPENAI_API_KEY`:

```powershell
Set-Location "$env:USERPROFILE\Documents\Nova-QnA-Chatbot"
git pull origin main
python .\app.py
```

No guide re-upload, OAuth sign-in, or hosted environment refresh is needed for
this logging change. Click **New chat** when you want a session using the latest
saved agent configuration. Logs are produced by this running web app; questions
sent directly on the OpenAI Platform do not pass through your local PowerShell.

Each accepted request gets a `request_id`; session and turn IDs are added as
OpenAI returns them. The response includes `X-Request-ID` for troubleshooting.
Interleaved requests can be followed using that ID.

| Log step | Information recorded |
| --- | --- |
| `server.started`, `server.configuration` | Address, project/agent/vector-store IDs, template ID, Vault count, whether the app key is configured |
| `request.received`, `request.validated` | Request ID, route, request size, message character count, new/existing session, selected configuration |
| `session.creating`, `session.created` | Agent and session association, environment choice, credential Vault count |
| `environment.wait_started`, `environment.state`, `environment.ready` | Provisioning state, time waiting, configured timeout, environment ID |
| `api.request_started`, `api.request_finished`, `api.request_failed` | HTTP method/endpoint, status when returned by curl, response bytes, curl exit code, duration; polling GET details require DEBUG |
| `stream.connecting`, `stream.connected`, `input.submitting`, `input.accepted` | Stream connection time, HTTP status, OpenAI request ID when provided, input submission/acceptance |
| `turn.state`, `tool.started`, `tool.finished` | Turn lifecycle, tool name/server/item ID, tool status, observed duration when both events arrive, reported failures |
| Guide tool result | Returned passage count, passage character count, score range, result characters/UTF-8 bytes, when present in the streamed result |
| `response.first_text` | Time from receipt of the browser request to its first text event; this includes setup and tool waits |
| `turn.finished` | Completion/failure/cancellation, provider duration when timestamps exist, input/output/total/cached/reasoning tokens when reported |
| `request.progress` | Every 15 seconds: current phase, elapsed time, time since last event, event/tool/output counts |
| `request.finished` | Request outcome, HTTP status, total elapsed time, first-text time, output character count, event/tool/failure counts, unfinished items |
| Errors and shutdown | Validation errors, stream/API failures, client disconnects, unexpected error type, clean Ctrl+C shutdown |

For the older application function path, `function.executing`,
`guide.search_started`, `guide.search_finished`, and `function.result_submitted`
also describe local guide execution. `tool.arguments_ready` means the model has
finished constructing a function call; execution is logged separately.

Enable additional metadata, including polling HTTP calls and output-item
lifecycle, and optionally save the same logs to a rotating file:

```powershell
$env:NOVA_LOG_LEVEL = "DEBUG"
$env:NOVA_LOG_FILE = ".\logs\nova-chatbot.log"
$env:NOVA_LOG_PROGRESS_SECONDS = "15"
python .\app.py
```

Supported levels: `DEBUG`, `INFO` (default), `WARNING`, `ERROR`. Set progress
seconds to `0` to disable periodic progress. Console logs are immediate; optional
UTF-8 files rotate at 5 MiB with three backups. `logs/` and `*.log*` are excluded
from Git and the container build context. File logging is off by default.
For NOVA, console output can be collected by the platform's container logging.

Logs record metadata rather than user questions, generated answers, tool
arguments, guide passages, Jira records, HTTP bodies/headers, or reasoning text.
Known runtime secrets and authorization strings are redacted as an additional
precaution. DEBUG does not enable payload logging. Diagnostic error summaries
are sanitized and bounded; unknown/non-JSON response bodies are omitted.

Durations named `observed_duration_ms` are measured between received stream
events and can include argument generation, scheduling, execution, and network
delivery; they are not exact provider trace-span timings. A completed turn can
contain failed tools, so read `tool_failures` and individual tool outcomes.
Missing token usage is logged as `usage_available=false`, never zero. Recorded
usage is best-effort and can arrive later; cached tokens are included in input
tokens and reasoning tokens in output tokens. There is no extra usage polling
or API call solely for logging. See the
[OpenAI observability guide](https://developers.openai.com/api/docs/guides/agents-api/observability)
for accounting details.

## Use an existing agent with an authenticated MCP connection

After saving MCP tools or instructions on the Platform, click **New chat** in the local app. A browser refresh continues the previous session and its original configuration. Keep the local app pointed at the same saved agent with `OPENAI_AGENT_ID`.

For MCP servers using Bearer or OAuth credentials, the Platform attaches an agent's saved MCP connections when it creates a session. API clients must explicitly attach the vault containing the MCP credential. In **Agents > Vault**, copy the **Vault ID** for the saved connection. A `vault_` ID is a resource reference, not the token itself; a `credential_` ID does not replace it. Atlassian personal API tokens require Basic authentication; follow the next section for those tokens.

Stop the existing local server with Ctrl+C, then run these commands in the same PowerShell window where `OPENAI_API_KEY` is already set. Replace the placeholder with the Vault ID displayed in your project:

```powershell
Set-Location "$env:USERPROFILE\Documents\Nova-QnA-Chatbot"
$env:OPENAI_AGENT_ID = "agent_791112d1d9c94517b1911ec1e9017f622f3f56e7468c4e6989"
$env:OPENAI_VAULT_IDS = "vault_REPLACE_WITH_YOUR_CONNECTION_VAULT_ID"
python .\app.py
```

`OPENAI_VAULT_IDS` accepts comma-separated vault IDs or a JSON array. Alternatively, add a `vault_ids` array to the ignored `nova_state.json`; an explicitly set environment variable takes precedence. Configure only the vaults this app needs. Never put the MCP token itself in this variable, source code, or the browser.

Open `http://localhost:8080/api/health` to check the selected `agent_id` and `mcp_vault_count`. Click **New chat** and test a read operation such as: "Use Atlassian MCP to list my accessible Jira projects. Do not create or modify anything." Confirm a successful tool result in the session trace. A connected-server card alone does not establish that the tools initialized in a session.

If an optional MCP connection fails to initialize, the agent can continue without its tools. For diagnosis, edit that MCP entry's advanced settings, enable **Required**, save the agent, and create a new session to expose the startup failure. This app now preserves JSON error bodies from failed streaming requests, including the API message, code, and parameter where provided; a generic HTTP 400 alone is not a diagnosis.

For containers/NOVA, pass `OPENAI_VAULT_IDS` as runtime configuration alongside the existing agent and vector-store IDs. Restart the Python process after code or environment changes; edits to a saved Platform agent require a new chat rather than a server restart.

## Configure Atlassian OAuth for OpenAI-hosted MCP

The deployed Agents API rejected inline MCP authorization with `agents must not include MCP authorization; provide credentials through session vault_ids`. The former `app.py --atlassian-mcp` Basic mode has been retired. A personal Atlassian API token can pass the direct diagnostic while still being incompatible with an OpenAI Vault Bearer credential. This setup uses browser OAuth and a Vault `mcp_oauth` credential instead. See [OpenAI Vaults](https://developers.openai.com/api/docs/guides/agents-api/tools/vaults) and [Atlassian OAuth](https://developer.atlassian.com/cloud/rovo-mcp/guides/configuring-oauth-2-1/).

Stop the previous local server with Ctrl+C, then run:

```powershell
Set-Location "$env:USERPROFILE\Documents\Nova-QnA-Chatbot"
python .\setup_atlassian_oauth.py --agent-id agent_791112d1d9c94517b1911ec1e9017f622f3f56e7468c4e6989 --environment-template-id envtmpl_57a1e715ebe14508ae646ebe7a97a0710f9f4e2374f043fd9d --run-app
```

If `OPENAI_API_KEY` is not already configured, enter the OpenAI project API key at the hidden prompt. The key needs agent read/write, Vault read/write, and session access. The helper discovers Atlassian's current OAuth endpoints, dynamically registers a public client, uses PKCE S256 and a checked state value, and opens the browser sign-in/consent page. Review the requested read-only Jira, account identity, and offline refresh access there. You do not enter your personal Atlassian API token. If no browser opens, copy the printed sign-in URL into your browser; the helper waits up to ten minutes for the localhost callback. Organization policies may require admin approval for that client/redirect URI.

After consent, the helper saves the OAuth access and refresh tokens in OpenAI Vault. It updates only the saved agent's tools to reference the new credential, preserving the guide search function and the saved model/instructions. The Atlassian allowlist is restricted to read tools and startup is required so failures are visible. OAuth secrets travel through curl stdin; they are not saved in local source, request body files, `nova_state.json`, command arguments, or browser chat. Only resource/session IDs are checkpointed locally. The helper then creates an OpenAI-hosted session using the saved agent ID and Vault ID and runs this read-only test:

> Use Atlassian MCP to call getAccessibleAtlassianResources. List the returned Jira sites and cloud IDs. Do not create or modify anything.

The helper reports `PASS` only after observing a completed MCP resource lookup without a reported tool error. It prints the Platform session URL. With `--run-app`, it starts the chatbot after that successful test, reusing the OpenAI key in the same Python process. Open [http://localhost:8080](http://localhost:8080) and click **New chat**. Inspect the native MCP call in the session trace. Ticket creation is not enabled or tested by this read-only setup.

If the credential was checkpointed but binding/testing failed, retry without a new login:

```powershell
python .\setup_atlassian_oauth.py --agent-id agent_791112d1d9c94517b1911ec1e9017f622f3f56e7468c4e6989 --environment-template-id envtmpl_57a1e715ebe14508ae646ebe7a97a0710f9f4e2374f043fd9d --resume --run-app
```

Subsequent Platform sessions use the saved OAuth connection. Subsequent local runs read the Vault ID from `nova_state.json`; an explicitly configured `OPENAI_VAULT_IDS` takes precedence, so update or remove any obsolete override. For NOVA hosting, inject the OpenAI API key through its approved secret mechanism and configure the new Vault ID. OAuth tokens remain in OpenAI Vault and are refreshed there using the saved refresh configuration.

The existing `env` hosted template above produced a successful native `getAccessibleAtlassianResources` call in Platform session `sess_0cac073205b63b84006ada316eb92881989ed83a9e53c5f6e0`. The earlier OAuth test with no sandbox failed before generation with a generic internal error. The helper saves `environment_template_id` locally; the app uses it for new sessions. `OPENAI_ENVIRONMENT_TEMPLATE_ID` can override it, and `/api/health` reports `environment_type`. Use the hosted template when testing this integration. The template must exist in the selected project; these IDs belong to this Nova project.

The first API test with the hosted template and initial input in the create request also failed before generation, although its environment later showed Connected. Hosted startup now follows the observed successful Platform sequence: create the session without input, poll the environment until Connected (up to five minutes), open the event stream, then submit the first message separately. The helper prints the session ID and environment states while waiting. It does not submit input after a startup failure or timeout, and it does not automatically retry a submitted message. The original combined-create flow remains available for sessions without a sandbox. This change still needs a live API result to establish that it resolves the app-created session failure.

## Check Atlassian MCP directly

Open another PowerShell window and run the diagnostic independently of the local chat server:

```powershell
Set-Location "$env:USERPROFILE\Documents\Nova-QnA-Chatbot"
python .\diagnose_atlassian_mcp.py --compare-bearer
```

Enter the Atlassian email that owns your existing personal API token, then paste that token at the hidden prompt. This uses the Atlassian token, not `OPENAI_API_KEY`. The token is sent only to `https://mcp.atlassian.com/v2/mcp`, through curl configuration on stdin. It is not written to a file or placed in process arguments. The script does not follow redirects or execute MCP tools; its allowed methods are initialization and tool discovery only. It compares Basic (email plus personal token) with Bearer (the same raw token) without changing the Platform connection.

The script attempts current MCP request metadata, then uses an older initialize/initialized handshake when the server requires it, carrying a returned session ID and negotiated protocol version. It prints HTTP statuses and sanitized errors. A successful tools/list response establishes direct discovery from this computer; it does not establish access to a specific Jira project or prove that OpenAI can reach the server. A 400 protocol error is not automatically an authentication failure; 401/403 responses require the server's error details to distinguish credentials, permissions, and organization controls.

Share the diagnostic's output when troubleshooting, never the token or Authorization header. No OpenAI API key or server restart is needed to run this check.

## Container runtime and NOVA handoff

Build and run the generic container locally:

```powershell
docker build -t nova-guide-assistant .
docker run --rm -p 8080:8080 `
  -e OPENAI_API_KEY `
  -e OPENAI_PROJECT_ID=proj_dXDjYYmzfErZmSza7mHcNP0y `
  -e OPENAI_AGENT_ID=agent_id_from_nova_state_json `
  -e OPENAI_VECTOR_STORE_ID=vector_store_id_from_nova_state_json `
  nova-guide-assistant
```

On NOVA, store `OPENAI_API_KEY` in the approved NOVA secret mechanism and provide `OPENAI_PROJECT_ID`, `OPENAI_AGENT_ID`, and `OPENAI_VECTOR_STORE_ID` as runtime configuration. Do not bake credentials into the image, repository, browser code, or pipeline YAML. The exact NOVA pipeline file, target cluster/namespace, identity, routing, and health-check conventions are environment-specific and must be filled in through the current NOVA/CPE onboarding process. This repository supplies a generic container and `/api/health`; it does not assume those NOVA values.

The sample app does not implement user sign-in. Expose it only behind the authentication and access controls required by the NOVA environment.

## Answering rules

Initial provisioning creates a `search_nova_guide` function, which the application handles by searching the indexed DOCX vector store. The optional [hosted guide MCP setup](HOSTED_GUIDE_MCP.md) replaces that function with an MCP tool of the same name running inside each prepared hosted environment. Other saved tools, including an added Atlassian connection, are preserved during that migration. The supplied `web_search` setting was excluded so the assistant cannot supplement guide answers from the public web. The guide instructions require relevant section citations, preservation of caveats, clarification that examples are illustrative, and the exact Jira fallback when the guide does not specify the answer. Live Jira requests use Atlassian MCP under the saved agent's Jira instructions. Retrieved content does not override the agent's instructions.

The implementation relays session events (including output text, guide-search activity, and errors) to the browser. It handles `agent.session.requires_action` by searching the configured vector store and returning the required function result before the session turn continues.
The Agents session-events endpoint can acknowledge accepted messages and tool results with an empty HTTP 202 response; the client treats a successful empty response as acceptance and keeps reading the session stream for completion.

## OpenAI API references

- [Agents API overview](https://developers.openai.com/api/docs/guides/agents-api/overview)
- [Configure and reuse agents](https://developers.openai.com/api/docs/guides/agents-api/configuration)
- [Run sessions and stream events](https://developers.openai.com/api/docs/guides/agents-api/sessions)
- [Events and items](https://developers.openai.com/api/docs/guides/agents-api/sessions/events)
- [File search and supported file types](https://developers.openai.com/api/docs/guides/tools-file-search)
