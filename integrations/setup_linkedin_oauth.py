"""Interactive LinkedIn OAuth 2.0 setup script.

Walks through the Authorization Code Flow to obtain an access token, then
stores credentials in Doppler under project `motto-director`, config `prd`.

Usage:
    doppler run --project motto-director --config prd -- python integrations/setup_linkedin_oauth.py

    Or without Doppler (will prompt for client ID/secret):
    python integrations/setup_linkedin_oauth.py

Prerequisites:
    1. A LinkedIn Developer app at https://developer.linkedin.com/
    2. OAuth 2.0 redirect URI set to http://localhost:8080/callback in the app settings
    3. Doppler CLI installed and authenticated (for automatic secret storage)
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
REDIRECT_URI = "http://localhost:8080/callback"
SCOPES = ["r_liteprofile", "r_emailaddress", "w_member_social"]

DOPPLER_PROJECT = "motto-director"
DOPPLER_CONFIG = "prd"

# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------

_auth_code: Optional[str] = None
_server_error: Optional[str] = None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        global _auth_code, _server_error
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "error" in params:
            _server_error = params["error"][0]
            self._respond(400, f"OAuth error: {_server_error}")
        elif "code" in params:
            _auth_code = params["code"][0]
            self._respond(200, "Authorization successful! You can close this tab.")
        else:
            self._respond(400, "Missing code parameter in callback.")

    def _respond(self, status: int, message: str) -> None:
        body = f"<html><body><h2>{message}</h2></body></html>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # suppress access logs
        pass


def _start_callback_server(port: int = 8080) -> http.server.HTTPServer:
    server = http.server.HTTPServer(("localhost", port), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------------------
# Doppler helpers
# ---------------------------------------------------------------------------

def _doppler_set(key: str, value: str) -> bool:
    """Store a secret in Doppler. Returns True on success."""
    try:
        result = subprocess.run(
            [
                "doppler", "secrets", "set", f"{key}={value}",
                "--project", DOPPLER_PROJECT,
                "--config", DOPPLER_CONFIG,
                "--silent",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _doppler_available() -> bool:
    try:
        result = subprocess.run(
            ["doppler", "--version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def main() -> None:
    print("\n=== LinkedIn OAuth 2.0 Setup ===\n")

    # 1. Get client credentials
    client_id = os.environ.get("LINKEDIN_CLIENT_ID") or input(
        "LinkedIn Client ID (from developer.linkedin.com): "
    ).strip()
    client_secret = os.environ.get("LINKEDIN_CLIENT_SECRET") or input(
        "LinkedIn Client Secret: "
    ).strip()

    if not client_id or not client_secret:
        print("ERROR: Client ID and Client Secret are required.")
        sys.exit(1)

    redirect_uri = os.environ.get("LINKEDIN_REDIRECT_URI", REDIRECT_URI)

    # 2. Build authorization URL
    state = secrets.token_urlsafe(16)
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": " ".join(SCOPES),
    }
    auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(auth_params)}"

    # 3. Start local callback server
    print(f"Starting local callback server on {redirect_uri} ...")
    try:
        server = _start_callback_server(port=8080)
    except OSError as e:
        print(f"ERROR: Could not start callback server on port 8080: {e}")
        print("Make sure port 8080 is free, or update REDIRECT_URI in this script.")
        sys.exit(1)

    # 4. Open browser
    print(f"\nOpening browser for LinkedIn authorization...")
    print(f"If the browser does not open, visit this URL manually:\n\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # 5. Wait for callback
    print("Waiting for authorization callback (timeout: 120s)...")
    import time
    for _ in range(120):
        if _auth_code or _server_error:
            break
        time.sleep(1)

    server.shutdown()

    if _server_error:
        print(f"\nERROR: LinkedIn returned an error: {_server_error}")
        sys.exit(1)

    if not _auth_code:
        print("\nERROR: Timed out waiting for authorization. Please try again.")
        sys.exit(1)

    print("Authorization code received.")

    # 6. Exchange code for token
    print("Exchanging authorization code for access token...")
    try:
        resp = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": _auth_code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        print(f"\nERROR: Token exchange failed: {e.response.status_code} {e.response.text}")
        sys.exit(1)

    token_data = resp.json()
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token", "")
    expires_in = token_data.get("expires_in", "unknown")

    if not access_token:
        print(f"\nERROR: No access_token in response: {json.dumps(token_data, indent=2)}")
        sys.exit(1)

    print(f"Access token obtained (expires in {expires_in}s).")

    # 7. Store in Doppler (or print for manual storage)
    secrets_to_store = {
        "LINKEDIN_CLIENT_ID": client_id,
        "LINKEDIN_CLIENT_SECRET": client_secret,
        "LINKEDIN_REDIRECT_URI": redirect_uri,
        "LINKEDIN_ACCESS_TOKEN": access_token,
    }
    if refresh_token:
        secrets_to_store["LINKEDIN_REFRESH_TOKEN"] = refresh_token

    if _doppler_available():
        print(f"\nStoring secrets in Doppler ({DOPPLER_PROJECT}/{DOPPLER_CONFIG})...")
        all_ok = True
        for key, value in secrets_to_store.items():
            ok = _doppler_set(key, value)
            status = "✓" if ok else "✗"
            print(f"  {status} {key}")
            if not ok:
                all_ok = False

        if not all_ok:
            print("\nWARNING: Some secrets failed to store. Set them manually (see below).")
    else:
        print("\nDoppler CLI not found. Store these secrets manually:\n")

    # Always print the commands for reference (mask sensitive values)
    print("\nDoppler commands to store secrets manually:")
    for key in secrets_to_store:
        print(f"  doppler secrets set {key}=<value> --project {DOPPLER_PROJECT} --config {DOPPLER_CONFIG}")

    print("\nSetup complete. Run the MCP server with:")
    print(f"  doppler run --project {DOPPLER_PROJECT} --config {DOPPLER_CONFIG} -- python integrations/linkedin_mcp_server.py\n")


if __name__ == "__main__":
    main()
