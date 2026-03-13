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

### TERMINOLOGY — "Invoice" vs "Bill"
Users often say "invoice" loosely. Interpret carefully:
- "show me expense invoices", "bills we received", "vendor invoices", "what we owe" → QB Bill entity (AP)
- "invoices we sent", "customer invoices", "who owes us", "our sales invoices" → QB Invoice entity (AR)
- When ambiguous, prefer Bill — this is an expense-heavy mining company, most queries are about costs

### Query API — SQL-style (Layer 2 — individual records)

**Bill** — vendor bills the company owes or has paid (ACCOUNTS PAYABLE)
  Safe header fields to filter on: VendorRef.name, TxnDate, DueDate, TotalAmt, Balance, DocNumber
  Filter by vendor: WHERE VendorRef.name = 'Vendor Name'
  Filter by date: WHERE TxnDate >= '2026-01-01' AND TxnDate <= '2026-03-31'
  Fetch all for period: SELECT * FROM Bill WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100

**Invoice** — money owed TO the company by customers (ACCOUNTS RECEIVABLE)
  Safe header fields to filter on: TxnDate, DueDate, TotalAmt, Balance, DocNumber
  NEVER filter by CustomerRef.name — it is a line item field, causes 400 Bad Request
  CORRECT for all invoices: SELECT * FROM Invoice WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100
  Analyst will filter by customer name from the returned data.

**Purchase** — outgoing payment records (bank transfers, card payments)
  Use ONLY for: "show me payments to vendor X", specific payment lookups
  Do NOT use for expense category analysis

**Vendor** / **Customer** / **Account** — entity lookups

### QB SQL — Filterable fields (CRITICAL — wrong fields cause 400 errors)

Bill HEADER fields (safe to filter): VendorRef.name, TxnDate, DueDate, TotalAmt, Balance, DocNumber
Bill LINE ITEM fields (NOT filterable): AccountRef.name ← NEVER use as WHERE filter

Invoice HEADER fields (safe to filter): TxnDate, DueDate, TotalAmt, Balance, DocNumber
Invoice LINE ITEM fields (NOT filterable): CustomerRef.name ← NEVER use as WHERE filter

RULE: When searching by vendor or customer name — fetch ALL records for the date range.
Let the analyst identify matching records from the returned data.

NEVER generate WHERE filters on: AccountRef.name, CustomerRef.name — always 400 Bad Request.

### Date format: YYYY-MM-DD
"""

# ─── Step 1: Retrieval Planner ────────────────────────────────────────

RETRIEVAL_SYSTEM = f"""You are a QuickBooks query generator for a Bitcoin mining company.

Today is {TODAY}.

{QB_SCHEMA}

Generate a JSON plan for fetching the data needed to answer the question.

DECISION RULES:
1. Expense category total only (no vendor detail asked) → ProfitAndLoss report only
2. Expense category + vendor/payee detail → Chain: ProfitAndLoss THEN Bill query
3. Balance sheet / financial position → BalanceSheet report
4. "Who owes us" / outstanding AR / customer invoices we sent → AgedReceivables report
5. "What we owe" / upcoming AP / vendor bills → AgedPayables report
6. Specific vendor bills by name →
   - If name is long and specific (full company name): WHERE VendorRef.name LIKE '%keyword%' AND TxnDate >= 'X' AND TxnDate <= 'Y'
   - If name is short, ambiguous, or a description (e.g. "lawyer", "S&E", "quickbooks", "internet"): fetch ALL bills for the period, analyst will fuzzy-match
   - When in doubt — fetch ALL bills. Never miss records due to a name mismatch.
   - LIKE keyword: extract the most distinctive word. "S And E Trading" → LIKE '%S%E%'. "Intuit Quickbooks" → LIKE '%quickbooks%'. "lawyer fees" → fetch ALL (description, not a name).
7. All bills for a period / "expense invoices" / "vendor invoices" → SELECT * FROM Bill WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100
8. Customer invoices (AR) for a period → SELECT * FROM Invoice WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100
9. Specific customer invoices by name → SELECT * FROM Invoice WHERE TxnDate >= 'X' AND TxnDate <= 'Y' (analyst filters by name — never use CustomerRef.name in WHERE)
10. Cash / bank balances → BalanceSheet report
11. Top expense vendors / who do we pay for X → Chain: ProfitAndLoss THEN Bill query

CHAINING RULE:
When question asks who/which vendor/payee is behind an expense category, always chain:
  Call 1: ProfitAndLoss for the date range (gets the category total)
  Call 2: SELECT * FROM Bill WHERE TxnDate >= '<start>' AND TxnDate <= '<end>' ORDERBY TxnDate DESC MAXRESULTS 100
          (fetch ALL bills — analyst will identify which relate to the category)

CRITICAL FILTER RULES — these fields cause 400 errors if used in WHERE:
  AccountRef.name — NEVER filter Bills by this
  CustomerRef.name — NEVER filter Invoices by this
Always fetch by date range only, let analyst match by name from returned data.

Examples:
- "show me all expense invoices Jan 26" → Bill query for Jan 2026 date range
- "S And E Trading invoices" → Invoice query for date range, analyst finds S&E records
- "utilities breakdown — who are the payees" → P&L + Bill query for date range
- "who do we pay for electricity" → P&L + Bill query for date range
- "top expense vendors last month" → P&L for last month + Bill query last month

ENTITY NAME MATCHING RULE:
When the user mentions a vendor or customer name, match it against the REAL QB NAMES list
provided at the bottom of their message. Match loosely — abbreviations, partial names,
missing words, different punctuation, shorthand all count as a match.
Always use the EXACT QB name in the SQL — never the user's version.

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


# ─── Step 0.5: Entity Detection + Name Resolution ────────────────────

ENTITY_DETECT_SYSTEM = """You are a query parser for a QuickBooks finance agent.

Determine if the user's question is asking about a SPECIFIC vendor or customer by name.

Examples:
- "show me S AND E trading invoices" → { "type": "customer", "term": "S AND E trading" }
- "bills from quickbooks" → { "type": "vendor", "term": "quickbooks" }
- "show me lawyer bills" → { "type": "vendor", "term": "lawyer" }
- "S&E invoices last month" → { "type": "customer", "term": "S&E" }
- "Vintech bills" → { "type": "vendor", "term": "Vintech" }
- "show me all expenses last month" → { "type": null, "term": null }
- "what are our total expenses" → { "type": null, "term": null }
- "balance sheet" → { "type": null, "term": null }

Rules:
- "vendor" = someone we pay (bills, expenses)
- "customer" = someone who pays us (invoices, AR)
- If ambiguous, use "customer" for invoice queries, "vendor" for bill/expense queries
- Only extract a name if the user clearly mentions a specific entity

Respond ONLY with valid JSON. No markdown, no backticks."""


def _detect_entity(question: str) -> dict:
    """
    Use Haiku to detect if question mentions a specific vendor/customer name.
    Returns { "type": "vendor"|"customer"|None, "term": "search term"|None }
    """
    try:
        client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=ENTITY_DETECT_SYSTEM,
            messages=[{"role": "user", "content": question}],
        )
        raw = response.content[0].text.strip()
        result = json.loads(raw)
        logger.info(f"Entity detected: {result}")
        return result
    except Exception as e:
        logger.error(f"Entity detection error: {e}")
        return {"type": None, "term": None}


def _enrich_question_with_resolved_name(question: str, original_term: str, resolved_names: list[str]) -> str:
    """
    Rewrite the question replacing the user's fuzzy term with the exact QB name(s).
    This ensures the planner generates correct SQL.
    """
    if not resolved_names:
        return question
    if len(resolved_names) == 1:
        # Replace the user's term with the exact QB name
        enriched = question.replace(original_term, resolved_names[0])
        if enriched == question:
            # Term wasn't found verbatim — append clarification
            enriched = f"{question} [resolved entity name: {resolved_names[0]}]"
        logger.info(f"Question enriched: '{question}' → '{enriched}'")
        return enriched
    else:
        # Multiple matches — tell planner to fetch all and let analyst filter
        names_str = ", ".join(resolved_names)
        enriched = f"{question} [possible QB matches: {names_str}]"
        logger.info(f"Question enriched with multiple matches: '{enriched}'")
        return enriched


# ─── Step 1.5: Vendor/Customer Name Resolution ───────────────────────

VENDOR_MATCH_SYSTEM = """You are a fuzzy name matcher for QuickBooks vendor records.

Given a user's search term and a list of real QB vendor names, find the best match.

Rules:
- Match loosely: "S&E" → "S And E Trading Sdn Bhd", "quickbooks" → "Intuit QuickBooks", "lawyer" → law firm name
- Check abbreviations, partial names, common shorthand
- If multiple vendors could match, return all plausible ones
- If nothing matches at all, return null

Respond ONLY with valid JSON:
{ "matched": ["Exact QB Vendor Name 1", "Exact QB Vendor Name 2"] }
or
{ "matched": null }
No markdown, no backticks."""

CUSTOMER_MATCH_SYSTEM = """You are a fuzzy name matcher for QuickBooks customer records.

Given a user's search term and a list of real QB customer names, find the best match.

Rules:
- Match loosely: "S&E" → "S And E Trading Sdn Bhd", abbreviations, partial names, shorthand
- If multiple customers could match, return all plausible ones
- If nothing matches at all, return null

Respond ONLY with valid JSON:
{ "matched": ["Exact QB Customer Name 1"] }
or
{ "matched": null }
No markdown, no backticks."""


def _fetch_all_vendors() -> list[str]:
    """Fetch full vendor list from QB. Returns list of vendor names."""
    try:
        data = qb_agent.query("SELECT * FROM Vendor MAXRESULTS 1000")
        vendors = data.get("QueryResponse", {}).get("Vendor", [])
        names = [v.get("DisplayName", "") for v in vendors if v.get("DisplayName")]
        logger.info(f"Fetched {len(names)} vendors from QB")
        return names
    except Exception as e:
        logger.error(f"Failed to fetch vendor list: {e}")
        return []


def _fetch_all_customers() -> list[str]:
    """Fetch full customer list from QB. Returns list of customer names."""
    try:
        data = qb_agent.query("SELECT * FROM Customer MAXRESULTS 1000")
        customers = data.get("QueryResponse", {}).get("Customer", [])
        names = [c.get("DisplayName", "") for c in customers if c.get("DisplayName")]
        logger.info(f"Fetched {len(names)} customers from QB")
        return names
    except Exception as e:
        logger.error(f"Failed to fetch customer list: {e}")
        return []


def _resolve_vendor_name(user_term: str, vendor_list: list[str]) -> list[str] | None:
    """
    Use Haiku to fuzzy-match user input against real QB vendor names.
    Returns list of matched QB vendor names, or None if no match.
    """
    if not vendor_list:
        return None
    try:
        client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=VENDOR_MATCH_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"User search term: {user_term}\n\nQB vendor names:\n" + "\n".join(vendor_list)
            }],
        )
        raw = response.content[0].text.strip()
        result = json.loads(raw)
        matched = result.get("matched")
        logger.info(f"Vendor resolution: '{user_term}' → {matched}")
        return matched
    except Exception as e:
        logger.error(f"Vendor resolution error: {e}")
        return None


def _resolve_customer_name(user_term: str, customer_list: list[str]) -> list[str] | None:
    """
    Use Haiku to fuzzy-match user input against real QB customer names.
    Returns list of matched QB customer names, or None if no match.
    """
    if not customer_list:
        return None
    try:
        client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=CUSTOMER_MATCH_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"User search term: {user_term}\n\nQB customer names:\n" + "\n".join(customer_list)
            }],
        )
        raw = response.content[0].text.strip()
        result = json.loads(raw)
        matched = result.get("matched")
        logger.info(f"Customer resolution: '{user_term}' → {matched}")
        return matched
    except Exception as e:
        logger.error(f"Customer resolution error: {e}")
        return None


def _needs_vendor_resolution(plan: dict) -> bool:
    """Check if any call in the plan uses a VendorRef.name filter."""
    for call in plan.get("calls", []):
        if call.get("type") == "query":
            sql = call.get("sql", "").lower()
            if "vendorref.name" in sql:
                return True
    return False


def _needs_customer_resolution(plan: dict) -> bool:
    """Check if any Invoice call could benefit from customer name resolution."""
    for call in plan.get("calls", []):
        if call.get("type") == "query":
            sql = call.get("sql", "").lower()
            if "from invoice" in sql:
                return True
    return False


def _extract_vendor_term(sql: str) -> str:
    """Extract the vendor search term from a SQL string."""
    import re
    # Match: VendorRef.name LIKE '%term%' or VendorRef.name = 'term'
    match = re.search(r"vendorref\.name\s+(?:like\s+'%?([^%']+)%?'|=\s+'([^']+)')", sql, re.IGNORECASE)
    if match:
        return (match.group(1) or match.group(2) or "").strip()
    return ""


def _rewrite_plan_with_vendor(plan: dict, vendor_names: list[str], date_range: dict) -> dict:
    """Rewrite plan calls using resolved vendor names."""
    new_calls = []
    for call in plan.get("calls", []):
        if call.get("type") == "query" and "vendorref.name" in call.get("sql", "").lower():
            # Build one query per matched vendor, or a combined LIKE query
            if len(vendor_names) == 1:
                start = date_range.get("start", "")
                end = date_range.get("end", "")
                name = vendor_names[0].replace("'", "\'")
                if start and end:
                    sql = f"SELECT * FROM Bill WHERE VendorRef.name = '{name}' AND TxnDate >= '{start}' AND TxnDate <= '{end}' ORDERBY TxnDate DESC MAXRESULTS 100"
                else:
                    sql = f"SELECT * FROM Bill WHERE VendorRef.name = '{name}' ORDERBY TxnDate DESC MAXRESULTS 100"
            else:
                # Multiple matches — fetch all bills for period, analyst will filter
                start = date_range.get("start", "")
                end = date_range.get("end", "")
                if start and end:
                    sql = f"SELECT * FROM Bill WHERE TxnDate >= '{start}' AND TxnDate <= '{end}' ORDERBY TxnDate DESC MAXRESULTS 100"
                else:
                    sql = "SELECT * FROM Bill ORDERBY TxnDate DESC MAXRESULTS 100"
            new_calls.append({**call, "sql": sql, "resolved_vendors": vendor_names})
        else:
            new_calls.append(call)
    return {**plan, "calls": new_calls}


def _extract_date_range_from_plan(plan: dict) -> dict:
    """Extract start/end dates from plan calls."""
    import re
    for call in plan.get("calls", []):
        sql = call.get("sql", "")
        start_match = re.search(r"TxnDate\s*>=\s*'([^']+)'", sql)
        end_match = re.search(r"TxnDate\s*<=\s*'([^']+)'", sql)
        if start_match or end_match:
            return {
                "start": start_match.group(1) if start_match else "",
                "end": end_match.group(1) if end_match else ""
            }
    return {}


def _generate_name_examples(names: list[str]) -> list[str]:
    """
    Dynamically generate fuzzy match examples from real QB names.
    Shows the planner what kinds of shorthand map to each exact name.
    """
    import re
    examples = []
    for name in names:
        variants = []

        # Abbreviation: first letters of significant words
        words = re.split(r"[\s\-\(\)\.]+", name)
        SKIP = {"SDN", "BHD", "PLT", "LTD", "PTE", "HK", "USD", "SMM2H", "AND", "THE", "FOR", "OF"}
        sig_words = [w for w in words if len(w) > 2 and w.upper() not in SKIP]
        if len(sig_words) >= 2:
            abbrev = " ".join(sig_words[:2])
            if abbrev.lower() != name.lower():
                variants.append(f'"{abbrev}"')

        # First word only (if distinctive)
        if sig_words and len(sig_words[0]) > 3:
            first = sig_words[0]
            if first.lower() != name.lower():
                variants.append(f'"{first}"')

        # Strip suffix noise: "- USD", "(HK)", "Pte. Ltd.", "Sdn Bhd", "PLT"
        stripped = re.sub(r"\s*[-–]\s*(USD|MYR|SGD)$", "", name, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*(Sdn\.?\s*Bhd\.?|Pte\.?\s*Ltd\.?|PLT|Limited)$", "", stripped, flags=re.IGNORECASE).strip()
        if stripped and stripped.lower() != name.lower() and len(stripped) > 3:
            variants.append(f'"{stripped}"')

        if variants:
            examples.append(f'  {" or ".join(variants)} → "{name}"')

    return examples


def _build_entity_context() -> str:
    """
    Fetch real vendor and customer names from QB and format as context for the planner.
    Includes dynamically generated fuzzy match examples so the planner understands
    what shorthand maps to which exact name — no hardcoding.
    """
    try:
        vendors = _fetch_all_vendors()
        customers = _fetch_all_customers()
        lines = []

        if vendors:
            lines.append("REAL QB VENDOR NAMES (use exact name in SQL):")
            for v in vendors:
                lines.append(f"  - {v}")
            vendor_examples = _generate_name_examples(vendors)
            if vendor_examples:
                lines.append("\nVendor fuzzy match examples (user shorthand → exact QB name):")
                lines.extend(vendor_examples)

        if customers:
            lines.append("\nREAL QB CUSTOMER NAMES (use exact name in SQL):")
            for c in customers:
                lines.append(f"  - {c}")
            customer_examples = _generate_name_examples(customers)
            if customer_examples:
                lines.append("\nCustomer fuzzy match examples (user shorthand → exact QB name):")
                lines.extend(customer_examples)

        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Could not fetch entity lists: {e}")
        return ""


def _plan_calls(question: str, intent: str) -> dict:
    """Generate QB API call plan based on classified intent."""
    system = FORECAST_SYSTEM if intent == "FORECAST_TREND" else RETRIEVAL_SYSTEM

    # Inject real entity names so planner can match user input to exact QB names
    entity_context = _build_entity_context()
    if entity_context:
        enriched_question = f"{question}\n\n---\n{entity_context}"
    else:
        enriched_question = question

    try:
        client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=system,
            messages=[{"role": "user", "content": enriched_question}],
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
    Step 1.5: Vendor/customer name resolution via Haiku (if needed)
    Step 2: Execute calls
    Returns structured result for qb_analyst.
    """
    logger.info(f"Interpreting: '{user_question}'")

    # Step 0 — Classify intent
    intent = _classify_intent(user_question)

    # Step 1 — Plan (entity context injected automatically inside _plan_calls)
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