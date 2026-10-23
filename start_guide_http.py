"""Hosted setup command: start the loopback MCP server and wait for readiness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent
HEALTH_URL = "http://127.0.0.1:8765/health"


def ready() -> bool:
    # Only this loopback health request bypasses proxies. The search process
    # retains all proxy/certificate variables for the outbound Vault substitution.
    try:
        with build_opener(ProxyHandler({})).open(HEALTH_URL, timeout=1) as response:
            info = json.load(response)
        if info.get("service") != "nova-guide-search" or info.get("transport") != "http":
            raise RuntimeError("Port 8765 is occupied by an unexpected service.")
        if not info.get("search_configured"):
            raise RuntimeError("Guide HTTP server has no search configuration. Attach the guide Vault/API secret to the session.")
        return True
    except (URLError, TimeoutError, ConnectionError):
        return False


def main() -> None:
    if os.name != "posix":
        raise RuntimeError("This startup command runs inside the Linux hosted environment.")
    if ready():
        print("Nova guide HTTP server is already ready on loopback.", flush=True)
        return
    # No token is included in process arguments, files, or printed diagnostics.
    with (ROOT / "guide-http.log").open("ab", buffering=0) as log:
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "guide_mcp.py"), "--transport", "streamable-http"],
            cwd=ROOT, env=os.environ.copy(), stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True, close_fds=True,
        )
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("Guide HTTP server exited during startup; inspect /workspace/nova-guide/guide-http.log.")
            if ready():
                print("Nova guide HTTP server ready on 127.0.0.1:8765; search configuration attached.", flush=True)
                return
            time.sleep(0.25)
        raise RuntimeError("Guide HTTP server did not become ready within 25 seconds.")
    except BaseException:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        raise


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print("Guide HTTP startup failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
