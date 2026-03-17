"""
qb_interpreter.py — Natural language → QuickBooks query execution.

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


def _today() -> str:
    """Current date as human-readable string. Called fresh on every use."""
    return datetime.now().strftime("%B %d, %Y")


def _today_iso() -> str:
    """Current date as ISO string. Called fresh on every use."""
    return datetime.now().strftime("%Y-%m-%d")

# ─── Step 0: Intent (all queries are RETRIEVAL) ──────────────────────

def _classify_intent(question: str) -> str:
    """All queries route to RETRIEVAL pipeline. Forecast removed from scope."""
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
- ExchangeRate — params: source_currency (e.g. "USD"), as_of_date (YYYY-MM-DD)
  → Returns QB's recorded exchange rate between source_currency and home currency (MYR)
  → Use the period end date as as_of_date
  → Only include when user explicitly requests a specific output currency ("in USD", "in MYR")
- BalanceSheet — params: date (YYYY-MM-DD)
- AgedReceivables — no params required
- AgedPayables — no params required
- CashFlow — params: start_date, end_date

### TERMINOLOGY — Standard Accounting Conventions
Follow standard accounting terminology strictly:

**Invoice** = QB Invoice entity (Accounts Receivable)
  - Documents YOU issue to customers requesting payment
  - "show me invoices", "invoices we sent", "what customers owe us"
  - Use the CUSTOMER list to match names

**Bill** = QB Bill entity (Accounts Payable)  
  - Documents VENDORS send you requesting payment
  - "show me bills", "bills we owe", "vendor bills", "what we owe"
  - Use the VENDOR list to match names

If a name is mentioned, cross-reference the VENDOR and CUSTOMER lists below to determine correct entity:
- Name in VENDOR list → Bill query
- Name in CUSTOMER list → Invoice query
- Name in both → use context (invoice = AR, bill = AP)

### Query API — SQL-style (Layer 2 — individual records)

**Bill** — vendor bills the company owes or has paid (ACCOUNTS PAYABLE)
  Safe header fields to filter on: TxnDate, DueDate, TotalAmt, Balance, DocNumber
  NEVER filter by VendorRef.name — causes 400 Bad Request, same as AccountRef.name
  Always fetch by date range: SELECT * FROM Bill WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100
  Analyst will filter by vendor name from the returned data.

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

Bill fields safe to filter: TxnDate, DueDate, TotalAmt, Balance, DocNumber
Bill fields NOT filterable (cause 400): VendorRef.name, AccountRef.name ← NEVER use in WHERE

Invoice HEADER fields (safe to filter): TxnDate, DueDate, TotalAmt, Balance, DocNumber
Invoice LINE ITEM fields (NOT filterable): CustomerRef.name ← NEVER use as WHERE filter

RULE: When searching by vendor or customer name — fetch ALL records for the date range.
Let the analyst identify matching records from the returned data.

NEVER generate WHERE filters on: AccountRef.name, CustomerRef.name — always 400 Bad Request.

### Date format: YYYY-MM-DD
"""

# ─── Step 1: Retrieval Planner ────────────────────────────────────────

def _build_retrieval_system() -> str:
    """Build retrieval planner prompt with fresh date on every call."""
    today = _today()
    today_iso = _today_iso()
    return f"""You are a QuickBooks query generator for a Bitcoin mining company.

Today is {today}.

{QB_SCHEMA}

Generate a JSON plan for fetching the data needed to answer the question.

DECISION RULES:
1. Mining P&L / mining revenue / others / all P&L / summary → ProfitAndLoss report only.
   Mining revenue = Revenue:Realised + Revenue:Un-Realised accounts in the P&L.
   NEVER use an Invoice query for mining or others revenue.

   Hosting revenue → Invoice query ONLY. No ProfitAndLoss call for hosting.
   Hosting has no cost segment in P&L — Utility-AA costs are classified as Others.
   When user asks "hosting revenue", "hosting P&L", "/pnl hosting", "/hosting", or
   anything hosting-related: generate an Invoice query only. Do NOT add a ProfitAndLoss call.
   "/pnl hosting" is NOT a valid P&L query — treat it as a hosting revenue Invoice query.
2. Expense category + vendor/payee detail → Chain: ProfitAndLoss THEN Bill query
3. Balance sheet / financial position → BalanceSheet report
4. "Who owes us" / outstanding AR / customer invoices we sent → AgedReceivables report
5. "What we owe" / upcoming AP / vendor bills → AgedPayables report
6. Specific vendor bills by name → fetch ALL bills for the period by date range only
   NEVER use VendorRef.name in WHERE — it causes 400 Bad Request
   Always: SELECT * FROM Bill WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100
   The analyst will filter by the resolved vendor name from the returned data.
7. All bills for a period / "expense invoices" / "vendor invoices" → SELECT * FROM Bill WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100
8. Specific vendor name (from VENDOR list) → same as rule 6 — date-range-only Bill query
9. Customer invoices (AR) for a period → SELECT * FROM Invoice WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100
10. Specific customer name → same as rule 9 — date-range-only Invoice query, analyst filters by name
11. Cash / bank balances → BalanceSheet report
12. Top vendors by spend / "who do we pay the most" / "biggest vendors" →
    Chain: ProfitAndLoss for period + Bill query for same period
    Analyst ranks vendors by total bill amount from the Bill results.
13. Transactions above a threshold / "transactions over $X" / "large payments" →
    SELECT * FROM Bill WHERE TxnDate >= 'X' AND TxnDate <= 'Y' AND TotalAmt > 'N' ORDERBY TotalAmt DESC MAXRESULTS 100
    TotalAmt IS filterable on Bill. Use the numeric value from the question. Default currency match.
14. New vendors / "vendors we started paying" / "new suppliers" →
    Chain: Bill query for the CURRENT period + Bill query for the PRIOR period (same length)
    Analyst compares vendor sets to identify vendors that appear in current but not prior period.
    Use two separate calls with different date ranges.
15. BillPayment / "payments made to" / "when did we pay" / "payment history" →
    SELECT * FROM BillPayment WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100
    BillPayment records actual payment transactions (cheques, bank transfers) against bills.
16. Currency conversion requested — user says "in USD", "in MYR", "convert to USD", "show in USD" etc. →
    Add ONE ExchangeRate call to the plan alongside the data calls.
    - source_currency: the FROM currency (e.g. "USD" if user wants MYR→USD, "USD" if user wants USD→MYR)
    - For hosting queries involving Northstar (USD invoices): source_currency = "USD"
    - For mining/MYR queries where user wants USD: source_currency = "USD"
    - as_of_date: last day of the queried period (or today for balance sheet queries)
    - Default (no currency mentioned) → do NOT add ExchangeRate call; report as-is from QB
    Example — "show hosting P&L Jan 2026 in USD":
      {{"type": "report", "report_name": "ProfitAndLoss", "params": {{"start_date": "2026-01-01", "end_date": "2026-01-31"}}}}
      {{"type": "exchangerate", "source_currency": "USD", "as_of_date": "2026-01-31"}}

17. ANY query with monthly breakdown / "breakdown by month" / "month by month" / "monthly" across a multi-month range →
    Generate ONE call per calendar month in the range, using the same call type as you would for a single period.
    Do NOT make a single call for the full range — the analyst needs one result per month to build per-month rows.
    Always use the last calendar day of each month as end_date:
      January=31, February=28 (29 in leap years: 2024, 2028 etc), March=31, April=30,
      May=31, June=30, July=31, August=31, September=30, October=31, November=30, December=31

    By query type:
    - Mining P&L / expenses / business line → one ProfitAndLoss report per month
    - Hosting revenue                → one Invoice query per month (no ProfitAndLoss for hosting)
    - Bills / vendor spend           → one Bill query per month (date range only, no vendor filter in SQL)
    - Invoices / AR / customer       → one Invoice query per month (date range only, no customer filter in SQL)
    - BillPayments / payments made   → one BillPayment query per month

    Example — "mining P&L Oct 2025 to Feb 2026 breakdown by month" → 5 ProfitAndLoss calls:
      {{"type": "report", "report_name": "ProfitAndLoss", "params": {{"start_date": "2025-10-01", "end_date": "2025-10-31"}}}}
      {{"type": "report", "report_name": "ProfitAndLoss", "params": {{"start_date": "2025-11-01", "end_date": "2025-11-30"}}}}
      {{"type": "report", "report_name": "ProfitAndLoss", "params": {{"start_date": "2025-12-01", "end_date": "2025-12-31"}}}}
      {{"type": "report", "report_name": "ProfitAndLoss", "params": {{"start_date": "2026-01-01", "end_date": "2026-01-31"}}}}
      {{"type": "report", "report_name": "ProfitAndLoss", "params": {{"start_date": "2026-02-01", "end_date": "2026-02-28"}}}}

    Example — "hosting revenue Dec 2025 to Feb 2026 breakdown by month" → 3 Invoice queries:
      {{"type": "query", "sql": "SELECT * FROM Invoice WHERE TxnDate >= '2025-12-01' AND TxnDate <= '2025-12-31' ORDERBY TxnDate DESC MAXRESULTS 100"}}
      {{"type": "query", "sql": "SELECT * FROM Invoice WHERE TxnDate >= '2026-01-01' AND TxnDate <= '2026-01-31' ORDERBY TxnDate DESC MAXRESULTS 100"}}
      {{"type": "query", "sql": "SELECT * FROM Invoice WHERE TxnDate >= '2026-02-01' AND TxnDate <= '2026-02-28' ORDERBY TxnDate DESC MAXRESULTS 100"}}

    Example — "bills from S And E month by month Q1 2026" → 3 Bill queries:
      {{"type": "query", "sql": "SELECT * FROM Bill WHERE TxnDate >= '2026-01-01' AND TxnDate <= '2026-01-31' ORDERBY TxnDate DESC MAXRESULTS 100"}}
      {{"type": "query", "sql": "SELECT * FROM Bill WHERE TxnDate >= '2026-02-01' AND TxnDate <= '2026-02-28' ORDERBY TxnDate DESC MAXRESULTS 100"}}
      {{"type": "query", "sql": "SELECT * FROM Bill WHERE TxnDate >= '2026-03-01' AND TxnDate <= '2026-03-31' ORDERBY TxnDate DESC MAXRESULTS 100"}}

    Example — "Northstar invoices breakdown by month Oct–Dec 2025" → 3 Invoice queries:
      {{"type": "query", "sql": "SELECT * FROM Invoice WHERE TxnDate >= '2025-10-01' AND TxnDate <= '2025-10-31' ORDERBY TxnDate DESC MAXRESULTS 100"}}
      {{"type": "query", "sql": "SELECT * FROM Invoice WHERE TxnDate >= '2025-11-01' AND TxnDate <= '2025-11-30' ORDERBY TxnDate DESC MAXRESULTS 100"}}
      {{"type": "query", "sql": "SELECT * FROM Invoice WHERE TxnDate >= '2025-12-01' AND TxnDate <= '2025-12-31' ORDERBY TxnDate DESC MAXRESULTS 100"}}

CHAINING RULE:
When question asks who/which vendor/payee is behind an expense category, always chain:
  Call 1: ProfitAndLoss for the date range (gets the category total)
  Call 2: SELECT * FROM Bill WHERE TxnDate >= '<start>' AND TxnDate <= '<end>' ORDERBY TxnDate DESC MAXRESULTS 100
          (fetch ALL bills — analyst will identify which relate to the category)

CRITICAL FILTER RULES — these fields cause 400 errors if used in WHERE:
  VendorRef.name — NEVER filter Bills by this
  AccountRef.name — NEVER filter Bills by this
  CustomerRef.name — NEVER filter Invoices by this
Always fetch Bills/Invoices by date range only. TotalAmt IS safe to filter.

Examples:
- "show me all expense invoices Jan 26" → Bill query for Jan 2026 date range
- "bills from TM Technology" → Bill query for default period, analyst filters by resolved name
- "S And E Trading invoices" → Invoice query for date range, analyst finds S&E records
- "utilities breakdown — who are the payees" → P&L + Bill query for date range
- "who do we pay for electricity" → P&L + Bill query for date range
- "top vendors by spend last quarter" → P&L + Bill query for last quarter
- "all transactions over MYR 50000 this month" → Bill query with TotalAmt > 50000
- "new vendors this quarter" → Bill query this quarter + Bill query last quarter (two calls)
- "when did we last pay Northstar" → BillPayment query for last 6 months
- "hosting revenue Jan 2026" → Invoice query (Jan 2026) only
- "hosting revenue Jan 2026 in USD" → Invoice query (Jan 2026) + ExchangeRate call (USD, 2026-01-31)
- "show mining P&L in USD" → ProfitAndLoss call + ExchangeRate call (USD, last day of period)
- "mining P&L Oct to Feb breakdown by month" → 5 separate ProfitAndLoss calls, one per month
- "hosting revenue Oct to Feb breakdown by month" → 3 Invoice queries, one per month
- "show monthly P&L last quarter" → 3 separate ProfitAndLoss calls, one per month in the quarter
- "S And E bills month by month last quarter" → 3 separate Bill queries, one per month
- "Northstar invoices breakdown by month Q4 2025" → 3 separate Invoice queries, one per month
- "payments to vendors month by month Jan–Mar" → 3 separate BillPayment queries, one per month

ENTITY NAME MATCHING RULE:
When the user mentions a vendor or customer name, match it against the REAL QB NAMES list
provided at the bottom of their message. Match loosely — abbreviations, partial names,
missing words, different punctuation, shorthand all count as a match.
Always use the EXACT QB name in the SQL — never the user's version.

DEFAULT DATE RULE:
If no date or period is specified:
- For Bill / Invoice / vendor / customer queries: default to past 3 completed months
  start_date = first day of month 3 months ago
  end_date = today ({today_iso})
- For balance sheet: use today
- For P&L summary / quarterly: use last completed month
  start_date = first day of last month
  end_date = last day of last month
{{
  "calls": [
    {{"type": "report", "report_name": "ProfitAndLoss", "params": {{"start_date": "2026-02-01", "end_date": "2026-02-28"}}}},
    {{"type": "query", "sql": "SELECT * FROM Bill WHERE TxnDate >= '2026-02-01' AND TxnDate <= '2026-02-28' ORDERBY TxnDate DESC MAXRESULTS 100"}}
  ],
  "query_complexity": "simple" | "detail",
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


# Module-level cache — fetched once per process, refreshed if empty
_entity_cache: dict = {
    "vendors": [],
    "customers": [],
    "context": "",
    "loaded": False,
    "loaded_at": 0.0,  # epoch timestamp of last successful load
}

CACHE_TTL_SECONDS = 86400  # 24 hours


def warm_cache():
    """
    Load vendor + customer lists from QB and populate the cache.
    Called at startup in a background thread and every 24h thereafter.
    Safe to call multiple times — idempotent.
    """
    import time
    global _entity_cache
    try:
        logger.info("🔄 Warming entity cache (vendor + customer lists)...")
        vendors = _fetch_all_vendors()
        customers = _fetch_all_customers()
        context = _build_context_string(vendors, customers)
        _entity_cache = {
            "vendors": vendors,
            "customers": customers,
            "context": context,
            "loaded": True,
            "loaded_at": time.time(),
        }
        logger.info(f"✅ Entity cache warm — {len(vendors)} vendors, {len(customers)} customers")
    except Exception as e:
        logger.error(f"Entity cache warm failed: {e}")


def _cache_is_fresh() -> bool:
    import time
    if not _entity_cache["loaded"]:
        return False
    age = time.time() - _entity_cache.get("loaded_at", 0)
    return age < CACHE_TTL_SECONDS


def refresh_entity_cache():
    """Force a full refresh of the vendor/customer cache. Call after adding new vendors in QB."""
    global _entity_cache
    _entity_cache["loaded"] = False
    _entity_cache["loaded_at"] = 0.0
    warm_cache()
    logger.info("Entity cache force-refreshed.")


def _build_context_string(vendors: list, customers: list) -> str:
    """Build the planner context string from vendor/customer lists."""
    lines = []
    if vendors:
        lines.append("REAL QB VENDOR NAMES — AP (Accounts Payable) — use Bill entity for these:")
        for v in vendors:
            lines.append(f"  - {v}")
        vendor_examples = _generate_name_examples(vendors)
        if vendor_examples:
            lines.append("\nVendor fuzzy match examples (user shorthand → exact QB name):")
            lines.extend(vendor_examples)
    if customers:
        lines.append("\nREAL QB CUSTOMER NAMES — AR (Accounts Receivable) — use Invoice entity for these:")
        for c in customers:
            lines.append(f"  - {c}")
        customer_examples = _generate_name_examples(customers)
        if customer_examples:
            lines.append("\nCustomer fuzzy match examples (user shorthand → exact QB name):")
            lines.extend(customer_examples)
    lines.append("\nCROSS-REFERENCE RULE: If a name appears in the VENDOR list → use Bill. If in CUSTOMER list → use Invoice.")
    return "\n".join(lines)


def _build_entity_context() -> str:
    """
    Return vendor/customer context string for the planner.
    Uses cache if fresh, otherwise triggers a warm.
    """
    if _cache_is_fresh():
        return _entity_cache["context"]
    # Cache stale or empty — warm it now (synchronous fallback)
    warm_cache()
    return _entity_cache.get("context", "")


def _plan_calls(question: str) -> dict:
    """Generate QB API call plan."""
    system = _build_retrieval_system()

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
            max_tokens=2000,
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
    """Execute all API calls concurrently and return results in original order."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    calls = plan.get("calls", [])
    if not calls:
        return []

    results = [None] * len(calls)

    def _execute_one(idx: int, call: dict):
        try:
            if call["type"] == "report":
                params = {**call.get("params", {}), "minorversion": "65"}
                data = qb_agent.get_report(call["report_name"], params)
                return idx, {"call": call, "data": data, "error": None}
            elif call["type"] == "query":
                data = qb_agent.query(call["sql"])
                return idx, {"call": call, "data": data, "error": None}
            elif call["type"] == "exchangerate":
                data = qb_agent.get_exchange_rate(call["source_currency"], call["as_of_date"])
                return idx, {"call": call, "data": data, "error": None}
            else:
                return idx, {"call": call, "data": None, "error": f"Unknown call type: {call['type']}"}
        except Exception as e:
            logger.error(f"QB call failed: {call} — {e}")
            return idx, {"call": call, "data": None, "error": str(e)}

    with ThreadPoolExecutor(max_workers=min(len(calls), 5)) as executor:
        futures = [executor.submit(_execute_one, i, call) for i, call in enumerate(calls)]
        for future in as_completed(futures):
            idx, result = future.result()
            results[idx] = result

    return results


# ─── Main Entry Point ─────────────────────────────────────────────────

def interpret_and_fetch(user_question: str) -> dict:
    """
    Main entry point.

        Step 0.5: Detect + resolve any vendor/customer name mentioned in the question
    Step 1:   Plan QB API calls (entity context injected so planner uses exact QB names)
    Step 2:   Execute calls
    Returns structured result for qb_analyst.
    """
    logger.info(f"Interpreting: '{user_question}'")

    # Step 0 — Classify intent
    intent = _classify_intent(user_question)

    # Step 0.5 — Detect and resolve entity name
    # Even though the planner has entity context, we resolve separately so the
    # analyst always receives the matched QB name(s) explicitly.
    # Always use cache — never call QB directly per query.
    resolved_vendors = []
    resolved_customers = []
    entity = _detect_entity(user_question)
    if entity.get("type") == "vendor" and entity.get("term"):
        if not _cache_is_fresh():
            warm_cache()
        vendor_list = _entity_cache.get("vendors", [])
        matches = _resolve_vendor_name(entity["term"], vendor_list)
        if matches:
            resolved_vendors = matches
            logger.info(f"Resolved vendor '{entity['term']}' → {matches}")
        else:
            logger.info(f"No vendor match for '{entity['term']}' — analyst will scan all results")
    elif entity.get("type") == "customer" and entity.get("term"):
        if not _cache_is_fresh():
            warm_cache()
        customer_list = _entity_cache.get("customers", [])
        matches = _resolve_customer_name(entity["term"], customer_list)
        if matches:
            resolved_customers = matches
            logger.info(f"Resolved customer '{entity['term']}' → {matches}")
        else:
            logger.info(f"No customer match for '{entity['term']}' — analyst will scan all results")

    # Step 1 — Plan (entity context injected inside _plan_calls)
    plan = _plan_calls(user_question)
    if "error" in plan and not plan.get("calls"):
        return {
            "question": user_question,
            "intent": intent,
            "query_complexity": "simple",
            "results": [],
            "resolved_vendors": resolved_vendors or None,
            "resolved_customers": resolved_customers or None,
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
        "resolved_vendors": resolved_vendors or None,
        "resolved_customers": resolved_customers or None,
        "error": None,
    }