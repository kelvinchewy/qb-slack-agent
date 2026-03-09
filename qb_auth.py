"""
qb_auth.py — One-time QuickBooks OAuth token generator.

Run this script ONCE locally to authorize your QuickBooks account
and get your access_token + refresh_token.

Paste the printed tokens into your Railway environment variables.
You won't need to run this again unless your refresh token expires (100 days of inactivity).

Usage:
    python qb_auth.py

Requirements:
    pip install requests python-dotenv
"""

import os
import json
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

# ─── Config ──────────────────────────────────────────────────────────

CLIENT_ID = os.environ.get("QB_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("QB_CLIENT_SECRET", "")
REDIRECT_URI = "http://localhost:8080/callback"
ENVIRONMENT = os.environ.get("QB_ENVIRONMENT", "sandbox")  # "sandbox" or "production"

# OAuth endpoints
AUTH_BASE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

# Scopes — com.intuit.quickbooks.accounting gives full read access
SCOPES = "com.intuit.quickbooks.accounting"

# ─── Validation ──────────────────────────────────────────────────────

if not CLIENT_ID or not CLIENT_SECRET:
    print("\n❌ ERROR: QB_CLIENT_ID and QB_CLIENT_SECRET must be set.")
    print("   Add them to your .env file or export them as environment variables.\n")
    exit(1)

# ─── OAuth Callback Handler ───────────────────────────────────────────

auth_code = None
realm_id = None
server_done = threading.Event()


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code, realm_id

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "code" in params:
            auth_code = params["code"][0]
            realm_id = params.get("realmId", [None])[0]

            # Send success response to browser
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="font-family: sans-serif; padding: 40px; text-align: center;">
                <h2>&#10003; Authorization successful!</h2>
                <p>You can close this tab and return to your terminal.</p>
                </body></html>
            """)
            server_done.set()
        elif "error" in params:
            error = params.get("error", ["unknown"])[0]
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(f"""
                <html><body style="font-family: sans-serif; padding: 40px; text-align: center;">
                <h2>&#10007; Authorization failed</h2>
                <p>Error: {error}</p>
                <p>Close this tab and check your terminal.</p>
                </body></html>
            """.encode())
            server_done.set()

    def log_message(self, format, *args):
        pass  # Suppress request logs


# ─── Main OAuth Flow ─────────────────────────────────────────────────

def run_oauth_flow():
    print("\n" + "="*60)
    print("  QuickBooks OAuth Token Generator")
    print("  Environment:", ENVIRONMENT.upper())
    print("="*60)

    # Step 1: Build authorization URL
    auth_params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": REDIRECT_URI,
        "state": "qb_auth_state",
    }
    auth_url = f"{AUTH_BASE_URL}?{urlencode(auth_params)}"

    # Step 2: Start local callback server
    server = HTTPServer(("localhost", 8080), CallbackHandler)
    server_thread = threading.Thread(target=server.handle_request)
    server_thread.daemon = True
    server_thread.start()

    # Step 3: Open browser for user authorization
    print("\n📂 Opening browser for QuickBooks authorization...")
    print("   If it doesn't open automatically, visit:")
    print(f"   {auth_url}\n")
    webbrowser.open(auth_url)

    # Step 4: Wait for callback
    print("⏳ Waiting for authorization...")
    server_done.wait(timeout=120)  # 2 minute timeout

    if not auth_code:
        print("\n❌ Timed out or authorization failed. Please try again.\n")
        return

    print(f"✅ Authorization code received!")
    if realm_id:
        print(f"   Company ID (realmId): {realm_id}")

    # Step 5: Exchange code for tokens
    print("\n🔄 Exchanging authorization code for tokens...")

    token_response = requests.post(
        TOKEN_URL,
        auth=HTTPBasicAuth(CLIENT_ID, CLIENT_SECRET),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": REDIRECT_URI,
        },
    )

    if token_response.status_code != 200:
        print(f"\n❌ Token exchange failed: {token_response.status_code}")
        print(f"   Response: {token_response.text}\n")
        return

    tokens = token_response.json()

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3600)
    refresh_expires = tokens.get("x_refresh_token_expires_in", 8726400)

    # Step 6: Print results
    print("\n" + "="*60)
    print("  ✅ SUCCESS — Copy these into Railway environment variables")
    print("="*60)
    print(f"\nQB_ACCESS_TOKEN={access_token}")
    print(f"\nQB_REFRESH_TOKEN={refresh_token}")
    if realm_id:
        print(f"\nQB_COMPANY_ID={realm_id}")
    print(f"\nQB_ENVIRONMENT={ENVIRONMENT}")
    print("\n" + "="*60)
    print(f"\n⏱  Access token expires in: {expires_in // 60} minutes (auto-refreshed by the bot)")
    print(f"⏱  Refresh token expires in: {refresh_expires // 86400} days of inactivity")
    print("\n⚠️  Keep these tokens private. Never commit them to git.\n")

    # Optional: save to local file for convenience
    save = input("💾 Save tokens to .env.tokens (local only, add to .gitignore)? [y/N]: ").strip().lower()
    if save == "y":
        with open(".env.tokens", "w") as f:
            f.write(f"QB_ACCESS_TOKEN={access_token}\n")
            f.write(f"QB_REFRESH_TOKEN={refresh_token}\n")
            if realm_id:
                f.write(f"QB_COMPANY_ID={realm_id}\n")
            f.write(f"QB_ENVIRONMENT={ENVIRONMENT}\n")
        print("✅ Saved to .env.tokens — add this file to your .gitignore!\n")


if __name__ == "__main__":
    run_oauth_flow()