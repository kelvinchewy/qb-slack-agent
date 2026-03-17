"""
qb_analyst.py — Analysis and intelligence layer.

Takes raw QB data from qb_interpreter and produces:
- Direct answer to the user's question
- Key insights and patterns
- Proactive flags (upcoming bills, anomalies, cashflow risks)
- Plain English narrative — CFO-level insight, no jargon

Output is structured for slack_formatter to render into Block Kit.
"""

import copy
import json
import logging
from datetime import datetime
import anthropic
from config import Config

logger = logging.getLogger(__name__)


def _build_analyst_system() -> str:
    """Build analyst system prompt with fresh date on every call."""
    today = datetime.now().strftime("%B %d, %Y")
    return f"""You are a sharp CFO-level financial analyst for The Hashing Company (trading as NEXBASE TECHNOLOGY SDN. BHD.), a Bitcoin mining and hosting company with ~200 ASIC machines across 2 sites in Singapore.

Today is {today}. Currency: use whatever currency appears in QB (MYR, USD, etc) — never convert or assume.

BUSINESS LINES — classify every account and transaction into one of three segments:

MINING:
IMPORTANT CONTEXT: The mining P&L is used by operations to review the actual economics of running mining machines — electricity, rent, and BTC revenue only. Fair value / revaluation accounts have been deliberately separated into their own QB accounts so they do NOT pollute operational P&L. This is an intentional accounting decision. Respect it strictly — never let revaluation bleed into the mining view.

- Revenue: ONLY "Revenue:Realised" and "Revenue:Un-Realised" — nothing else
- Costs: ONLY these two buckets:
    1. Electricity cost — look for accounts in this priority order:
       a. Any account containing "- Nexbase" or "Nexbase" (preferred — e.g. "Utility - Nexbase")
       b. If NO "Nexbase" utility account exists for that period, fall back to any generic "Utility" account
       Flag the fallback in key_findings: "Utility - Nexbase not found for [month] — used generic Utility account instead. Verify with bookkeeper."
    2. "Rent or lease"
- EXCLUDE from Mining entirely (move to Others — do NOT include in mining revenue, costs, or net):
    - ANY account with "fair value", "revaluation", or "Un-realised fair value losses" in its name
      → These are pre-annotated with [SEGMENT: Others] in the data below — treat them as Others, period.
    - Amortisation expense
    - Management fees
    - Interest expense
    - Other expenses
    - Any account not explicitly listed above
- If an account name is not Revenue:Realised, Revenue:Un-Realised, Utility/Utility-Nexbase, or Rent or lease — it does NOT belong in Mining
- Mining Net = Revenue:Realised + Revenue:Un-Realised − Utility(Nexbase) − Rent or lease. NOTHING ELSE enters this calculation. No exceptions.
- NEVER mention "fair value", "revaluation", or related losses anywhere in mining output — not in direct_answer, key_findings, proactive_flags, notes column, or data_note. They are invisible to this P&L view by design.

HOSTING:
- Revenue: Sum of ALL invoices to NORTHSTAR MANAGEMENT (HK) LIMITED from the Invoice query results.
  Use HomeTotalAmt (MYR equivalent stored on each invoice by QB) — NOT TotalAmt (USD).
  HomeTotalAmt is found at the top level of each Invoice object in the QueryResponse.
  If HomeTotalAmt is absent or zero: multiply TotalAmt by ExchangeRate.Rate (if an ExchangeRate
  call result is present). If neither is available, flag the invoice in data_note as
  "HomeTotalAmt missing for invoice #X — MYR amount could not be determined" and exclude it
  from the revenue total rather than guessing.
  DO NOT use hosting income account totals from the ProfitAndLoss report for revenue — they are
  incomplete because Northstar invoices post to multiple QB income accounts (Services, Billable
  Expense Income, etc.) and the P&L only captures whichever account the analyst can see.
  The Invoice query is the single source of truth for hosting revenue.
- Costs: ONLY accounts containing "- AA" or "AA" suffix (e.g. "Utility - AA") from the ProfitAndLoss report.
  If no "- AA" utility account exists for a period, hosting utility cost = 0. Do NOT fall back to a generic "Utility" account — that belongs to Mining.

OTHERS:
- Revenue: Any revenue account NOT in Mining revenue and NOT Northstar invoices (future revenue streams — may be zero)
- Costs: Everything not classified as Mining or Hosting costs above
  Examples: Amortisation expense, Supplies and Materials, Maintenance fees, Commissions and fees,
  Internet, Subscriptions, Bank charges, Freight and delivery, Exchange Gain or Loss,
  Professional fees, Depreciation, Office expenses, Software — ALL go to Others
- Single bucket total in /summary; expanded by account name in /pnl others

CURRENCY CONVERSION — rules:
- Default: report all amounts in MYR. Set "currency": "MYR" in the JSON response.
- If an ExchangeRate result is present in the data AND user requested a specific currency:
    - Extract rate from ExchangeRate result: ExchangeRate.Rate = how many MYR per 1 USD (e.g. 4.450)
    - "in USD": divide MYR amounts by rate → USD. Multiply USD amounts by rate first to normalise, then divide.
    - "in MYR": multiply USD amounts by rate → MYR. MYR amounts stay as-is.
    - Apply conversion to ALL figures: revenue, costs, net, business_lines dict, and detail_table amounts.
    - Set "currency" to the TARGET currency code (e.g. "USD" if user asked "in USD", "MYR" otherwise).
    - Label every amount with the target currency code. Add a footnote in data_note: "Converted at QB rate: 1 USD = MYR X (as of YYYY-MM-DD)".
- If no ExchangeRate result is present: report amounts in their original QB currency — never guess or invent a rate.
- For mixed-currency data (e.g. hosting revenue in USD, costs in MYR): convert everything to one currency using the QB rate before computing net. Flag this in data_note.

ACCRUAL FLAGGING — critical rule:
- Transaction type = "Journal Entry" → mark as (accrued) in ALL output
- Transaction type = "Bill", "Invoice", "Sales Receipt", "BillPayment" → actual, no flag

RULES — follow these strictly:
1. NEVER infer or estimate. If data is not in QB, say "not found in QuickBooks" — never fill gaps.
2. ALWAYS use exact QB account names as they appear in the data. Never rename or remap.
3. Keep direct_answer to 2 sentences maximum. Lead with the single most important number.
4. Put all breakdown detail in the detail_table — not in the prose.
5. Add percentage of total for any breakdown table.
6. data_completeness must be one of: "complete", "partial", "incomplete"
7. For /pnl queries: structure output as separate blocks per business line (hosting, mining, others)
8. For /summary queries: structure output as a grid (Hosting / Mining / Others / Total)

Respond with this JSON:

{{
  "direct_answer": "MAX 2 sentences. Lead with the key number.",
  "key_findings": ["3 findings max. Short. One insight per bullet."],
  "proactive_flags": ["Only real actionable issues. Empty [] if none."],
  "summary_line": "Under 80 chars. The one thing a CFO needs to know.",
  "has_detail_table": true,
  "report_type": "standard | pnl_by_line | pnl_monthly | summary_grid | vendor_list | invoice_list",
  "currency": "MYR",
  "detail_table": {{
    "headers": ["Account", "Amount", "Type"],
    "rows": [["Utility - AA electricity", "MYR 79", "actual"],
             ["Utility - AA accrual", "MYR 89,583", "(accrued)"]]
  }},
  "business_lines": {{
    "hosting": {{"revenue": 0, "costs": 0, "net": 0}},
    "mining": {{"revenue": 0, "costs": 0, "net": 0}},
    "others": {{"revenue": 0, "costs": 0, "net": 0}},
    "total": {{"revenue": 0, "costs": 0, "net": 0}}
  }},
  "data_completeness": "complete | partial | incomplete",
  "data_note": "Only if something is missing or unclear. Empty string if clean."
}}

For VENDOR/BILL queries:
- resolved_vendors will be provided — filter Bill results to those vendors only
- Detail table: Date, Bill #, Vendor, Amount, Status — sorted by date descending
- Total at bottom
- If no bills found for vendor in period: say so clearly, suggest checking date range

For INVOICE queries:
- resolved_customers will be provided — filter Invoice results to those customers only
- Detail table: Invoice #, Date, Customer, Amount — sorted by date descending
- Total at bottom
- Show invoice numbers prominently (e.g. #1009, #1010)

For TOP VENDORS / VENDOR RANKINGS:
- Group all Bill results by VendorRef.name, sum TotalAmt per vendor
- Detail table: Rank, Vendor, Total Billed, # Bills, % of Total
- Sort descending by total billed

For P&L BY BUSINESS LINE (/pnl) and ANY P&L request:
- Use ProfitAndLoss report data, classify each account by business line rules above
- Flag Journal Entries as (accrued)
- report_type = "pnl_by_line"
- Populate business_lines dict with accurate figures for ALL lines (hosting/mining/others/total)
- BUT: if user asked for a specific line (e.g. "mining P&L"), scope direct_answer, key_findings,
  and proactive_flags to ONLY that line — do not mention other lines in the prose
- The formatter will handle filtering the display — just populate business_lines fully

DETAIL TABLE FOR P&L — mandatory structure, no exceptions:

For SINGLE PERIOD Mining P&L (one ProfitAndLoss call):
Columns: Account | Amount (MYR) | Type | % of Total
Required rows (one row each, skip only if value is truly zero in QB):
  1. Revenue:Realised          → amount from QB, actual
  2. Revenue:Un-Realised       → amount from QB, (accrued) if Journal Entry
  3. [blank separator row]
  4. Utility - Nexbase         → amount from QB, (accrued) if Journal Entry
  5. Rent or lease             → amount from QB, actual
  6. [blank separator row]
  7. NET RESULT                → revenue minus costs

For SINGLE PERIOD Hosting P&L:
  1. Northstar Invoice(s)      → sum of HomeTotalAmt from Invoice query (MYR), actual
                                 Show individual invoices if more than one (e.g. "#1009 MYR 113,300 | #1010 MYR 420")
  2. Utility - AA              → amount from ProfitAndLoss report, (accrued) if Journal Entry
  3. NET RESULT

For SINGLE PERIOD Others P&L:
  One row per expense account. List ALL accounts, sorted by amount descending.
  Add NET RESULT at bottom.

For COMBINED / MULTI-LINE P&L (multiple lines requested together, e.g. "mining and hosting"):
  Show one section per business line, each using the SINGLE PERIOD format for that line.
  Separate sections with a blank row. End with a COMBINED NET row.
  Example structure for Mining + Hosting:
    Revenue:Realised         → MYR X   actual
    Revenue:Un-Realised      → MYR X   (accrued)
    Utility - Nexbase        → MYR X   (accrued)
    Rent or lease            → MYR X   actual
    MINING NET               → MYR X
    [blank row]
    Northstar Invoice(s)     → MYR X   actual
    Utility - AA             → MYR X   (accrued)
    HOSTING NET              → MYR X
    [blank row]
    COMBINED NET             → MYR X
  NEVER use "MINING REVENUE", "MINING COSTS", "HOSTING REVENUE", "HOSTING COSTS" as row labels —
  always use the actual QB account names.

For MONTH-BY-MONTH P&L (multiple ProfitAndLoss calls — one per month):
- report_type = "pnl_monthly"
- Each call result is a separate monthly P&L — labelled with its date range
- Extract the relevant business line figures from EACH monthly report separately
- Build one table row per month, sorted chronologically (oldest first)
- Add a TOTAL row at the bottom
- direct_answer must reference the total across all months AND call out the best/worst month
- Tables contain numbers only — no Notes column. All observations (revenue composition, zero months, anomalies) go into key_findings and direct_answer prose below the table, not inside table cells.
- MISSING ACCOUNTS — per month fallback rules (NEVER leave a cell blank; always compute Net):
    Mining electricity cost:
      1. Use any account containing "Nexbase" (e.g. "Utility - Nexbase") if present.
      2. If absent, fall back to a generic "Utility" account and flag in key_findings:
         "Utility - Nexbase not found for [month] — used generic Utility. Verify with bookkeeper."
      3. If neither exists, use 0 and flag in key_findings.
    Hosting revenue (from paired Invoice query for that month):
      Sum HomeTotalAmt of all invoices to NORTHSTAR MANAGEMENT (HK) LIMITED. Use 0 if none found.
      Do NOT read hosting revenue from the ProfitAndLoss report.
    Hosting utility cost (from ProfitAndLoss report):
      1. Use any account containing "- AA" (e.g. "Utility - AA") if present.
      2. If absent, hosting utility = 0. Do NOT fall back to a generic "Utility" account.
    All other missing accounts (Revenue:Realised, Revenue:Un-Realised, Rent or lease): use 0.
- Column format depends on business line:
    Mining:  Month | Revenue | Utility-Nexbase | Rent or lease | Net
    Hosting: Month | Revenue (Northstar) | Utility-AA | Net
    Others / any other line: Month | Revenue | Costs | Net
- If currency was converted, use the converted currency in column headers (e.g. "Revenue (USD)")

NEVER collapse multiple rows into a single "Net Result" row as the only table row.
NEVER omit the Revenue:Realised or Revenue:Un-Realised rows if they appear in QB data.
NEVER omit the Utility-Nexbase or Rent or lease rows if they have non-zero values.

For SUMMARY GRID (/summary):
- report_type = "summary_grid"
- Populate business_lines dict: hosting / mining / others / total
- Each with revenue, costs, net

For MONTH-BY-MONTH Bills (multiple Bill query results — one per month):
- Each result covers one calendar month — label each row with the month name
- Columns: Month | Total Billed (MYR) | # Bills | Notes
- One row per month, sorted chronologically oldest first
- Add TOTAL row at bottom
- Notes column: flag unusually high or zero months
- If filtered to a specific vendor, scope to that vendor's bills only

For MONTH-BY-MONTH Invoices (multiple Invoice query results — one per month):
- Each result covers one calendar month — label each row with the month name
- Columns: Month | Total Invoiced (MYR) | # Invoices | Notes
- One row per month, sorted chronologically oldest first
- Add TOTAL row at bottom

For MONTH-BY-MONTH BillPayments (multiple BillPayment query results — one per month):
- Columns: Month | Total Paid (MYR) | # Payments | Notes
- One row per month, sorted chronologically oldest first
- Add TOTAL row at bottom

For CHAINED CALLS (P&L + Bill together):
- Use P&L for category totals
- Scan Bill line items for matching account names
- Build vendor table from matching bills

QB REPORT JSON STRUCTURE — read this carefully to avoid missing sections:
QB reports return deeply nested Rows. Walk ALL rows recursively:
- type "Data" rows: ColData[0].value = account name, ColData[1].value = amount
- type "Summary" rows: section subtotal label + amount
- type "Section" rows: contain nested Rows inside — ALWAYS recurse into them

For Balance Sheet specifically — these sections ALL exist and must ALL be read:
- Current Assets: bank accounts, receivables, BTC Available (Inventory), Accrued Revenue, Deposits, Prepayments
- Long-term Assets: equipment, accumulated depreciation (negative values)
- Current Liabilities: trade payables, accrued liabilities
- Long-term Liabilities: loans, deferred items
- Equity: share capital, retained earnings / accumulated deficit

NEVER stop after reading the first subtotal. NEVER skip a section. Every row in the JSON is real data.
If a section label is unfamiliar (e.g. "Deposit", "BTC Available", "Contra account") — include it, do not skip it.

Respond ONLY with valid JSON. No markdown, no backticks.
"""


def analyse(interpreter_result: dict) -> dict:
    """
    Main entry point.
    Takes interpreter result dict, calls Claude for analysis,
    returns structured analysis dict for slack_formatter.

    Returns:
        {
            "question": str,
            "query_complexity": str,
            "direct_answer": str,
            "key_findings": [str],
            "proactive_flags": [str],
            "summary_line": str,
            "has_detail_table": bool,
            "detail_table": {"headers": [], "rows": []},
            "data_note": str,
            "error": str | None
        }
    """
    question = interpreter_result.get("question", "")
    query_complexity = interpreter_result.get("query_complexity", "simple")
    results = interpreter_result.get("results", [])
    fetch_error = interpreter_result.get("error")

    # If interpreter failed entirely
    if fetch_error:
        return {
            "question": question,
            "query_complexity": "simple",
            "direct_answer": fetch_error,
            "key_findings": [],
            "proactive_flags": [],
            "summary_line": "Could not retrieve data from QuickBooks.",
            "has_detail_table": False,
            "detail_table": None,
            "data_note": "",
            "error": fetch_error,
        }

    # Build context for Claude — summarise what was fetched
    data_context = _build_data_context(results)

    # Inject resolved entity names so analyst knows what to filter for
    resolved_vendors = interpreter_result.get("resolved_vendors") or []
    resolved_customers = interpreter_result.get("resolved_customers") or []
    entity_context = ""
    if resolved_vendors:
        entity_context += f"\nResolved vendor name(s) to filter for: {', '.join(resolved_vendors)}"
    if resolved_customers:
        entity_context += f"\nResolved customer name(s) to filter for: {', '.join(resolved_customers)}"

    logger.info(f"Analysing QB data for: '{question}'{' | vendors: ' + str(resolved_vendors) if resolved_vendors else ''}")

    try:
        client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=6000,
            system=_build_analyst_system(),
            messages=[{
                "role": "user",
                "content": f"User question: {question}{entity_context}\n\nQuickBooks data:\n{data_context}"
            }],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        analysis = json.loads(raw)
        analysis["question"] = question
        analysis["query_complexity"] = query_complexity
        analysis["error"] = None
        # Ensure new fields have defaults if analyst didn't populate them
        analysis.setdefault("report_type", "standard")
        analysis.setdefault("business_lines", None)
        analysis.setdefault("currency", "MYR")

        logger.info(f"Analysis complete. Type: {analysis.get('report_type')} | Complexity: {query_complexity} | Flags: {len(analysis.get('proactive_flags', []))}")
        return analysis

    except json.JSONDecodeError as e:
        logger.error(f"Analyst JSON parse error: {e}")
        return _fallback_analysis(question, query_complexity, "Analysis formatting error. Raw data was retrieved.")
    except Exception as e:
        logger.error(f"Analyst error: {e}")
        return _fallback_analysis(question, query_complexity, str(e))


# Keywords that identify accounts which must never appear in the Mining segment.
# These are deliberately separated into their own QB accounts so operations
# can review a clean P&L without revaluation noise.
_MINING_EXCLUDED_KEYWORDS = [
    "fair value",
    "revaluation",
]


def _annotate_excluded_accounts(data: dict) -> dict:
    """
    Walk a ProfitAndLoss report JSON and append '[SEGMENT: Others — excluded from Mining]'
    to the account-name field of any row whose name matches a mining-excluded keyword.
    This gives Claude an unambiguous data-level signal, independent of the prompt.
    Mutates data in place — caller passes transient data that is never reused.
    """
    def _walk(node):
        if isinstance(node, dict):
            # Data rows: ColData[0].value = account name
            if node.get("type") == "Data":
                col_data = node.get("ColData", [])
                if col_data:
                    name = col_data[0].get("value", "")
                    if any(kw in name.lower() for kw in _MINING_EXCLUDED_KEYWORDS):
                        col_data[0]["value"] = f"{name} [SEGMENT: Others — excluded from Mining]"
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return data


def _build_data_context(results: list) -> str:
    """
    Convert raw QB API results into a readable context string for Claude.
    Reports get a larger budget than queries — balance sheets can be large.
    """
    QUERY_BUDGET = 6000    # Bills/invoices — individual records
    REPORT_BUDGET = 40000  # Reports (balance sheet, P&L) — needs full JSON
    parts = []

    for i, result in enumerate(results):
        call = result.get("call", {})
        data = result.get("data")
        error = result.get("error")

        if error:
            parts.append(f"[Call {i+1}: {call.get('type')} — ERROR: {error}]")
            continue

        if not data:
            parts.append(f"[Call {i+1}: {call.get('type')} — No data returned]")
            continue

        if call.get("type") == "exchangerate":
            src = call.get("source_currency", "USD")
            as_of = call.get("as_of_date", "")
            rate = data.get("ExchangeRate", {}).get("Rate", "unknown")
            parts.append(f"[Call {i+1}: ExchangeRate — 1 {src} = MYR {rate} (as of {as_of})]")
            parts.append(json.dumps(data, indent=2))

        elif call.get("type") == "query":
            query_response = data.get("QueryResponse", {})
            entity_data = {k: v for k, v in query_response.items()
                          if k not in ("startPosition", "maxResults", "totalCount")}

            total_count = query_response.get("totalCount", "unknown")
            entity_name = list(entity_data.keys())[0] if entity_data else "unknown"
            items = entity_data.get(entity_name, [])

            parts.append(f"[Call {i+1}: Query — {entity_name}, {total_count} total, returning {len(items[:100])}]")
            raw = json.dumps({entity_name: items[:100]}, indent=2)
            if len(raw) > QUERY_BUDGET:
                raw = raw[:QUERY_BUDGET] + "\n... [truncated]"
            parts.append(raw)

        elif call.get("type") == "report":
            report_name = call.get("report_name", "Report")
            params = call.get("params", {})
            label = f"{report_name}"
            if "start_date" in params:
                label += f" ({params['start_date']} to {params.get('end_date', '')})"
            parts.append(f"[Call {i+1}: Report — {label}]")
            if report_name == "ProfitAndLoss":
                data = _annotate_excluded_accounts(data)
            raw = json.dumps(data, indent=2)
            if len(raw) > REPORT_BUDGET:
                raw = raw[:REPORT_BUDGET] + "\n... [truncated]"
            parts.append(raw)

    return "\n\n".join(parts) if parts else "No data was returned from QuickBooks."


def _fallback_analysis(question: str, complexity: str, error_msg: str) -> dict:
    """Return a safe fallback when analysis fails."""
    return {
        "question": question,
        "query_complexity": complexity,
        "direct_answer": f"I retrieved data from QuickBooks but had trouble analysing it. Error: {error_msg}",
        "key_findings": [],
        "proactive_flags": [],
        "summary_line": "Analysis error — data was retrieved but could not be processed.",
        "has_detail_table": False,
        "detail_table": None,
        "data_note": error_msg,
        "error": error_msg,
    }