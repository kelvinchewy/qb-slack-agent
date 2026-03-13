"""
QB Slack Agent — Main entry point.
Slack Bolt app using Socket Mode + Flask HTTP API for agent-to-agent calls.

HTTP endpoints:
  GET  /health  — uptime check, no auth required
  POST /query   — agent-to-agent query, requires X-API-Key header
    Request:  { "query": "what were expenses last month?" }
    Response: { "status": "ok", "question": "...", "answer": "...", "data": {...} }
"""

import logging
import os
import re
import threading

from flask import Flask, request, jsonify
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import Config
from orchestrator import classify_intent
from report_builder import build_report
from qb_interpreter import interpret_and_fetch
from qb_analyst import analyse

logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("qb-agent")

Config.validate_slack()
Config.validate_anthropic()

logger.info(f"MOCK_MODE: {Config.MOCK_MODE}")
logger.info("Starting QB Slack Agent...")

QB_API_KEY = os.environ.get("QB_API_KEY", "")

# ── Slack app ──────────────────────────────────────────────────────────────

slack_app = App(token=Config.SLACK_BOT_TOKEN, signing_secret=Config.SLACK_SIGNING_SECRET)


def strip_mention(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


def process_and_reply(client, channel: str, thread_ts: str, user_message: str):
    """
    Runs in a background thread — does the heavy lifting after Slack ack.
    Posts a thinking message immediately, then updates it with the real answer.
    """
    thinking_ts = None

    try:
        clean_text = strip_mention(user_message)

        if not clean_text:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Hey! Ask me a financial question, or type *help* to see what I can do.",
            )
            return

        # Post thinking message immediately so user knows it's working
        thinking_resp = client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="⏳ Looking into that, give me a moment...",
        )
        thinking_ts = thinking_resp.get("ts")

        logger.info(f"Processing query: '{clean_text}'")

        intent_data = classify_intent(clean_text)
        intent_data["original_question"] = clean_text

        logger.info(f"Route: {intent_data.get('route')} | Intent: {intent_data.get('intent')}")

        blocks = build_report(intent_data)

        # Update the thinking message with the real answer
        if thinking_ts:
            client.chat_update(
                channel=channel,
                ts=thinking_ts,
                blocks=blocks,
                text=f"Finance report: {intent_data.get('intent', 'query')}",
            )
        else:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=blocks,
                text=f"Finance report: {intent_data.get('intent', 'query')}",
            )

    except Exception as e:
        logger.error(f"Query processing error: {e}")
        try:
            error_text = "Something went wrong. Please try again."
            if thinking_ts:
                client.chat_update(
                    channel=channel,
                    ts=thinking_ts,
                    text=error_text,
                )
            else:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=error_text,
                )
        except Exception:
            pass


@slack_app.event("app_mention")
def handle_mention(event, ack, client):
    ack()
    thread_ts = event.get("ts")
    channel = event.get("channel")
    user_message = event.get("text", "")
    logger.info(f"Mention from {event.get('user')}: {user_message}")
    threading.Thread(
        target=process_and_reply,
        args=(client, channel, thread_ts, user_message),
        daemon=True,
    ).start()


@slack_app.event("message")
def handle_dm(event, ack, client):
    ack()
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel_type") != "im":
        return
    channel = event.get("channel")
    user_message = event.get("text", "")
    logger.info(f"DM from {event.get('user')}: {user_message}")
    threading.Thread(
        target=process_and_reply,
        args=(client, channel, None, user_message),
        daemon=True,
    ).start()


# ── Flask HTTP API ─────────────────────────────────────────────────────────

flask_app = Flask(__name__)


def check_api_key():
    """Returns 401 response if key is wrong, None if OK."""
    if not QB_API_KEY:
        return None  # No key set — open access
    if request.headers.get("X-API-Key", "") != QB_API_KEY:
        return jsonify({"error": "Unauthorised", "status": "error"}), 401
    return None


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "qb-slack-agent"})


@flask_app.route("/auth", methods=["GET"])
def auth():
    """
    Start QuickBooks OAuth flow.
    Visit https://qb-slack-agent-production.up.railway.app/auth in browser to re-authorize.
    """
    import secrets
    from urllib.parse import urlencode

    state = secrets.token_urlsafe(16)
    params = {
        "client_id": Config.QB_CLIENT_ID,
        "scope": "com.intuit.quickbooks.accounting",
        "redirect_uri": Config.QB_REDIRECT_URI,
        "response_type": "code",
        "state": state,
    }
    auth_url = "https://appcenter.intuit.com/connect/oauth2?" + urlencode(params)
    logger.info(f"Starting QB OAuth flow, redirecting to Intuit...")
    from flask import redirect
    return redirect(auth_url)


@flask_app.route("/callback", methods=["GET"])
def callback():
    """
    QuickBooks OAuth callback — exchanges code for tokens and saves to Railway.
    """
    import requests as req
    from requests.auth import HTTPBasicAuth

    code = request.args.get("code")
    error = request.args.get("error")

    if error:
        logger.error(f"QB OAuth error: {error}")
        return f"<h2>❌ Authorization failed: {error}</h2>", 400

    if not code:
        return "<h2>❌ No authorization code received.</h2>", 400

    try:
        # Exchange code for tokens
        response = req.post(
            "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
            auth=HTTPBasicAuth(Config.QB_CLIENT_ID, Config.QB_CLIENT_SECRET),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": Config.QB_REDIRECT_URI,
            },
        )

        if response.status_code != 200:
            logger.error(f"Token exchange failed: {response.status_code} — {response.text}")
            return f"<h2>❌ Token exchange failed: {response.text}</h2>", 400

        tokens = response.json()
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]

        logger.info("✅ QB OAuth tokens obtained successfully.")

        # Update in-memory token manager immediately
        import time
        import qb_agent
        qb_agent._token_manager.access_token = access_token
        qb_agent._token_manager.refresh_token = refresh_token
        qb_agent._token_manager.expires_at = time.time() + tokens.get("expires_in", 3600)
        logger.info("✅ In-memory tokens updated.")

        # Persist to Railway so tokens survive future restarts
        qb_agent._persist_tokens_to_railway(access_token, refresh_token)

        return """
        <h2>✅ QuickBooks Authorization Successful!</h2>
        <p>Tokens have been saved to Railway. The finance agent is now connected to QuickBooks.</p>
        <p>You can close this window.</p>
        """, 200

    except Exception as e:
        logger.error(f"Callback error: {e}")
        return f"<h2>❌ Error: {e}</h2>", 500


@flask_app.route("/auth-status", methods=["GET"])
def auth_status():
    """
    Debug endpoint — shows QB token state and Railway persistence config.
    Visit https://qb-slack-agent-production.up.railway.app/auth-status to check.
    """
    import time
    import qb_agent
    tm = qb_agent._token_manager
    now = time.time()
    expires_in = int(tm.expires_at - now) if tm.expires_at > now else -1

    railway_ok = all([
        os.environ.get("RAILWAY_API_TOKEN"),
        os.environ.get("RAILWAY_SERVICE_ID"),
        os.environ.get("RAILWAY_ENVIRONMENT_ID"),
        os.environ.get("RAILWAY_PROJECT_ID"),
    ])

    return jsonify({
        "token_state": {
            "has_access_token": bool(tm.access_token),
            "has_refresh_token": bool(tm.refresh_token),
            "expires_in_seconds": expires_in,
            "needs_refresh": tm._needs_refresh(),
        },
        "railway_persistence": {
            "configured": railway_ok,
            "has_api_token": bool(os.environ.get("RAILWAY_API_TOKEN")),
            "has_service_id": bool(os.environ.get("RAILWAY_SERVICE_ID")),
            "has_environment_id": bool(os.environ.get("RAILWAY_ENVIRONMENT_ID")),
            "has_project_id": bool(os.environ.get("RAILWAY_PROJECT_ID")),
        },
        "action": "Visit /auth to re-authorize QuickBooks" if expires_in < 0 else "OK"
    })


@flask_app.route("/query", methods=["POST"])
def query():
    """
    Agent-to-agent query endpoint.
    Returns plain structured JSON — no Slack formatting.

    Request:
      POST /query
      X-API-Key: <QB_API_KEY>
      Content-Type: application/json
      { "query": "what were our total expenses last month?" }

    Response:
      {
        "status": "ok",
        "question": "...",
        "answer": "...",         ← plain text direct answer
        "data": {                ← full structured result for coordinator to use
          "key_findings": [...],
          "proactive_flags": [...],
          "detail_table": {...},
          "data_completeness": "complete|partial|incomplete"
        }
      }
    """
    auth_error = check_api_key()
    if auth_error:
        return auth_error

    body = request.get_json(silent=True)
    if not body or not body.get("query"):
        return jsonify({"error": "Missing 'query' field", "status": "error"}), 400

    question = body["query"].strip()
    if not question:
        return jsonify({"error": "Query cannot be empty", "status": "error"}), 400

    logger.info(f"HTTP /query: '{question}'")

    try:
        # Full pipeline — same as Slack path
        interpreter_result = interpret_and_fetch(question)
        analysis = analyse(interpreter_result)

        return jsonify({
            "status": "ok",
            "question": question,
            "answer": analysis.get("direct_answer", ""),
            "data": {
                "key_findings": analysis.get("key_findings", []),
                "proactive_flags": analysis.get("proactive_flags", []),
                "summary_line": analysis.get("summary_line", ""),
                "detail_table": analysis.get("detail_table"),
                "data_completeness": analysis.get("data_completeness", ""),
                "data_note": analysis.get("data_note", ""),
            }
        })

    except Exception as e:
        logger.error(f"HTTP query error: {e}")
        return jsonify({"error": str(e), "status": "error"}), 500


# ── Entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("⚡ QB Slack Agent is starting...")

    port = int(os.environ.get("PORT", 3000))
    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    logger.info(f"🌐 HTTP API listening on port {port}")

    handler = SocketModeHandler(slack_app, Config.SLACK_APP_TOKEN)
    handler.start()