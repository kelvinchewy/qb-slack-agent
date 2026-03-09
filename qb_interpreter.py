"""
qb_interpreter.py — Natural language → QuickBooks query execution.

Takes any plain English financial question, reasons about what QB data
is needed, executes the right API call or SQL query, and returns
structured raw data + metadata for the analyst layer.

Called by report_builder.py for any dynamic (non-fixed) query.
"""

import json
import logging
from datetime import datetime
import anthropic
from config import Config
import qb_agent

logger = logging.getLogger(__name__)

# Today's date injected into prompts so Claude reasons correctly about periods
TODAY = datetime.now().strftime("%B %d, %Y")
CURRENT_YEAR = datetime.now().year
CURRENT_MONTH = datetime.now().strftime("%B")

# ─── QB Schema Knowledge ──────────────────────────────────────────────
# Claude needs to know what's queryable in QB to generate correct calls.

QB_SCHEMA = """
## QuickBooks Online — Available Data

### Report API (pre-built reports, use for summaries)
- ProfitAndLoss — params: start_date, end_date (YYYY-MM-DD)
- BalanceSheet — params: date (YYYY-MM-DD)
- AgedReceivables — params: none required
- AgedPayables — params: none required
- CashFlow — params: start_date, end_date
- GeneralLedger — params: start_date, end_date

### Query API (SQL-style, use for specific lookups)
Entities and their key fields:

**Invoice** (money owed TO the company)
  - Id, DocNumber, TxnDate, DueDate, TotalAmt, Balance
  - CustomerRef.name (customer name)
  - Line items, LinkedTxn

**Bill** (money the company OWES to vendors)
  - Id, DocNumber, TxnDate, DueDate, TotalAmt, Balance
  - VendorRef.name (vendor name)
  - APAccountRef, Line items

**Vendor**
  - Id, DisplayName, Balance, CurrencyRef

**Customer**
  - Id, DisplayName, Balance, CurrencyRef

**Account**
  - Id, Name, AccountType (Bank, Accounts Receivable, Accounts Payable, 
    Income, Cost of Goods Sold, Expense, Fixed Asset, Equity, etc.)
  - CurrentBalance, Active

**Purchase** (expenses/purchases made by company)
  - Id, TxnDate, TotalAmt, PaymentType
  - AccountRef, EntityRef (vendor)
  - Line items with AccountBasedExpenseLineDetail

**Payment** (customer payments received)
  - Id, TxnDate, TotalAmt, CustomerRef.name

**Deposit**
  - Id, TxnDate, TotalAmt, DepositToAccountRef

**JournalEntry**
  - Id, TxnDate, Adjustment

### Query Syntax
Standard SQL SELECT with these operators:
- WHERE field = 'value' (exact match, use single quotes)
- WHERE field > 'value' or WHERE field < 'value'
- WHERE field LIKE '%partial%'
- AND, OR operators
- ORDER BY field ASC/DESC
- MAXRESULTS N (max 1000)
- STARTPOSITION N (for pagination)

Date format in queries: YYYY-MM-DD (e.g. '2026-01-01')

### Example Queries
- All bills from a vendor: SELECT * FROM Bill WHERE VendorRef.name = 'PowerGrid Energy'
- Unpaid bills: SELECT * FROM Bill WHERE Balance > '0' ORDER BY DueDate ASC
- Bills due this month: SELECT * FROM Bill WHERE DueDate >= '2026-03-01' AND DueDate <= '2026-03-31'
- Large invoices: SELECT * FROM Invoice WHERE TotalAmt > '10000' ORDER BY TotalAmt DESC
- Bank accounts: SELECT * FROM Account WHERE AccountType = 'Bank' AND Active = true
- Vendor spend: SELECT * FROM Purchase WHERE TxnDate >= '2026-01-01' ORDER BY TotalAmt DESC MAXRESULTS 50
- Customer payments: SELECT * FROM Payment WHERE TxnDate >= '2026-01-01'
"""

INTERPRETER_SYSTEM = f"""You are a QuickBooks query generator for a Bitcoin mining company called The Hashing Company.

Today is {TODAY}. The company runs ~200 ASIC mining machines across 2 sites.
Their main costs are electricity, facility leases, and equipment. Revenue is primarily BTC mining.

{QB_SCHEMA}

Given a user's financial question, output a JSON object describing what QB data to fetch.

You must decide:
1. **call_type**: "report" (pre-built QB report) or "query" (SQL-style entity query) or "multi" (need both)
2. **calls**: list of API calls to make
3. **query_complexity**: "simple" (single number/summary) or "detail" (list of items, breakdown, extraction)
4. **reasoning**: brief note on why you chose this approach

For "report" calls:
{{"type": "report", "report_name": "ProfitAndLoss", "params": {{"start_date": "2026-01-01", "end_date": "2026-03-31"}}}}

For "query" calls:
{{"type": "query", "sql": "SELECT * FROM Bill WHERE VendorRef.name = 'PowerGrid Energy' ORDER BY TxnDate DESC"}}

Respond ONLY with valid JSON. No markdown, no backticks, no explanation outside the JSON.

Example output:
{{
  "call_type": "query",
  "calls": [
    {{"type": "query", "sql": "SELECT * FROM Bill WHERE VendorRef.name = 'PowerGrid Energy' ORDER BY TxnDate DESC MAXRESULTS 100"}}
  ],
  "query_complexity": "detail",
  "reasoning": "User wants specific vendor bills - entity query is more precise than a report"
}}

Another example (multi):
{{
  "call_type": "multi", 
  "calls": [
    {{"type": "report", "report_name": "AgedPayables", "params": {{}}}},
    {{"type": "query", "sql": "SELECT * FROM Bill WHERE Balance > '0' ORDER BY DueDate ASC MAXRESULTS 20"}}
  ],
  "query_complexity": "detail",
  "reasoning": "Cash flow question needs both aging summary and specific upcoming bills"
}}
"""


# ─── Interpreter ──────────────────────────────────────────────────────

def interpret_and_fetch(user_question: str) -> dict:
    """
    Main entry point.
    Takes a natural language question, generates QB calls, executes them,
    returns raw data + metadata for qb_analyst.

    Returns:
        {
            "question": str,
            "query_complexity": "simple" | "detail",
            "reasoning": str,
            "results": [ {"call": {...}, "data": {...}}, ... ],
            "error": str | None
        }
    """
    logger.info(f"Interpreting query: '{user_question}'")

    # Step 1: Ask Claude what QB calls to make
    try:
        client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=INTERPRETER_SYSTEM,
            messages=[{"role": "user", "content": user_question}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        plan = json.loads(raw)
        logger.info(f"Query plan: {json.dumps(plan, indent=2)}")
    except Exception as e:
        logger.error(f"Interpreter planning error: {e}")
        return {
            "question": user_question,
            "query_complexity": "simple",
            "reasoning": "Planning failed",
            "results": [],
            "error": f"Couldn't figure out how to query QuickBooks for that. Try rephrasing.",
        }

    # Step 2: Execute each call
    results = []
    for call in plan.get("calls", []):
        try:
            if call["type"] == "report":
                data = qb_agent._client.get_report(
                    call["report_name"],
                    {**call.get("params", {}), "minorversion": "65"}
                )
                results.append({"call": call, "data": data, "error": None})

            elif call["type"] == "query":
                data = qb_agent._client.query(call["sql"])
                results.append({"call": call, "data": data, "error": None})

        except Exception as e:
            logger.error(f"QB call failed: {call} — {e}")
            results.append({"call": call, "data": None, "error": str(e)})

    return {
        "question": user_question,
        "query_complexity": plan.get("query_complexity", "simple"),
        "reasoning": plan.get("reasoning", ""),
        "results": results,
        "error": None,
    }
