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

### QB Data Has Two Layers — Understanding This is Critical

**Layer 1 — Summary totals (Report API)**
ProfitAndLoss returns expense category totals only. Example:
  Utilities → MYR 300,864
It does NOT tell you which vendor that money went to. No payee names, no individual transactions.

**Layer 2 — Transaction detail (Query API)**
Bill and Purchase entities contain the individual transactions with vendor names.
But they do NOT contain category labels like "Utilities" or "Rent".

### When to Chain Both Layers
If the question asks for BOTH an expense category total AND vendor/payee detail, plan TWO calls:
1. ProfitAndLoss → get the category total and date range
2. SELECT * FROM Bill WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100
   → fetch ALL bills for the period, analyst will match line items to the expense category

Chain examples:
- "breakdown of utilities — who are the payees?" → P&L + all Bills for period
- "show electricity vendors this quarter" → P&L + all Bills for period
- "who do we pay for rent?" → P&L + all Bills for period
- "top expense vendors last month" → P&L for totals + all Bills for period

Do NOT chain if the question only asks for totals/summary (no vendor detail requested).

### Report API (Layer 1 — summaries)
- ProfitAndLoss — params: start_date, end_date (YYYY-MM-DD)
  → Returns expense category totals: Utilities, Rent, Salaries, COGS etc.
  → ONLY way to get expense category data
- BalanceSheet — params: date (YYYY-MM-DD)
- AgedReceivables — no params required
- AgedPayables — no params required
- CashFlow — params: start_date, end_date

### Query API — SQL-style (Layer 2 — individual records)
**Bill** — vendor bills the company owes or has paid
  - VendorRef.name, TxnDate, DueDate, TotalAmt, Balance, AccountRef.name
  - Filter by account: WHERE AccountRef.name LIKE '%Utilities%'
  - Filter by date: WHERE TxnDate >= '2026-01-01' AND TxnDate <= '2026-03-31'

**Invoice** — money owed TO the company by customers
  - CustomerRef.name, TxnDate, DueDate, TotalAmt, Balance

**Purchase** — outgoing payment records (bank transfers, card payments)
  - Use ONLY for: "show me payments to vendor X", specific payment lookups
  - Do NOT use for expense category analysis

**Vendor** / **Customer** / **Account** — entity lookups

### QB SQL — What is and isn't filterable on Bill

Bill HEADER fields (safe to filter on):
  VendorRef.name, TxnDate, DueDate, TotalAmt, Balance, DocNumber

Bill LINE ITEM fields (NOT filterable — causes 400 error):
  AccountRef.name ← NEVER use this as a WHERE filter

CRITICAL: You CANNOT filter Bills by account name in SQL. It lives on line items, not the header.

CORRECT approach for vendor/payee breakdown by expense category:
  Fetch ALL bills for the date range with no account filter:
  SELECT * FROM Bill WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100

Then the analyst will identify which bills relate to the expense category from the line item data returned.

NEVER generate: WHERE AccountRef.name LIKE '%anything%' — this always returns 400 Bad Request.

### Date format: YYYY-MM-DD
"""

# ─── Step 1: Retrieval Planner ────────────────────────────────────────

RETRIEVAL_SYSTEM = f"""You are a QuickBooks query generator for a Bitcoin mining company.

Today is {TODAY}.

{QB_SCHEMA}

Generate a JSON plan for fetching the data needed to answer the question.

DECISION RULES:
1. Expense category total only (no vendor detail asked) → ProfitAndLoss report only
2. Expense category + vendor/payee detail → Chain: ProfitAndLoss THEN Bill query on that account
3. Balance sheet / financial position → BalanceSheet report
4. Who owes us / outstanding invoices → AgedReceivables report
5. What we owe / upcoming bills → AgedPayables report
6. Specific vendor bills by name → SQL query on Bill with VendorRef filter
7. Specific customer invoices by name → SQL query on Invoice
8. Cash / bank balances → BalanceSheet report
9. Top expense vendors / who do we pay for X → Chain: ProfitAndLoss THEN Bill query

CHAINING RULE:
When question asks who/which vendor/payee is behind an expense category, always chain:
  Call 1: ProfitAndLoss for the date range (gets the category total)
  Call 2: SELECT * FROM Bill WHERE TxnDate >= '<start>' AND TxnDate <= '<end>' ORDERBY TxnDate DESC MAXRESULTS 100
          (fetch ALL bills — analyst will identify which relate to the category)

DO NOT filter by AccountRef.name — that field is on line items, not the Bill header.
Filtering by it causes a 400 Bad Request error.

Examples of chained questions:
- "utilities breakdown — who are the payees" → P&L + all Bills for that date range
- "who do we pay for electricity" → P&L + all Bills for that date range  
- "rent vendors" → P&L + all Bills for that date range
- "top expense vendors last month" → P&L for last month + all Bills last month

DEFAULT DATE RULE:
If no date or period is specified, default to the current calendar month:
  start_date = first day of current month
  end_date = today ({TODAY_ISO})
For balance sheet with no date specified, use today.
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

Today is {TODAY}. Today's date in ISO format: {TODAY_ISO}.

{QB_SCHEMA}

The user wants trend analysis or forecasting. This ALWAYS requires ProfitAndLoss reports
across multiple months. Never use SQL queries for trend/forecast questions.

Generate a plan that fetches ProfitAndLoss for each relevant month separately.
This gives the analyst a proper time series to work with.

DYNAMIC DATE CALCULATION — always calculate from today ({TODAY_ISO}):
- "past 3 months" = the 3 most recently COMPLETED calendar months before today
- "past 6 months" = the 6 most recently COMPLETED calendar months before today
- "forecast this month" = past 3 completed months
- "cashflow next 30 days" = past 3 completed months + AgedPayables
- Never include the current partial month as a completed month

To calculate: take today's month, go back N months, use exact first/last day of each month.
Example if today is March 10 2026:
  Past 3 months = Feb 2026 (2026-02-01 to 2026-02-28), Jan 2026 (2026-01-01 to 2026-01-31), Dec 2025 (2025-12-01 to 2025-12-31)
  Past 6 months = Feb, Jan, Dec, Nov, Oct, Sep

Always use exact calendar month boundaries. February ends on 28 (non-leap) or 29 (leap year).

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
                data = qb_agent.get_report(call["report_name"], params)
                results.append({"call": call, "data": data, "error": None})
            elif call["type"] == "query":
                data = qb_agent.query(call["sql"])
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
