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

For RETRIEVAL:
- Describe what you actually see in the QB data using real account names
- Balance sheet / P&L: breakdown table with % of total column
- Transaction lists: Date, Vendor/Customer, Amount, Status columns
- Cash/AR/AP: sorted by amount descending

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

    logger.info(f"Analysing QB data for: '{question}'")

    try:
        client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=ANALYST_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"User question: {question}\n\nQuery intent: {intent}\n\nQuickBooks data:\n{data_context}"
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
    Trims oversized responses to avoid context overflow.
    """
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

        # For query results, extract the entity list
        if call.get("type") == "query":
            query_response = data.get("QueryResponse", {})
            # Find the first non-metadata key (the entity list)
            entity_data = {k: v for k, v in query_response.items()
                          if k not in ("startPosition", "maxResults", "totalCount")}

            total_count = query_response.get("totalCount", "unknown")
            entity_name = list(entity_data.keys())[0] if entity_data else "unknown"
            items = entity_data.get(entity_name, [])

            parts.append(f"[Call {i+1}: Query — {entity_name}, {total_count} total results, returning {len(items)}]")

            # Include full data but cap at 50 items to avoid context overflow
            capped = items[:50]
            parts.append(json.dumps({entity_name: capped}, indent=2))

        # For report results, include the full report (they're already structured)
        elif call.get("type") == "report":
            report_name = call.get("report_name", "Report")
            parts.append(f"[Call {i+1}: Report — {report_name}]")
            # Reports can be large — include but truncate raw rows if massive
            report_str = json.dumps(data, indent=2)
            if len(report_str) > 8000:
                report_str = report_str[:8000] + "\n... [truncated for length]"
            parts.append(report_str)

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