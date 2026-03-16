"""
QB Slack Agent — Main entry point.
Slack Bolt app using Socket Mode + Flask HTTP API for agent-to-agent calls.

Entry points:
  Slash commands: /nb-bills /nb-invoices /nb-vendors /nb-summary /nb-balance /nb-pnl /nb-finance
  @mention / DM:  natural language via #ask-finance or direct message
  HTTP API:       POST /query (agent-to-agent, X-API-Key required)

HTTP endpoints:
  GET  /health     — uptime check
  GET  /auth       — start QB OAuth flow in browser
  GET  /callback   — QB OAuth callback
  GET  /auth-status — token + Railway config debug
  POST /query      — agent-to-agent query, requires X-API-Key header
"""

import logging
import os
import re
import threading
import time

from flask import Flask, request, jsonify
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import Config
from orchestrator import classify_intent
from report_builder import build_report
from qb_interpreter import interpret_and_fetch, warm_cache, refresh_entity_cache
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


# ─── Helpers ──────────────────────────────────────────────────────────────

def strip_mention(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


def _run_pipeline(question: str) -> tuple[list, str]:
    """
    Core pipeline: question → interpreter → analyst → formatter.
    Returns (blocks, plain_text_fallback).
    """
    intent_data = classify_intent(question)
    intent_data["original_question"] = question
    blocks = build_report(intent_data)
    return blocks, intent_data.get("intent", "query")


def process_and_reply(client, channel: str, thread_ts: str | None, user_message: str):
    """
    Runs in background thread. Posts thinking message immediately, then updates in-place.
    Used for @mentions and DMs.
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

        thinking_resp = client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="⏳ Looking into that, give me a moment...",
        )
        thinking_ts = thinking_resp.get("ts")
        logger.info(f"Processing: '{clean_text}'")

        blocks, intent = _run_pipeline(clean_text)

        if thinking_ts:
            client.chat_update(
                channel=channel,
                ts=thinking_ts,
                blocks=blocks,
                text=f"Finance report: {intent}",
            )
        else:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, blocks=blocks,
                                    text=f"Finance report: {intent}")

    except Exception as e:
        logger.error(f"Query processing error: {e}")
        try:
            error_text = "Something went wrong. Please try again."
            if thinking_ts:
                client.chat_update(channel=channel, ts=thinking_ts, text=error_text)
            else:
                client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=error_text)
        except Exception:
            pass


def _slash_worker(respond, natural_language_query: str, thinking_done: bool = False):
    """
    Runs the full pipeline in a background thread.
    Posts thinking message first (unless thinking_done=True — used when caller
    already posted a thinking message, e.g. handle_clarification).
    Then replaces with real answer via replace_original=True.
    """
    try:
        logger.info(f"Slash command query: '{natural_language_query}'")
        if not thinking_done:
            respond("⏳ Looking into that, give me a moment...")
        blocks, intent = _run_pipeline(natural_language_query)
        respond(blocks=blocks, text=f"Finance report: {intent}", replace_original=True)
    except Exception as e:
        logger.error(f"Slash command error: {e}")
        respond(text="Something went wrong. Please try again.", replace_original=True)


def process_slash(respond, natural_language_query: str):
    """
    Dispatch slash command pipeline to background thread.
    Caller must ack() before calling this.
    """
    threading.Thread(
        target=_slash_worker,
        args=(respond, natural_language_query),
        daemon=True,
    ).start()


def _ensure_cache_loaded():
    """Load entity cache synchronously if not yet loaded. Handles startup race condition."""
    from qb_interpreter import _entity_cache, warm_cache
    if not _entity_cache.get("loaded") or not _entity_cache.get("vendors"):
        logger.info("Entity cache empty at query time — loading synchronously...")
        warm_cache()


def _get_vendor_matches(term: str) -> list[str]:
    """
    Return vendor names matching the search term from the cache.
    Ensures cache is loaded before matching.
    Delegates to Haiku for fuzzy matching — handles shorthand, typos, abbreviations.
    """
    from qb_interpreter import _entity_cache, _resolve_vendor_name
    _ensure_cache_loaded()
    vendors = _entity_cache.get("vendors", [])
    if not vendors or not term:
        return []
    matches = _resolve_vendor_name(term, vendors)
    return matches or []


def _get_customer_matches(term: str) -> list[str]:
    """
    Return customer names matching the search term from the cache.
    Ensures cache is loaded before matching.
    """
    from qb_interpreter import _entity_cache, _resolve_customer_name
    _ensure_cache_loaded()
    customers = _entity_cache.get("customers", [])
    if not customers or not term:
        return []
    matches = _resolve_customer_name(term, customers)
    return matches or []


def _clarification_blocks(term: str, matches: list[str], pending_query_template: str, entity_type: str = "vendor") -> list:
    """
    Build Slack Block Kit clarification message with buttons.
    entity_type: "vendor" or "customer" — used in the question text.
    pending_query_template has {name} placeholder replaced per button.
    """
    label = "vendors" if entity_type == "vendor" else "customers"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f'🔍 Found {len(matches)} {label} matching *"{term}"* — which one did you mean?'
            }
        },
        {"type": "actions", "elements": []}
    ]
    for name in matches[:3]:  # max 3 buttons
        blocks[1]["elements"].append({
            "type": "button",
            "text": {"type": "plain_text", "text": name},
            "value": pending_query_template.replace("{name}", name),
            "action_id": f"clarify_{entity_type}_{name[:20].replace(' ', '_')}",
        })
    return blocks


# ─── Slash Command Handlers ───────────────────────────────────────────────

@slack_app.command("/nb-bills")
def handle_bills(ack, respond, command):
    """
    /bills [vendor or all] [period]
    Default: all vendors, past 3 months
    Examples:
      /bills                            → all vendors, past 3 months
      /bills S And E past 6 months      → specific vendor drill-down
      /bills top 5 last quarter         → top 5 by spend
      /bills others past 3 months       → Others bucket only
    """
    ack()
    text = (command.get("text") or "").strip()

    if not text:
        query = "show me all bills for all vendors past 3 months"
        process_slash(respond=respond, natural_language_query=query)
        return

    # Check if this is an aggregate query (no specific vendor)
    lower = text.lower()
    is_aggregate = any(w in lower for w in ["all", "top", "others", "every", "customer", "customers"])

    if not is_aggregate:
        # Extract potential vendor name (words before time words)
        time_words = ["past", "last", "this", "since", "from", "in", "for"]
        words = text.split()
        vendor_words = []
        for w in words:
            if w.lower() in time_words:
                break
            vendor_words.append(w)
        vendor_term = " ".join(vendor_words).strip()

        if vendor_term and vendor_term.lower() not in ["all", "top", "others"]:
            matches = _get_vendor_matches(vendor_term)
            if len(matches) == 0:
                # No match — show vendor list
                from qb_interpreter import _entity_cache
                vendor_list = _entity_cache.get("vendors", [])
                vendor_text = "\n".join(f"• {v}" for v in vendor_list[:20])
                respond(f"❓ No vendors found matching *\"{vendor_term}\"*.\n\nYour vendors:\n{vendor_text}")
                return
            elif len(matches) > 1:
                # Ambiguous — show clarification buttons
                period = text[len(vendor_term):].strip() or "past 3 months"
                template = f"show me all bills for {{name}} {period}"
                respond(blocks=_clarification_blocks(vendor_term, matches, template),
                        text=f"Multiple vendors match '{vendor_term}'")
                return
            # else: single confident match — fall through with resolved name
            text = f"{matches[0]} {text[len(vendor_term):].strip()}"

    query = f"show me all bills for {text}" if text else "show me all bills past 3 months"
    process_slash(respond=respond, natural_language_query=query)


@slack_app.command("/nb-invoices")
def handle_invoices(ack, respond, command):
    """
    /nb-invoices [customer or all] [period]
    Default: all customers, past 3 months
    Examples:
      /nb-invoices                          → all customers, past 3 months
      /nb-invoices Northstar last quarter   → Northstar drill-down
      /nb-invoices all last quarter         → all customers listed
    """
    ack()
    text = (command.get("text") or "").strip()

    if not text:
        process_slash(respond=respond, natural_language_query="show me all invoices for all customers past 3 months")
        return

    lower = text.lower()
    # Treat "vendors", "vendor", "all", "top", "every" as aggregate — no specific customer
    is_aggregate = any(w in lower for w in ["all", "top", "every", "vendor", "vendors"])

    if is_aggregate:
        # Strip any mention of "vendors"/"vendor" — invoices are always for customers
        clean = text.lower().replace("vendors", "").replace("vendor", "").replace("all", "").strip()
        period = clean or "past 3 months"
        process_slash(respond=respond, natural_language_query=f"show me all invoices for all customers {period}")
        return

    # Specific customer name — extract term and resolve
    time_words = ["past", "last", "this", "since", "from", "in", "for"]
    words = text.split()
    customer_words = []
    for w in words:
        if w.lower() in time_words:
            break
        customer_words.append(w)
    customer_term = " ".join(customer_words).strip()

    if customer_term:
        matches = _get_customer_matches(customer_term)
        if len(matches) == 0:
            from qb_interpreter import _entity_cache
            customer_list = _entity_cache.get("customers", [])
            customer_text = "\n".join(f"• {c}" for c in customer_list[:20])
            respond(f"❓ No customers found matching *\"{customer_term}\"*.\n\nYour customers:\n{customer_text}")
            return
        elif len(matches) > 1:
            period = text[len(customer_term):].strip() or "past 3 months"
            template = f"show me all invoices for {{name}} {period}"
            respond(blocks=_clarification_blocks(customer_term, matches, template, entity_type="customer"),
                    text=f"Multiple customers match '{customer_term}'")
            return
        # Single match — rewrite with exact QB name
        text = f"{matches[0]} {text[len(customer_term):].strip()}"

    process_slash(respond=respond, natural_language_query=f"show me all invoices for {text}")


@slack_app.command("/nb-vendors")
def handle_vendors(ack, respond, command):
    """
    /vendors [period]
    Always aggregate — all vendors ranked by total billed.
    Default: past 3 months
    """
    ack()
    text = (command.get("text") or "").strip()
    period = text or "past 3 months"
    query = f"show me all vendors ranked by total billed amount {period}"
    process_slash(respond=respond, natural_language_query=query)


@slack_app.command("/nb-summary")
def handle_summary(ack, respond, command):
    """
    /summary [period]
    Top-level P&L grid: Hosting / Mining / Others / Total.
    Default: last completed month
    """
    ack()
    text = (command.get("text") or "").strip()
    period = text or "last month"
    query = f"give me a financial summary split by hosting mining and others for {period}"
    process_slash(respond=respond, natural_language_query=query)


@slack_app.command("/nb-balance")
def handle_balance(ack, respond, command):
    """
    /balance
    Balance sheet as of today. No params.
    """
    ack()
    process_slash(respond=respond,
                  natural_language_query="show me the balance sheet as of today")


@slack_app.command("/nb-pnl")
def handle_pnl(ack, respond, command):
    """
    /pnl [hosting | mining | others | all] [period]
    Full P&L by business line with accrual flagging.
    Default: all lines, last completed month
    Examples:
      /pnl                          → all lines, last month
      /pnl hosting last quarter     → hosting only
      /pnl mining last quarter      → mining only
      /pnl others past 3 months     → others expanded by account
      /pnl all last quarter         → all three lines + combined
    """
    ack()
    text = (command.get("text") or "").strip()

    if not text:
        query = "show me the full P&L for all business lines last month with accrual breakdown"
    else:
        lower = text.lower()
        if lower.startswith("hosting"):
            period = text[7:].strip() or "last month"
            query = f"show me the hosting P&L for {period} with accrual breakdown"
        elif lower.startswith("mining"):
            period = text[6:].strip() or "last month"
            query = f"show me the mining P&L for {period} with accrual breakdown"
        elif lower.startswith("others"):
            period = text[6:].strip() or "last month"
            query = f"show me the others P&L breakdown by account category for {period}"
        else:
            query = f"show me the full P&L for all business lines {text} with accrual breakdown"

    process_slash(respond=respond, natural_language_query=query)


@slack_app.command("/nb-finance")
def handle_finance(ack, respond, command):
    """
    /finance [anything]
    Free-form natural language. Catch-all for any financial question.
    """
    ack()
    text = (command.get("text") or "").strip()
    if not text:
        respond("Ask me anything — e.g. `/finance what's our cash position` or `/finance how did we do last quarter`")
        return
    process_slash(respond=respond, natural_language_query=text)


# ─── Clarification Button Handler ─────────────────────────────────────────

@slack_app.action(re.compile(r"clarify_(vendor|customer)_.+"))
def handle_clarification(ack, body, respond):
    """
    Handles button taps from clarification messages.
    The button value contains the fully-formed natural language query.
    """
    ack()
    query = body.get("actions", [{}])[0].get("value", "")
    if not query:
        respond("Something went wrong — please try again.", replace_original=True)
        return
    logger.info(f"Clarification selected: '{query}'")
    respond("⏳ Got it, looking that up...", replace_original=True)
    # thinking_done=True — caller already posted the thinking message above,
    # so _slash_worker skips its own respond("⏳...") to avoid an orphaned message.
    threading.Thread(
        target=_slash_worker,
        args=(respond, query, True),
        daemon=True,
    ).start()


# ─── @mention + DM Handlers ───────────────────────────────────────────────

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
    if not QB_API_KEY:
        return None
    if request.headers.get("X-API-Key", "") != QB_API_KEY:
        return jsonify({"error": "Unauthorised", "status": "error"}), 401
    return None


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "qb-slack-agent"})


@flask_app.route("/auth", methods=["GET"])
def auth():
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
    from flask import redirect
    return redirect(auth_url)


@flask_app.route("/callback", methods=["GET"])
def callback():
    import requests as req
    from requests.auth import HTTPBasicAuth
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"<h2>❌ Authorization failed: {error}</h2>", 400
    if not code:
        return "<h2>❌ No authorization code received.</h2>", 400
    try:
        response = req.post(
            "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
            auth=HTTPBasicAuth(Config.QB_CLIENT_ID, Config.QB_CLIENT_SECRET),
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": Config.QB_REDIRECT_URI},
        )
        if response.status_code != 200:
            logger.error(f"Token exchange failed: {response.status_code} — {response.text}")
            return "<h2>❌ Token exchange failed. Please try authorizing again.</h2>", 400
        tokens = response.json()
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]
        import qb_agent
        qb_agent._token_manager.access_token = access_token
        qb_agent._token_manager.refresh_token = refresh_token
        qb_agent._token_manager.expires_at = time.time() + tokens.get("expires_in", 3600)
        logger.info("✅ In-memory tokens updated.")
        threading.Thread(target=qb_agent._persist_tokens_to_railway, args=(access_token, refresh_token), daemon=True).start()
        # Refresh entity cache with new valid tokens
        threading.Thread(target=refresh_entity_cache, daemon=True).start()
        return """
        <h2>✅ QuickBooks Authorization Successful!</h2>
        <p>Tokens saved to Railway. The finance agent is now connected to QuickBooks.</p>
        <p>You can close this window.</p>
        """, 200
    except Exception as e:
        logger.error(f"Callback error: {e}")
        return "<h2>❌ Authorization error. Please try again or contact your admin.</h2>", 500


@flask_app.route("/auth-status", methods=["GET"])
def auth_status():
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
    from qb_interpreter import _entity_cache
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
        "entity_cache": {
            "loaded": _entity_cache.get("loaded", False),
            "vendor_count": len(_entity_cache.get("vendors", [])),
            "customer_count": len(_entity_cache.get("customers", [])),
            "age_seconds": int(time.time() - _entity_cache.get("loaded_at", 0)),
        },
        "action": "Visit /auth to re-authorize QuickBooks" if expires_in < 0 else "OK"
    })


@flask_app.route("/query", methods=["POST"])
def query():
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
        return jsonify({"error": "Internal error processing query.", "status": "error"}), 500


# ── Startup ────────────────────────────────────────────────────────────────

def _startup_tasks():
    """
    Run on startup in a background thread:
    1. Check QB token health — logs warning if broken, tells admin what to do
    2. Warm entity cache (vendor + customer lists)
    3. Schedule 24h cache refresh
    """
    import qb_agent as _qb_agent

    # Step 1 — Token health check
    logger.info("🔍 Checking QB token health...")
    health = _qb_agent.check_token_health()
    if not health["healthy"]:
        logger.error("❌ QB token is broken at startup — queries will fail until re-authorized.")
        logger.error("👉 Visit https://qb-slack-agent-production.up.railway.app/auth to fix.")
    else:
        # Step 2 — Warm entity cache (only if token is healthy)
        warm_cache()

        # Step 3 — Schedule 24h cache refresh
        def _refresh_loop():
            while True:
                time.sleep(86400)  # 24 hours
                logger.info("⏰ 24h cache refresh triggered")
                try:
                    warm_cache()
                except Exception as e:
                    logger.error(f"Scheduled cache refresh failed: {e}")

        threading.Thread(target=_refresh_loop, daemon=True).start()


if __name__ == "__main__":
    import sys
    logger.info("⚡ QB Slack Agent is starting...")

    port = int(os.environ.get("PORT", 3000))
    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    logger.info(f"🌐 HTTP API listening on port {port}")

    # Run startup tasks in background (token check + cache warm)
    threading.Thread(target=_startup_tasks, daemon=True).start()

    # Brief pause — Railway's network stack (DNS, routing) takes a few seconds
    # to fully initialise after container start. Without this, auth.test times out.
    time.sleep(8)

    # Retry loop: if auth.test fails on first attempt (transient Railway network blip),
    # wait and retry rather than crashing and triggering a Railway restart loop.
    MAX_SOCKET_ATTEMPTS = 5
    for attempt in range(1, MAX_SOCKET_ATTEMPTS + 1):
        try:
            logger.info(f"Connecting to Slack Socket Mode (attempt {attempt}/{MAX_SOCKET_ATTEMPTS})...")
            handler = SocketModeHandler(slack_app, Config.SLACK_APP_TOKEN)
            handler.start()  # blocks until disconnected; Bolt handles reconnects internally
            break
        except KeyboardInterrupt:
            logger.info("Shutting down.")
            sys.exit(0)
        except Exception as e:
            if attempt < MAX_SOCKET_ATTEMPTS:
                wait = min(15 * attempt, 60)
                logger.error(f"Socket Mode connection failed (attempt {attempt}): {e}. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"Socket Mode failed after {MAX_SOCKET_ATTEMPTS} attempts. Exiting.")
                sys.exit(1)