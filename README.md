# Nova Deployment Guide Assistant

A small, runnable web chat app grounded only in the Nova Deployment Guide. It saves a reusable agent and guide vector store in the selected OpenAI project, then serves a browser UI that streams Agents API session events. The API key stays on the server and is never sent to the browser.

## What this repository contains

- `provision.py` uploads and indexes the source DOCX, creates the reusable **Nova Deployment Guide Assistant**, then starts a session with an initial user message and prints the streamed events.
- `app.py` is a dependency-free Python HTTP app for chat, health checks, and streamed session events.
- `openai_http.py` calls the OpenAI HTTP API through `curl` / `curl.exe`; it contains no credentials.
- `index.html` is the browser chat UI.
- `Dockerfile` packages the runtime for a container-based platform such as NOVA.

The deployment guide itself is not part of this repository. Provision it from the approved source file path. Do not put it in GitHub unless its owner has separately approved that destination.

## Create the OpenAI agent and vector store once

Requirements: Python 3.10+, `curl` in `PATH`, and an OpenAI project API key with access to project `proj_dXDjYYmzfErZmSza7mHcNP0y`. For a restricted key, provisioning needs file and vector-store write access plus agent read/write and response write access. The running chat app needs agent read/write and response write access.

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

Provisioning creates a vector store, uploads and indexes the DOCX, creates the reusable agent with `file_search` attached to that vector store, then creates a session using the returned agent ID. Its initial user message is “What does the guide say about getting started with a Nova deployment? Cite the relevant section.” Override it with `--initial-question "..."`.

Provisioning writes the OpenAI resource IDs to ignored local file `nova_state.json`. The file contains no API key. Keep it out of source control. If setup stops after creating OpenAI resources, the printed error identifies the failed step; a retry creates new resources, so check the project before rerunning.

## Run the web chat locally

In the same PowerShell window where `OPENAI_API_KEY` is set:

```powershell
python .\app.py
```

Open [http://localhost:8080](http://localhost:8080). The first message creates a new session from `OPENAI_AGENT_ID` (or the local ID in `nova_state.json`); follow-up messages continue that browser’s session. A new chat starts another session. The browser stores only the session ID in local storage.

If running the web app elsewhere, set `OPENAI_API_KEY`, `OPENAI_PROJECT_ID`, and `OPENAI_AGENT_ID` in that process’s environment. The project ID defaults to the Nova project above. `PORT` defaults to `8080`; `HOST` defaults to `0.0.0.0`. The health endpoint is `GET /api/health`.

## Container runtime and NOVA handoff

Build and run the generic container locally:

```powershell
docker build -t nova-guide-assistant .
docker run --rm -p 8080:8080 `
  -e OPENAI_API_KEY `
  -e OPENAI_PROJECT_ID=proj_dXDjYYmzfErZmSza7mHcNP0y `
  -e OPENAI_AGENT_ID=agent_id_from_nova_state_json `
  nova-guide-assistant
```

On NOVA, store `OPENAI_API_KEY` in the approved NOVA secret mechanism and provide `OPENAI_PROJECT_ID` and `OPENAI_AGENT_ID` as runtime configuration. Do not bake credentials into the image, repository, browser code, or pipeline YAML. The exact NOVA pipeline file, target cluster/namespace, identity, routing, and health-check conventions are environment-specific and must be filled in through the current NOVA/CPE onboarding process. This repository supplies a generic container and `/api/health`; it does not assume those NOVA values.

The sample app does not implement user sign-in. Expose it only behind the authentication and access controls required by the NOVA environment.

## Answering rules

The saved agent has only the `file_search` tool connected to the indexed DOCX; the supplied `web_search` setting was excluded so the assistant cannot supplement answers from the public web. The instructions require relevant section citations, preservation of caveats, clarification that examples are illustrative, and the exact Jira fallback when the guide does not specify the answer. The document is reference content; instructions embedded in it do not override the assistant’s instructions.

The implementation relays session events (including output text, file-search activity, and errors) to the browser. It has no custom function tools requiring application-side tool-result handling. If custom tools are added later, implement their `requires_action` flow before enabling them.

## OpenAI API references

- [Agents API overview](https://developers.openai.com/api/docs/guides/agents-api/overview)
- [Configure and reuse agents](https://developers.openai.com/api/docs/guides/agents-api/configuration)
- [Run sessions and stream events](https://developers.openai.com/api/docs/guides/agents-api/sessions)
- [Events and items](https://developers.openai.com/api/docs/guides/agents-api/sessions/events)
- [File search and supported file types](https://developers.openai.com/api/docs/guides/tools-file-search)
