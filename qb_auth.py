"""
qb_auth.py — One-time QuickBooks OAuth token generator.
Run locally for sandbox. Deploy to Railway for production.
"""

import os
import threading
import webbrowser
from urllib.parse import urlencode, urlparse, parse_qs
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.environ.get("QB_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("QB_CLIENT_SECRET", "")
ENVIRONMENT = os.environ.get("QB_ENVIRONMENT", "sandbox")
REDIRECT_URI = os.environ.get("QB_REDIRECT_URI", "http://localhost:8080/callback")

AUTH_BASE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPES = "com.intuit.quickbooks.accounting"

if not CLIENT_ID or not CLIENT_SECRET:
    print("\n❌ ERROR: QB_CLIENT_ID and QB_CLIENT_SECRET must be set\n")
    exit(1)

# Use PORT env var (set by Railway) to detect if running on Railway
# If PORT is set, we're on Railway → use Flask/Railway flow
# If PORT is not set, we're local → use localhost flow
IS_RAILWAY = os.environ.get("PORT") is not None

print(f"\n🔧 Environment: {ENVIRONMENT.upper()}")
print(f"🔧 Redirect URI: {REDIRECT_URI}")
print(f"🔧 Running on: {'Railway' if IS_RAILWAY else 'Local'}\n")


def exchange_and_print(auth_code, realm_id):
    print("🔄 Exchanging authorization code for tokens...")
    token_response = requests.post(
        TOKEN_URL,
        auth=HTTPBasicAuth(CLIENT_ID, CLIENT_SECRET),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "authorization_code", "code": auth_code, "redirect_uri": REDIRECT_URI},
    )
    if token_response.status_code != 200:
        print(f"\n❌ Token exchange failed: {token_response.status_code}")
        print(f"   {token_response.text}\n")
        return
    tokens = token_response.json()
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3600)
    refresh_expires = tokens.get("x_refresh_token_expires_in", 8726400)
    print("\n" + "="*60)
    print("  ✅ SUCCESS — Copy these into Railway environment variables")
    print("="*60)
    print(f"\nQB_ACCESS_TOKEN={access_token}")
    print(f"\nQB_REFRESH_TOKEN={refresh_token}")
    if realm_id:
        print(f"\nQB_COMPANY_ID={realm_id}")
    print(f"\nQB_ENVIRONMENT=production")
    print("\n" + "="*60)
    print(f"\n⏱  Access token expires: {expires_in // 60} min (auto-refreshed by bot)")
    print(f"⏱  Refresh token expires: {refresh_expires // 86400} days")
    print("\n⚠️  Keep these private. Never commit to git.\n")


def run_localhost_flow():
    from http.server import HTTPServer, BaseHTTPRequestHandler
    auth_code = None
    realm_id = None
    server_done = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code, realm_id
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if "code" in params:
                auth_code = params["code"][0]
                realm_id = params.get("realmId", [None])[0]
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body style='font-family:sans-serif;padding:40px;text-align:center'><h2>&#10003; Authorized! Return to your terminal.</h2></body></html>")
                server_done.set()
            else:
                self.send_response(400)
                self.end_headers()
                server_done.set()
        def log_message(self, format, *args): pass

    auth_url = f"{AUTH_BASE_URL}?{urlencode({'client_id': CLIENT_ID, 'response_type': 'code', 'scope': SCOPES, 'redirect_uri': REDIRECT_URI, 'state': 'qb_auth_state'})}"
    server = HTTPServer(("localhost", 8080), CallbackHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    print(f"📂 Opening browser for authorization...")
    print(f"   If it doesn't open: {auth_url}\n")
    webbrowser.open(auth_url)
    print("⏳ Waiting for authorization...")
    server_done.wait(timeout=120)
    if auth_code:
        exchange_and_print(auth_code, realm_id)
    else:
        print("\n❌ Timed out. Try again.\n")


def run_railway_flow():
    try:
        from flask import Flask, request as flask_request
    except ImportError:
        os.system("pip install flask --break-system-packages -q")
        from flask import Flask, request as flask_request

    app = Flask(__name__)

    @app.route("/callback")
    def callback():
        code = flask_request.args.get("code")
        realm_id = flask_request.args.get("realmId")
        error = flask_request.args.get("error")
        if error:
            return f"<h2>Authorization failed: {error}</h2>", 400
        if code:
            exchange_and_print(code, realm_id)
            return """<html><body style='font-family:sans-serif;padding:40px;text-align:center'>
                <h2>&#10003; Authorization successful!</h2>
                <p>Tokens printed to Railway logs. Copy them into Railway env vars, then redeploy.</p>
                </body></html>"""
        return "<h2>No code received</h2>", 400

    auth_url = f"{AUTH_BASE_URL}?{urlencode({'client_id': CLIENT_ID, 'response_type': 'code', 'scope': SCOPES, 'redirect_uri': REDIRECT_URI, 'state': 'qb_auth_state'})}"

    print("="*60)
    print("  QuickBooks OAuth — PRODUCTION MODE (Railway)")
    print("="*60)
    print("\n📋 STEP 1: Open this URL in your browser:\n")
    print(f"   {auth_url}\n")
    print("📋 STEP 2: Log in with your REAL Hashing Company QB account")
    print("📋 STEP 3: After authorizing, check these Railway logs for tokens\n")

    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    if IS_RAILWAY:
        run_railway_flow()
    else:
        run_localhost_flow()