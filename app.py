"""
QB Slack Agent — Main entry point.
Slack Bolt app using Socket Mode.

Responds immediately to avoid Slack's 3-second timeout,
then processes the query and posts the result as a follow-up.
"""

import logging
import re
import threading
from flask import Flask, request, jsonify
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import Config
from orchestrator import classify_intent
from report_builder import build_report

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

app = App(token=Config.SLACK_BOT_TOKEN, signing_secret=Config.SLACK_SIGNING_SECRET)


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


@app.event("app_mention")
def handle_mention(event, ack, client):
    ack()  # Acknowledge immediately — avoids Slack 3s timeout
    thread_ts = event.get("ts")
    channel = event.get("channel")
    user_message = event.get("text", "")

    logger.info(f"Mention from {event.get('user')}: {user_message}")

    t = threading.Thread(
        target=process_and_reply,
        args=(client, channel, thread_ts, user_message),
        daemon=True,
    )
    t.start()


@app.event("message")
def handle_dm(event, ack, client):
    ack()  # Acknowledge immediately
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel_type") != "im":
        return

    channel = event.get("channel")
    user_message = event.get("text", "")

    logger.info(f"DM from {event.get('user')}: {user_message}")

    t = threading.Thread(
        target=process_and_reply,
        args=(client, channel, None, user_message),
        daemon=True,
    )
    t.start()


http_app = Flask(__name__)


@http_app.route("/query", methods=["POST"])
def query():
    data = request.json
    question = data.get("query", "")
    if not question:
        return jsonify({"error": "Missing 'query' field"}), 400
    try:
        from qb_interpreter import interpret_and_fetch
        from qb_analyst import analyse
        interpreter_result = interpret_and_fetch(question)
        analysis = analyse(interpreter_result)
        return jsonify({"answer": analysis})
    except Exception as e:
        logger.error(f"HTTP query error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    logger.info("⚡ QB Slack Agent is starting...")
    threading.Thread(
        target=lambda: http_app.run(port=3000, use_reloader=False),
        daemon=True,
    ).start()
    logger.info("🌐 HTTP endpoint listening on port 3000")
    handler = SocketModeHandler(app, Config.SLACK_APP_TOKEN)
    handler.start()
