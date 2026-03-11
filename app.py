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
    Posts the result directly to the channel/thread.
    """
    try:
        clean_text = strip_mention(user_message)

        if not clean_text:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Hey! Ask me a financial question, or type *help* to see what I can do.",
            )
            return

        logger.info(f"Processing query: '{clean_text}'")

        intent_data = classify_intent(clean_text)
        intent_data["original_question"] = clean_text

        logger.info(f"Route: {intent_data.get('route')} | Intent: {intent_data.get('intent')}")

        blocks = build_report(intent_data)

        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            blocks=blocks,
            text=f"Finance report: {intent_data.get('intent', 'query')}",
        )

    except Exception as e:
        logger.error(f"Query processing error: {e}")
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Something went wrong. Please try again.",
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