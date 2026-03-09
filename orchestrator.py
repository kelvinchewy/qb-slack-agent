"""
Orchestrator — Routes queries to either:
  (A) Fixed report pipeline → report_builder.py
  (B) Dynamic query pipeline → qb_interpreter → qb_analyst

Fixed reports: quarterly summary, balance sheet, P&L, cash position, AR/AP, help
Dynamic: anything else — vendor lookups, custom extracts, forecasts, analysis questions
"""

import json
import logging
import anthropic
from config import Config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a router for a financial Slack bot at a Bitcoin mining company.

Given a user message, decide if it maps to a FIXED report or needs DYNAMIC querying.

FIXED reports (fast, pre-built):
- "quarterly_summary" — Q1/Q2/Q3/Q4 summary, "how did we do last quarter"
- "balance_sheet" — assets, liabilities, equity, financial position
- "pnl" — profit and loss for a specific month or period
- "cash_position" — cash balances, AR aging, AP aging, bank accounts
- "help" — what can you do, list of commands

DYNAMIC queries (anything more specific or analytical):
- Vendor-specific questions: "bills from PowerGrid", "how much did we pay to X"
- Custom extracts: "all invoices over $10k", "unpaid bills this month"
- Forecasting: "cashflow next 30 days", "what's coming up", "upcoming expenses"
- Analysis: "anything I should worry about", "unusual spend", "compare vendors"
- Any question that needs filtering, grouping, or reasoning beyond a standard report

Respond ONLY with JSON. No markdown, no backticks.

For FIXED reports:
{
  "route": "fixed",
  "intent": "quarterly_summary",
  "quarter": 4,
  "year": 2025,
  "comparison": true,
  "comparison_quarter": 3,
  "comparison_year": 2025
}

For DYNAMIC queries:
{
  "route": "dynamic",
  "intent": "dynamic"
}

For HELP:
{
  "route": "fixed",
  "intent": "help"
}

For UNKNOWN (can't determine at all):
{
  "route": "fixed",
  "intent": "unknown",
  "message": "I'm not sure what you're asking. Try: 'Q4 summary', 'balance sheet', or 'bills from [vendor name]'"
}

Current date context: March 2026. Most recent completed quarter: Q4 2025.

Fixed report examples:
"How did we do in Q4?" → quarterly_summary, quarter=4, year=2025
"Balance sheet" → balance_sheet, as_of_date=today
"P&L for January" → pnl, start_date=2026-01-01, end_date=2026-01-31
"Cash position" → cash_position
"What can you do?" → help

Dynamic examples:
"Show me all bills from PowerGrid this year" → dynamic
"What's our cashflow looking like next month?" → dynamic
"Any unusual expenses lately?" → dynamic
"Total spend on electricity in Q1" → dynamic
"Which vendors are we paying the most?" → dynamic
"Anything I should be worried about?" → dynamic
"""


def classify_intent(user_message: str) -> dict:
    """
    Routes the message to fixed or dynamic pipeline.
    Returns structured dict with 'route' key.
    """
    try:
        client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        result = json.loads(raw)
        logger.info(f"Route: {result.get('route')} | Intent: {result.get('intent')} | Message: '{user_message}'")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Orchestrator JSON parse error: {e}")
        return {"route": "dynamic", "intent": "dynamic"}
    except Exception as e:
        logger.error(f"Orchestrator error: {e}")
        return {"route": "fixed", "intent": "unknown", "message": "Something went wrong. Please try again."}


def get_help_text() -> str:
    return (
        "Here's what I can help you with:\n\n"
        "📊 *Quarterly Summary* — \"How did we do in Q4?\" or \"Quarterly report\"\n"
        "📋 *Balance Sheet* — \"Show me the balance sheet\" or \"Financial position\"\n"
        "📈 *P&L* — \"P&L for January\" or \"Compare Q3 vs Q4\"\n"
        "💰 *Cash Position* — \"What's our cash position?\" or \"Bank balances\"\n"
        "📥 *AR Aging* — \"Who owes us money?\" or \"Outstanding invoices\"\n"
        "📤 *AP Aging* — \"What bills are due?\" or \"What do we owe?\"\n\n"
        "*Or ask anything in plain English:*\n"
        "🔍 \"Show me all bills from PowerGrid this year\"\n"
        "🔍 \"What's our cashflow looking like next 30 days?\"\n"
        "🔍 \"Which vendors are we paying the most?\"\n"
        "🔍 \"Anything unusual in our spending lately?\"\n\n"
        "Just ask — I'll figure out what to pull from QuickBooks."
    )
