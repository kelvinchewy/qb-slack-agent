"""
QB Slack Agent — Main entry point.
Slack Bolt app using Socket Mode.
Listens for @mentions and DMs, routes through orchestrator → report builder.
"""

import logging
import re
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


def handle_query(message_text: str, say, thread_ts: str = None):
    clean_text = strip_mention(message_text)

    if not clean_text:
        say(
            text="Hey! Ask me a financial question, or type *help* to see what I can do.",
            thread_ts=thread_ts,
        )
        return

    logger.info(f"Processing query: '{clean_text}'")

    # Step 1: Classify intent — fixed or dynamic route
    intent_data = classify_intent(clean_text)

    # Pass original question through so dynamic pipeline can use it
    intent_data["original_question"] = clean_text

    logger.info(f"Route: {intent_data.get('route')} | Intent: {intent_data.get('intent')}")

    # Step 2: Build report
    blocks = build_report(intent_data)

    # Step 3: Send to Slack
    say(
        blocks=blocks,
        text=f"Finance report: {intent_data.get('intent', 'query')}",
        thread_ts=thread_ts,
    )


@app.event("app_mention")
def handle_mention(event, say):
    logger.info(f"Mention from {event.get('user')}: {event.get('text')}")
    handle_query(
        message_text=event.get("text", ""),
        say=say,
        thread_ts=event.get("ts"),
    )


@app.event("message")
def handle_dm(event, say):
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel_type") == "im":
        logger.info(f"DM from {event.get('user')}: {event.get('text')}")
        handle_query(message_text=event.get("text", ""), say=say)


if __name__ == "__main__":
    logger.info("⚡ QB Slack Agent is starting...")
    handler = SocketModeHandler(app, Config.SLACK_APP_TOKEN)
    handler.start()
