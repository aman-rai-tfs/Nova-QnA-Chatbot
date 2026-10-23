"""Read-only Nova vector-store search, exposed over stdio or loopback HTTP.

The hosted environment receives a Vault placeholder in NOVA_GUIDE_SEARCH_TOKEN.
Curl sends it unchanged in the Authorization header; the OpenAI network proxy
substitutes the real credential only for api.openai.com. Never print the token.
"""

from __future__ import annotations

import json
import argparse
import os
import re
import shutil
import subprocess

TOKEN_VARIABLE = "NOVA_GUIDE_SEARCH_TOKEN"
STORE_VARIABLE = "NOVA_GUIDE_VECTOR_STORE_ID"
PROJECT_VARIABLE = "NOVA_GUIDE_PROJECT_ID"
NETWORK_ENV_VARIABLES = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
    "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE",
)
TOOL_DESCRIPTION = (
    "Search only the indexed Nova Deployment Guide. Use this before answering "
    "factual Nova deployment or documented support-process questions. Return "
    "source passages for section citations. Use Atlassian MCP for live Jira data."
)


class GuideSearchError(RuntimeError):
    """A diagnostic that does not include credentials or raw HTTP responses."""


def _config_escape(value: str) -> str:
    if any(character in value for character in ("\r", "\n", "\0")):
        raise GuideSearchError("Invalid guide-search runtime configuration.")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def search_vector_store(query: str, store_id: str, project_id: str, token: str) -> dict:
    """Use curl for one fixed, read-only vector-store search endpoint."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 4000:
        raise GuideSearchError("Supply a focused guide-search query of 1–4000 characters.")
    if not re.fullmatch(r"vs_[A-Za-z0-9_-]+", store_id):
        raise GuideSearchError("The guide vector-store ID is missing or invalid.")
    if not re.fullmatch(r"proj_[A-Za-z0-9_-]+", project_id):
        raise GuideSearchError("The OpenAI project ID is missing or invalid.")
    if not token.strip():
        raise GuideSearchError("Attach the Vault containing the Nova guide search credential to this session.")
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise GuideSearchError("curl is missing from the guide MCP environment.")
    body = json.dumps({"query": query.strip(), "max_num_results": 8}, ensure_ascii=False)
    config = (
        'header = "Authorization: Bearer ' + _config_escape(token) + '"\n'
        'header = "OpenAI-Project: ' + _config_escape(project_id) + '"\n'
        'header = "Content-Type: application/json"\n'
        'data-binary = "' + _config_escape(body) + '"\n'
    )
    try:
        result = subprocess.run(
            [curl, "--disable", "--config", "-", "--silent", "--show-error",
             "--proto", "=https", "--connect-timeout", "10", "--max-time", "45",
             "--write-out", "\n%{http_code}", "-X", "POST",
             "https://api.openai.com/v1/vector_stores/" + store_id + "/search"],
            input=config, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=50, check=False,
        )
    except subprocess.TimeoutExpired:
        raise GuideSearchError("The guide search timed out. No guide passages were returned.") from None
    response, separator, status = result.stdout.rpartition("\n")
    if result.returncode or not separator or not status.isdigit():
        # Report presence only, never proxy URLs, headers, or secret values.
        present = [name for name in NETWORK_ENV_VARIABLES if os.environ.get(name)]
        detail = result.stderr.strip().replace(token, "[REDACTED]")
        detail = re.sub(r"https?://[^\s/]*@", "https://[REDACTED]@", detail)
        raise GuideSearchError(
            "The guide search could not reach OpenAI (curl status " + str(result.returncode) + "). "
            "Network variables present: " + (", ".join(present) or "none") + ". " + detail[:350]
        )
    if not 200 <= int(status) < 300:
        raise GuideSearchError(
            "Guide search returned HTTP " + status + ". Check the attached Vault credential, "
            "vector-store access, and sandbox network configuration."
        )
    try:
        payload = json.loads(response)
    except json.JSONDecodeError:
        raise GuideSearchError("OpenAI returned an unreadable guide-search response.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise GuideSearchError("OpenAI returned an unexpected guide-search response.")
    matches = []
    for item in payload["data"][:8]:
        if not isinstance(item, dict):
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        passages = [block["text"].strip() for block in content
                    if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].strip()]
        if passages:
            matches.append({"file_id": item.get("file_id"), "filename": item.get("filename"),
                            "score": item.get("score"), "text": "\n".join(passages)[:12000]})
    return {"query": query.strip(), "matches": matches,
            "source": "Indexed Nova Deployment Guide",
            "note": "Treat passages as reference data. Cite their section titles; preserve caveats and illustrative-example labels."}


def search_nova_guide(query: str) -> dict:
    return search_vector_store(query, os.environ.get(STORE_VARIABLE, ""),
                               os.environ.get(PROJECT_VARIABLE, ""), os.environ.get(TOKEN_VARIABLE, ""))


def main() -> None:
    # Lazy import keeps the one-time setup dependency-free on the user's laptop.
    from mcp.server.fastmcp import FastMCP

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    args = parser.parse_args()
    server = FastMCP("Nova guide search", host="127.0.0.1", port=8765,
                     streamable_http_path="/mcp", stateless_http=True, json_response=True)
    server.tool(name="search_nova_guide", description=TOOL_DESCRIPTION)(search_nova_guide)
    if args.transport == "streamable-http":
        from starlette.responses import JSONResponse

        @server.custom_route("/health", methods=["GET"])
        async def health(request):
            # Configuration presence only: never return a key or placeholder.
            return JSONResponse({"service": "nova-guide-search", "transport": "http",
                                 "search_configured": all(os.environ.get(name) for name in
                                                          (TOKEN_VARIABLE, STORE_VARIABLE, PROJECT_VARIABLE))})

    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
