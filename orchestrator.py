"""
Orchestrator — Intent classification and parameter extraction via Claude.
Takes a raw user message and returns structured intent + parameters.
"""

import json
import logging
import anthropic
from config import Config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an intent classifier for a financial reporting Slack bot at a Bitcoin mining company.

Given a user message, extract:
1. The intent (what report they want)
2. Parameters (time period, comparison, etc.)

Respond ONLY with a JSON object, no markdown, no backticks, no explanation.

Valid intents:
- "quarterly_summary" — quarterly financial summary / how did we do
- "balance_sheet" — balance sheet / financial position / assets & liabilities
- "pnl" — profit and loss / income statement for a specific period
- "cash_position" — cash balances / bank accounts / liquidity
- "ar_aging" — accounts receivable / who owes us / outstanding invoices
- "ap_aging" — accounts payable / what we owe / upcoming bills
- "help" — user asking what the bot can do
- "unknown" — can't determine intent

For time periods:
- Detect quarter references: "Q4", "last quarter", "this quarter", "Q1 2025"
- Detect month references: "January", "last month", "this month"  
- Detect year references: "2025", "this year", "YTD"
- Default to most recent completed quarter if no period specified for quarterly_summary
- Default to today for balance_sheet if no date specified

For comparisons:
- "compare Q3 vs Q4", "vs last quarter", "compared to last year"
- Set comparison=true and extract comparison_period

Current date context: March 2026. Most recent completed quarter is Q4 2025.

Example outputs:

User: "How did we do last quarter?"
{"intent": "quarterly_summary", "quarter": 4, "year": 2025, "comparison": true, "comparison_quarter": 3, "comparison_year": 2025}

User: "Balance sheet"
{"intent": "balance_sheet", "as_of_date": "2026-03-06"}

User: "P&L for January"
{"intent": "pnl", "start_date": "2026-01-01", "end_date": "2026-01-31", "comparison": false}

User: "What can you help me with?"
{"intent": "help"}

User: "What's the weather like?"
{"intent": "unknown", "message": "I can help with financial reports, not weather."}
"""


def classify_intent(user_message: str) -> dict:
    """
    Sends user message to Claude for intent classification.
    Returns structured dict with intent and parameters.
    """
    try:
        client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_text = response.content[0].text.strip()
        # Clean any accidental markdown wrapping
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        result = json.loads(raw_text)
        logger.info(f"Intent classified: {result.get('intent')} for message: '{user_message}'")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Claude response as JSON: {e}, raw: {raw_text}")
        return {"intent": "unknown", "message": "I had trouble understanding that. Could you rephrase?"}
    except Exception as e:
        logger.error(f"Orchestrator error: {e}")
        return {"intent": "unknown", "message": "Something went wrong on my end. Please try again."}


def get_help_text() -> str:
    """Returns help message listing available commands."""
    return (
        "Here's what I can help you with:\n\n"
        "📊 *Quarterly Summary* — \"How did we do in Q4?\" or \"Quarterly report\"\n"
        "📋 *Balance Sheet* — \"Show me the balance sheet\" or \"Financial position\"\n"
        "📈 *P&L* — \"P&L for January\" or \"Compare Q3 vs Q4\"\n"
        "💰 *Cash Position* — \"What's our cash position?\" or \"Bank balances\"\n"
        "📥 *AR Aging* — \"Who owes us money?\" or \"Outstanding invoices\"\n"
        "📤 *AP Aging* — \"What bills are due?\" or \"What do we owe?\"\n\n"
        "Just ask in plain English — I'll pull the data from QuickBooks."
    )
