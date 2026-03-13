"""
qb_analyst.py — Analysis and intelligence layer.

Takes raw QB data from qb_interpreter and produces:
- Direct answer to the user's question
- Key insights and patterns
- Proactive flags (upcoming bills, anomalies, cashflow risks)
- Plain English narrative — CFO-level insight, no jargon

Output is structured for slack_formatter to render into Block Kit.
"""

import json
import logging
from datetime import datetime
import anthropic
from config import Config

logger = logging.getLogger(__name__)

TODAY = datetime.now().strftime("%B %d, %Y")

ANALYST_SYSTEM = f"""You are a sharp CFO-level financial analyst for The Hashing Company, a Bitcoin mining company with ~200 ASIC machines across 2 sites in Singapore.

Today is {TODAY}. Currency: use whatever currency appears in QB (MYR, USD, etc) — never convert or assume.

RULES — follow these strictly:
1. NEVER infer or estimate. If data is not in QB, say "not found in QuickBooks" — never fill gaps.
2. ALWAYS use the exact QB account names as they appear in the data. Never rename or remap accounts.
3. Keep direct_answer to 2 sentences maximum. Lead with the single most important number.
4. Put all breakdown detail in the detail_table — not in the prose.
5. Add a percentage of total for any breakdown table (assets, expenses, etc).
6. data_completeness must be one of: "complete", "partial", "incomplete"

Respond with this JSON:

{{
  "direct_answer": "MAX 2 sentences. Lead with the key number. Example: 'Total assets MYR 5.08M as of Mar 10 2026. Zero liabilities — 100% equity financed.'",
  "key_findings": ["3 findings max. Short. One insight per bullet. Include % or ratio where possible."],
  "proactive_flags": ["Only real actionable issues. Empty [] if none."],
  "summary_line": "Under 80 chars. The one thing a CFO needs to know.",
  "has_detail_table": true,
  "detail_table": {{
    "headers": ["Account", "Amount", "%"],
    "rows": [["BTC Available (Inventory)", "MYR 1,346,697", "26.5%"]]
  }},
  "data_completeness": "complete | partial | incomplete",
  "data_note": "Only if something is missing or unclear. Empty string if clean."
}}

You will also receive a "Query intent" field — either RETRIEVAL or FORECAST_TREND.

For RETRIEVAL with chained calls (P&L + Bill query together):
- Use the P&L result for the expense category total
- The Bill query returns ALL bills for the period — scan each bill's line items for the relevant account name
- Build the vendor table from bills whose line items match the expense category (e.g. "Utilities", "Rent")
- Detail table: Vendor, Date, Amount, Status — sorted by amount descending
- If no bills match the category: say "No Bills recorded under this category in QB for this period — expenses may be entered via bank feed (Purchase entity) rather than as vendor Bills"
- If Bill query returned zero records at all: say "No Bills found for this period in QB"
- Never say "query limitations" or "API restrictions" — explain specifically what was found and what wasn't

For RETRIEVAL with vendor/customer name search:
- The interpreter may have already resolved the vendor/customer name — check for "resolved_vendors" or "resolved_customers" in the data context
- If resolved names are present, filter results to only those vendors/customers and note which QB name matched the user's search term
- If no resolved names (unmatched search), scan ALL returned bills/invoices and surface any plausible partial matches
- Check BOTH VendorRef.name AND line item descriptions for matches
- Always tell the user what QB vendor/customer name was matched against their search term
- Never return zero results if there are plausible partial matches in the data

For TOP VENDORS BY SPEND:
- From the Bill results, group bills by VendorRef.name and sum TotalAmt per vendor
- Sort descending by total spend
- Detail table: Rank, Vendor, Total Amount, # Bills, % of Total Spend
- Lead with: "Top N vendors account for X% of total spend of MYR Y"

For LARGE TRANSACTIONS (amount threshold):
- List all bills returned (already filtered by TotalAmt in the SQL)
- Detail table: Date, Vendor, Amount, Status, Due Date
- Sort by amount descending
- Note the threshold used in the direct_answer

For NEW VENDOR DETECTION (two-period Bill queries):
- Compare vendor sets across the two periods
- New vendors = appear in period 2 (recent) but NOT in period 1 (prior)
- Detail table: Vendor, First Bill Date, Total Amount, # Bills
- Also note vendors that stopped appearing (churned vendors)

For BILLPAYMENT queries:
- BillPayment records when money actually left the account (vs Bill = when obligation was recorded)
- Detail table: Date, Vendor, Amount, Payment Method
- Note any Bills that are recorded but NOT yet paid (outstanding)

For FORECAST_TREND:
- State the trend first: "Utilities averaged MYR 194K/month over 3 months"
- Then the forecast: "March forecast: MYR 190K-200K based on run rate"
- Detail table must include: Expense Category, each month's actual, 3-month avg, forecast
- Flag any anomalous months explicitly (e.g. one month unusually low/high)
- If a month shows zero or near-zero for a normally large category — flag as potential missing data, not real zero

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
    intent = interpreter_result.get("intent", "RETRIEVAL")
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
            max_tokens=2000,
            system=ANALYST_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"User question: {question}\n\nQuery intent: {intent}{entity_context}\n\nQuickBooks data:\n{data_context}"
            }],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        analysis = json.loads(raw)
        analysis["question"] = question
        analysis["query_complexity"] = query_complexity
        analysis["error"] = None

        logger.info(f"Analysis complete. Complexity: {query_complexity}, Flags: {len(analysis.get('proactive_flags', []))}")
        return analysis

    except json.JSONDecodeError as e:
        logger.error(f"Analyst JSON parse error: {e}")
        return _fallback_analysis(question, query_complexity, "Analysis formatting error. Raw data was retrieved.")
    except Exception as e:
        logger.error(f"Analyst error: {e}")
        return _fallback_analysis(question, query_complexity, str(e))


def _build_data_context(results: list) -> str:
    """
    Convert raw QB API results into a readable context string for Claude.
    Applies a per-call character budget to prevent context overflow on
    multi-month forecast queries (6 P&L reports = ~48K chars uncapped).
    """
    # Budget: 6000 chars per call, max 10 calls = 60K total (safe for Claude context)
    PER_CALL_BUDGET = 6000
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

        if call.get("type") == "query":
            query_response = data.get("QueryResponse", {})
            entity_data = {k: v for k, v in query_response.items()
                          if k not in ("startPosition", "maxResults", "totalCount")}

            total_count = query_response.get("totalCount", "unknown")
            entity_name = list(entity_data.keys())[0] if entity_data else "unknown"
            items = entity_data.get(entity_name, [])

            parts.append(f"[Call {i+1}: Query — {entity_name}, {total_count} total, returning {len(items[:50])}]")
            raw = json.dumps({entity_name: items[:50]}, indent=2)
            if len(raw) > PER_CALL_BUDGET:
                raw = raw[:PER_CALL_BUDGET] + "\n... [truncated]"
            parts.append(raw)

        elif call.get("type") == "report":
            report_name = call.get("report_name", "Report")
            params = call.get("params", {})
            label = f"{report_name}"
            if "start_date" in params:
                label += f" ({params['start_date']} to {params.get('end_date', '')})"
            parts.append(f"[Call {i+1}: Report — {label}]")
            raw = json.dumps(data, indent=2)
            if len(raw) > PER_CALL_BUDGET:
                raw = raw[:PER_CALL_BUDGET] + "\n... [truncated]"
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