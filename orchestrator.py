"""
Orchestrator — Classifies intent for routing.
All financial queries now go through the dynamic pipeline.
Fixed routing removed — Claude handles everything via natural language.
"""

import logging

logger = logging.getLogger(__name__)


def classify_intent(user_message: str) -> dict:
    """
    All queries route to dynamic pipeline.
    Returns structured dict with route=dynamic.
    """
    msg = user_message.strip().lower()

    # Help: only match if the message IS one of these — not if it contains them
    help_triggers = {"help", "what can you do", "commands", "how do i use this", "how do i use you"}
    if msg in help_triggers or msg.startswith("help ") and len(msg) < 20:
        return {"route": "fixed", "intent": "help"}

    # Everything else goes dynamic
    logger.info(f"Routing to dynamic pipeline: '{user_message}'")
    return {"route": "dynamic", "intent": "dynamic"}


def get_help_text() -> str:
    return (
        "Just ask me anything about your finances in plain English:\n\n"
        "📊 *Reports* — \"Balance sheet\", \"P&L for Q1\", \"How did we do last quarter?\"\n"
        "💰 *Cash* — \"What's our cash position?\", \"What bills are due?\"\n"
        "🔍 *Vendor lookups* — \"Show me all bills from PowerGrid this year\"\n"
        "📥 *AR/AP* — \"Who owes us money?\", \"What do we owe vendors?\"\n"
        "⚠️ *Analysis* — \"Anything I should be worried about?\", \"Unusual expenses lately?\"\n"
        "📈 *Forecasting* — \"Cashflow next 30 days?\", \"Upcoming large bills?\"\n\n"
        "No commands needed — just ask naturally and I'll figure out what to pull from QuickBooks."
    )
