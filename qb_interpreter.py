"""
qb_interpreter.py — Natural language → QuickBooks query execution.

Step 0: Classify intent as RETRIEVAL or FORECAST_TREND
Step 1: Generate QB API call plan based on classification
Step 2: Execute calls and return structured results

Key rule: expense categories ONLY exist in ProfitAndLoss report API.
Never use SQL Purchase queries to look for expense categories.
"""

import json
import logging
from datetime import datetime
import anthropic
from config import Config
import qb_agent

logger = logging.getLogger(__name__)

TODAY = datetime.now().strftime("%B %d, %Y")
TODAY_ISO = datetime.now().strftime("%Y-%m-%d")
CURRENT_YEAR = datetime.now().year
CURRENT_MONTH = datetime.now().strftime("%B")

# ─── Step 0: Classifier ───────────────────────────────────────────────

CLASSIFIER_SYSTEM = """You are a query classifier for a QuickBooks finance agent.

Classify the user's question into exactly one of:
- RETRIEVAL: looking up specific records, a point-in-time report, or a vendor/customer query
- FORECAST_TREND: asking about trends over time, historical patterns, forecasting future expenses

RETRIEVAL examples:
- "balance sheet"
- "who owes us money"
- "bills from PowerGrid"
- "what's our cash position"
- "show me unpaid invoices"
- "utility expenses for a specific month"

FORECAST_TREND examples:
- "forecast my spending this month"
- "top expenses last month using 3 months data"
- "utility expenses for the past 6 months"
- "how has our electricity cost changed"
- "cashflow next 30 days"
- "are our costs going up"
- ANY question asking for history across multiple months
- ANY question asking to forecast or project

Respond with ONLY one word: RETRIEVAL or FORECAST_TREND
"""

def _classify_intent(question: str) -> str:
    """Returns 'RETRIEVAL' or 'FORECAST_TREND'"""
    try:
        client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=10,
            system=CLASSIFIER_SYSTEM,
            messages=[{"role": "user", "content": question}],
        )
        result = response.content[0].text.strip().upper()
        if result not in ("RETRIEVAL", "FORECAST_TREND"):
            result = "RETRIEVAL"
        logger.info(f"Intent classified as: {result}")
        return result
    except Exception as e:
        logger.error(f"Classifier error: {e}")
        return "RETRIEVAL"


# ─── QB Schema Knowledge ──────────────────────────────────────────────

QB_SCHEMA = """
## QuickBooks Online — Available Data

### CRITICAL RULE — Expense Categories vs Transactions
Expense categories (Utilities, Rent, Salaries, Electricity, etc.) ONLY exist in the
ProfitAndLoss report. They are NEVER returned by SQL queries on Purchase, Bill, or
any other entity. If the user asks about an expense category or type, always use the
ProfitAndLoss report — never SQL.

SQL queries on Purchase only return manually-entered payment records (bank transfers,
vendor payments) — not categorised expense data.

### Report API (use for summaries and expense categories)
- ProfitAndLoss — params: start_date, end_date (YYYY-MM-DD)
  → Returns all income and expense categories including Utilities, Rent, Salaries, COGS etc.
  → This is the ONLY way to get expense category data
- BalanceSheet — params: date (YYYY-MM-DD)
- AgedReceivables — no params required
- AgedPayables — no params required
- CashFlow — params: start_date, end_date

### Query API (SQL-style — use ONLY for specific record lookups)
Use ONLY when looking for specific vendors, customers, invoices, or bills by name/ID.
NEVER use for expense category analysis.

**Bill** — money the company OWES vendors
  - VendorRef.name, TxnDate, DueDate, TotalAmt, Balance

**Invoice** — money OWED to the company
  - CustomerRef.name, TxnDate, DueDate, TotalAmt, Balance

**Purchase** — outgoing payment records only (NOT expense categories)
  - Use only for: "show me payments to vendor X", "bank transfers", specific payment lookup
  - Do NOT use for: utility expenses, rent, salaries, or any cost category

**Vendor** / **Customer** / **Account** — entity lookups

### Date format: YYYY-MM-DD
"""

# ─── Step 1: Retrieval Planner ────────────────────────────────────────

RETRIEVAL_SYSTEM = f"""You are a QuickBooks query generator for a Bitcoin mining company.

Today is {TODAY}.

{QB_SCHEMA}

Generate a JSON plan for fetching the data needed to answer the question.

DECISION RULES:
1. Expense category question (utilities, rent, electricity, any cost type) → ProfitAndLoss report
2. Balance sheet / financial position → BalanceSheet report
3. Who owes us / outstanding invoices → AgedReceivables report
4. What we owe / upcoming bills → AgedPayables report
5. Specific vendor bills by name → SQL query on Bill
6. Specific customer invoices by name → SQL query on Invoice
7. Cash / bank balances → BalanceSheet report

Output format:
{{
  "call_type": "report" | "query" | "multi",
  "calls": [
    {{"type": "report", "report_name": "ProfitAndLoss", "params": {{"start_date": "2026-02-01", "end_date": "2026-02-28"}}}},
    {{"type": "query", "sql": "SELECT * FROM Bill WHERE VendorRef.name = 'X'"}}
  ],
  "query_complexity": "simple" | "detail",
  "reasoning": "brief note"
}}

Respond ONLY with valid JSON. No markdown, no backticks.
"""

# ─── Step 1: Forecast/Trend Planner ──────────────────────────────────

FORECAST_SYSTEM = f"""You are a QuickBooks query generator for a Bitcoin mining company.

Today is {TODAY}. Current month start: first day of this month.

{QB_SCHEMA}

The user wants trend analysis or forecasting. This ALWAYS requires ProfitAndLoss reports
across multiple months. Never use SQL queries for trend/forecast questions.

Generate a plan that fetches ProfitAndLoss for each relevant month separately.
This gives the analyst a proper time series to work with.

For "past 3 months": fetch the 3 most recently completed months separately.
For "past 6 months": fetch the 6 most recently completed months separately.
For "forecast this month": fetch the past 3 completed months.
For "cashflow next 30 days": fetch past 3 months P&L + AgedPayables.

Month date ranges (use exact first/last day of each month):
- Feb 2026: 2026-02-01 to 2026-02-28
- Jan 2026: 2026-01-01 to 2026-01-31
- Dec 2025: 2025-12-01 to 2025-12-31
- Nov 2025: 2025-11-01 to 2025-11-30
- Oct 2025: 2025-10-01 to 2025-10-31
- Sep 2025: 2025-09-01 to 2025-09-30

Output format:
{{
  "call_type": "multi",
  "calls": [
    {{"type": "report", "report_name": "ProfitAndLoss", "params": {{"start_date": "2026-02-01", "end_date": "2026-02-28"}}}},
    {{"type": "report", "report_name": "ProfitAndLoss", "params": {{"start_date": "2026-01-01", "end_date": "2026-01-31"}}}},
    {{"type": "report", "report_name": "ProfitAndLoss", "params": {{"start_date": "2025-12-01", "end_date": "2025-12-31"}}}}
  ],
  "query_complexity": "detail",
  "intent": "forecast_trend",
  "reasoning": "brief note"
}}

Respond ONLY with valid JSON. No markdown, no backticks.
"""


def _plan_calls(question: str, intent: str) -> dict:
    """Generate QB API call plan based on classified intent."""
    system = FORECAST_SYSTEM if intent == "FORECAST_TREND" else RETRIEVAL_SYSTEM
    try:
        client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=system,
            messages=[{"role": "user", "content": question}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        plan = json.loads(raw)
        logger.info(f"Call plan: {json.dumps(plan, indent=2)}")
        return plan
    except Exception as e:
        logger.error(f"Planner error: {e}")
        return {"calls": [], "query_complexity": "simple", "error": str(e)}


# ─── Step 2: Execute ──────────────────────────────────────────────────

def _execute_calls(plan: dict) -> list:
    """Execute each API call in the plan and return results."""
    results = []
    for call in plan.get("calls", []):
        try:
            if call["type"] == "report":
                params = {**call.get("params", {}), "minorversion": "65"}
                data = qb_agent._client.get_report(call["report_name"], params)
                results.append({"call": call, "data": data, "error": None})
            elif call["type"] == "query":
                data = qb_agent._client.query(call["sql"])
                results.append({"call": call, "data": data, "error": None})
        except Exception as e:
            logger.error(f"QB call failed: {call} — {e}")
            results.append({"call": call, "data": None, "error": str(e)})
    return results


# ─── Main Entry Point ─────────────────────────────────────────────────

def interpret_and_fetch(user_question: str) -> dict:
    """
    Main entry point.

    Step 0: Classify intent (RETRIEVAL vs FORECAST_TREND)
    Step 1: Plan QB API calls based on classification
    Step 2: Execute calls
    Returns structured result for qb_analyst.
    """
    logger.info(f"Interpreting: '{user_question}'")

    # Step 0 — Classify
    intent = _classify_intent(user_question)

    # Step 1 — Plan
    plan = _plan_calls(user_question, intent)
    if "error" in plan and not plan.get("calls"):
        return {
            "question": user_question,
            "intent": intent,
            "query_complexity": "simple",
            "results": [],
            "error": "Couldn't figure out how to query QuickBooks for that. Try rephrasing.",
        }

    # Step 2 — Execute
    results = _execute_calls(plan)

    return {
        "question": user_question,
        "intent": intent,
        "query_complexity": plan.get("query_complexity", "simple"),
        "reasoning": plan.get("reasoning", ""),
        "results": results,
        "error": None,
    }