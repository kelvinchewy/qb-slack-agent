"""
qb_auditor.py — Context-aware output audit layer.

Two-layer architecture:
  Layer 1 — Python pre-checks: pure arithmetic invariants (fast, always run).
             Failures go straight to RETRY (structural corruption).
  Layer 2 — Haiku semantic check: scope, sign, figure quoting, entity matching.
             Uses question/resolved_vendors/reasoning from interpreter_result
             so no hard-coded heuristics are needed.

Verdicts:
  CLEAN  → pass through unchanged
  FIX    → Haiku rewrites direct_answer + key_findings only (table untouched)
  RETRY  → re-call analyst (Sonnet) with error context injected; max 1 retry
  FLAGGED → retry also failed; deliver with ⚠️ note in proactive_flags
"""

import json
import logging
import re

import anthropic

from config import Config
from table_utils import parse_amount as _parse_amount

logger = logging.getLogger(__name__)
_client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# Haiku system prompt
# ---------------------------------------------------------------------------

_AUDIT_SYSTEM = """You are a financial audit checker for a QuickBooks Slack agent.
Verify that an analyst's written output is correctly scoped to the user's question
and internally consistent with the table data. The table and business_lines are
authoritative — the prose must match them. Respond ONLY with valid JSON."""

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def audit(analysis: dict, interpreter_result: dict | None = None) -> dict:
    """
    Run the audit pipeline on the analyst output.

    Args:
        analysis:           JSON dict produced by qb_analyst.analyse()
        interpreter_result: raw QB data including question, resolved_vendors, reasoning

    Returns:
        Audited (possibly mutated) analysis dict.
    """
    if analysis.get("error") or not analysis.get("has_detail_table"):
        logger.info("Audit: skipped (error or no table)")
        return analysis

    report_type = analysis.get("report_type", "standard")

    # --- Layer 1: Python arithmetic pre-checks ---
    python_issues = _run_python_checks(analysis, report_type)
    if python_issues:
        logger.info(f"Audit Layer 1: RETRY — {len(python_issues)} arithmetic issue(s)")
        return _handle_retry(analysis, interpreter_result, python_issues, report_type, layer2=False)

    # --- Layer 2: Haiku semantic check ---
    haiku_result = _run_haiku_check(analysis, interpreter_result)
    if haiku_result is None:
        # Haiku failed — soft flag, Python pre-checks already passed
        logger.warning("Audit: Haiku check unavailable, adding soft flag")
        flags = list(analysis.get("proactive_flags", []))
        flags.insert(0, "⚠️ Audit skipped (semantic check unavailable)")
        analysis["proactive_flags"] = flags
        return analysis

    verdict = haiku_result.get("verdict", "CLEAN")
    raw_issues = haiku_result.get("issues", [])
    issue_strings = _format_issues(raw_issues)

    if verdict == "CLEAN" or not issue_strings:
        logger.info("Audit: CLEAN")
        return analysis

    logger.info(f"Audit: {verdict} — {len(issue_strings)} issue(s)")

    if verdict == "FIX":
        return _fix_prose(analysis, issue_strings)

    if verdict == "RETRY":
        return _handle_retry(analysis, interpreter_result, issue_strings, report_type, layer2=True)

    return analysis


# ---------------------------------------------------------------------------
# Retry handler (shared by Layer 1 and Layer 2 RETRY paths)
# ---------------------------------------------------------------------------

def _handle_retry(
    analysis: dict,
    interpreter_result: dict | None,
    issues: list[str],
    report_type: str,
    layer2: bool,
) -> dict:
    """Attempt a RETRY; fall back to FIX + FLAG if retry fails or unavailable."""
    if interpreter_result is not None:
        retried = _retry_analyst(interpreter_result, issues)
        if retried is not None:
            # Re-audit the retried result (Layers 1 + optionally 2, once only)
            retry_python = _run_python_checks(retried, report_type)
            if retry_python:
                logger.warning("Audit: RETRY → still failing (arithmetic), FIX + FLAG")
                retried = _fix_prose(retried, retry_python)
                return _add_audit_flag(retried, retry_python)

            if layer2:
                retry_haiku = _run_haiku_check(retried, interpreter_result)
                if retry_haiku is not None and retry_haiku.get("verdict") not in ("CLEAN", None):
                    retry_issues = _format_issues(retry_haiku.get("issues", []))
                    logger.warning("Audit: RETRY → still failing (semantic), FIX + FLAG")
                    retried = _fix_prose(retried, retry_issues)
                    return _add_audit_flag(retried, retry_issues)

            logger.info("Audit: RETRY → CLEAN on second attempt")
            return retried

    fixed = _fix_prose(analysis, issues)
    return _add_audit_flag(fixed, issues)


def _format_issues(raw_issues: list[dict]) -> list[str]:
    """Convert Haiku structured issue dicts to plain strings for existing helpers."""
    result = []
    for i in raw_issues:
        check = i.get("check", "")
        found = i.get("found", "")
        expected = i.get("expected", "")
        severity = i.get("severity", "FIX")
        result.append(f"[{severity}] {check}: found '{found}', expected '{expected}'")
    return result


# ---------------------------------------------------------------------------
# Layer 1 — Python arithmetic pre-checks
# ---------------------------------------------------------------------------

def _run_python_checks(analysis: dict, report_type: str) -> list[str]:
    """
    Pure arithmetic invariants only. Returns list of issue strings.
    All failures are RETRY class (structural data corruption).
    """
    issues: list[str] = []

    if report_type == "summary_grid":
        issues.extend(_check_summary_grid_arithmetic(analysis))
    elif report_type == "pnl_monthly":
        issues.extend(_check_pnl_monthly_arithmetic(analysis))
    else:
        # standard — could be balance sheet or bills
        issues.extend(_check_standard_arithmetic(analysis))

    return issues


def _check_summary_grid_arithmetic(analysis: dict) -> list[str]:
    """total.net must equal mining.net + others.net (±1)."""
    bl = analysis.get("business_lines", {})
    m_net = (bl.get("mining") or {}).get("net", 0) or 0
    o_net = (bl.get("others") or {}).get("net", 0) or 0
    t_net = (bl.get("total") or {}).get("net", 0) or 0

    computed = m_net + o_net
    if abs(computed - t_net) > 1:
        return [
            f"[RETRY] business_lines.total.net ({t_net:,.0f}) ≠ "
            f"mining.net ({m_net:,.0f}) + others.net ({o_net:,.0f}) = {computed:,.0f}."
        ]
    return []


def _check_pnl_monthly_arithmetic(analysis: dict) -> list[str]:
    """TOTAL row Net must equal sum of individual month Net column values (±1)."""
    table = analysis.get("detail_table", {})
    headers = table.get("headers", [])
    rows = table.get("rows", [])

    net_col = _col_index(headers, "net")
    if net_col is None:
        return []

    total_row = _find_total_row(rows)
    if total_row is None:
        return []

    data_rows = [r for r in rows if not _is_total_row(r) and not _is_blank_row(r)]
    data_nets = []
    for r in data_rows:
        if len(r) > net_col:
            val = _parse_amount(r[net_col])
            data_nets.append(val)

    if not data_nets:
        return []

    computed = sum(data_nets)
    total_net = _parse_amount(total_row[net_col]) if len(total_row) > net_col else 0.0

    if abs(computed - total_net) > 1:
        return [
            f"[RETRY] TOTAL row Net ({total_net:,.0f}) ≠ sum of monthly Net values "
            f"({computed:,.0f}). Table has arithmetic error."
        ]
    return []


def _check_standard_arithmetic(analysis: dict) -> list[str]:
    """Balance sheet: Assets = Liab + Equity (±1). Bills: Grand Total = Unpaid + Paid (±1)."""
    table = analysis.get("detail_table", {})
    rows = table.get("rows", [])

    row_labels = [str(r[0]).lower() if r else "" for r in rows]
    has_assets = any("asset" in l for l in row_labels)
    has_liab = any("liabilit" in l for l in row_labels)
    has_equity = any("equity" in l for l in row_labels)

    if has_assets and (has_liab or has_equity):
        assets = _find_row_amount(rows, "total assets") or _find_row_amount(rows, "assets")
        liabilities = _find_row_amount(rows, "total liabilit") or _find_row_amount(rows, "liabilit")
        equity = _find_row_amount(rows, "total equity") or _find_row_amount(rows, "equity")

        if assets is not None and liabilities is not None and equity is not None:
            rhs = liabilities + equity
            if abs(assets - rhs) > 1:
                return [
                    f"[RETRY] Balance sheet equation violated: Assets {assets:,.0f} ≠ "
                    f"Liabilities {liabilities:,.0f} + Equity {equity:,.0f} = {rhs:,.0f}. "
                    f"LLM likely misread QB JSON sections."
                ]
        return []

    # Bills/invoices
    unpaid = _find_row_amount(rows, "unpaid total")
    paid = _find_row_amount(rows, "paid total")
    grand = _find_row_amount(rows, "grand total")

    if unpaid is not None and paid is not None and grand is not None:
        computed = unpaid + paid
        if abs(computed - grand) > 1:
            return [
                f"[RETRY] Grand Total {grand:,.0f} ≠ Unpaid {unpaid:,.0f} + Paid {paid:,.0f} "
                f"= {computed:,.0f}."
            ]

    return []


# ---------------------------------------------------------------------------
# Layer 2 — Haiku semantic check
# ---------------------------------------------------------------------------

def _run_haiku_check(analysis: dict, interpreter_result: dict | None) -> dict | None:
    """
    Call Haiku to check scope, sign, figure quoting, and entity matching.
    Returns parsed JSON dict from Haiku, or None on error.
    """
    prompt = _build_haiku_prompt(analysis, interpreter_result)
    try:
        response = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=_AUDIT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        result = json.loads(raw)
        logger.info(f"Audit Haiku verdict: {result.get('verdict')} — {len(result.get('issues', []))} issue(s)")
        return result
    except Exception as e:
        logger.error(f"Audit Haiku check failed: {e}")
        return None


def _build_haiku_prompt(analysis: dict, interpreter_result: dict | None) -> str:
    """Build the token-budgeted prompt for the Haiku semantic check."""
    # Query context (from interpreter_result if available)
    question = ""
    resolved_vendors = []
    resolved_customers = []
    reasoning = ""
    if interpreter_result:
        question = interpreter_result.get("question", "") or interpreter_result.get("user_question", "") or ""
        resolved_vendors = interpreter_result.get("resolved_vendors", []) or []
        resolved_customers = interpreter_result.get("resolved_customers", []) or []
        reasoning = interpreter_result.get("reasoning", "") or ""

    report_type = analysis.get("report_type", "standard")
    bl_json = json.dumps(analysis.get("business_lines", {}), separators=(",", ":"))
    key_rows = _extract_key_rows(analysis)
    key_rows_text = json.dumps(key_rows, separators=(",", ":"))
    direct_answer = analysis.get("direct_answer", "")
    key_findings = json.dumps(analysis.get("key_findings", []), separators=(",", ":"))

    prompt = f"""Check the analyst output below for correctness.

=== QUERY CONTEXT (authoritative scope) ===
question: {question}
resolved_vendors: {json.dumps(resolved_vendors)}
resolved_customers: {json.dumps(resolved_customers)}
reasoning: {reasoning}
report_type: {report_type}

=== AUTHORITATIVE DATA ===
business_lines: {bl_json}
key_table_rows: {key_rows_text}

=== ANALYST OUTPUT TO CHECK ===
direct_answer: {direct_answer}
key_findings: {key_findings}

=== CHECKS TO PERFORM ===
1. SCOPE: Does the prose reference the correct entity/business-line for the question? (e.g., if question asks about "others", prose must not say "mining")
2. NET VALUE: Does the key figure in direct_answer match the authoritative net in business_lines?
3. SIGN: If net < 0, does prose say "loss" and not "profit"?
4. FIGURES: Do numbers cited in key_findings contradict the key table rows?
5. PERCENTAGES: Do % figures in key_findings differ from the table by > 0.5%?

Respond ONLY with this JSON (no markdown):
{{
  "verdict": "CLEAN | FIX | RETRY",
  "issues": [
    {{
      "check": "SCOPE | NET VALUE | SIGN | FIGURES | PERCENTAGES",
      "found": "what the analyst said",
      "expected": "what it should say",
      "severity": "FIX | RETRY"
    }}
  ]
}}

If any issue has severity RETRY, set verdict to RETRY.
If no issues, set verdict to CLEAN and issues to [].
"""
    return prompt


def _extract_key_rows(analysis: dict) -> list:
    """Return NET/TOTAL/GRAND TOTAL rows + first 5 non-blank data rows."""
    table = analysis.get("detail_table", {})
    rows = table.get("rows", [])
    headers = table.get("headers", [])

    key = []
    data_count = 0
    total_labels = {"total", "grand total", "total:"}

    for row in rows:
        if not row or _is_blank_row(row):
            continue
        label = str(row[0]).strip().lower()
        if label in total_labels or "net result" in label:
            key.append(row)
        elif data_count < 5:
            key.append(row)
            data_count += 1

    # Prepend headers for context
    if headers:
        return [headers] + key
    return key


# ---------------------------------------------------------------------------
# FIX: rewrite prose via Haiku
# ---------------------------------------------------------------------------

def _fix_prose(analysis: dict, issues: list[str]) -> dict:
    """Ask Haiku to rewrite direct_answer and key_findings to match the table."""
    issues_text = "\n".join(f"- {i}" for i in issues)
    table_json = json.dumps(analysis.get("detail_table", {}), separators=(",", ":"))
    bl_json = json.dumps(analysis.get("business_lines", {}), separators=(",", ":"))

    prompt = f"""You are correcting errors in a financial report. The table data is authoritative.
Rewrite ONLY `direct_answer` and `key_findings` so they match the table exactly.
Do NOT change any numbers in the table, business_lines, or any other field.

ISSUES FOUND:
{issues_text}

AUTHORITATIVE TABLE DATA:
{table_json}

AUTHORITATIVE BUSINESS LINES:
{bl_json}

CURRENT direct_answer:
{analysis.get("direct_answer", "")}

CURRENT key_findings:
{json.dumps(analysis.get("key_findings", []))}

Respond with ONLY a JSON object with two keys:
{{
  "direct_answer": "corrected text — max 2 sentences, lead with the key number",
  "key_findings": ["corrected finding 1", "corrected finding 2", ...]
}}"""

    try:
        response = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        patch = json.loads(raw)
        analysis["direct_answer"] = patch.get("direct_answer", analysis["direct_answer"])
        analysis["key_findings"] = patch.get("key_findings", analysis["key_findings"])
        logger.info("Audit FIX: prose rewritten by Haiku")
    except Exception as e:
        logger.error(f"Audit FIX failed: {e}")

    return analysis


# ---------------------------------------------------------------------------
# RETRY: re-call Sonnet analyst with error context
# ---------------------------------------------------------------------------

def _retry_analyst(interpreter_result: dict, issues: list[str]) -> dict | None:
    """Re-call the Sonnet analyst with audit findings injected as context."""
    try:
        from qb_analyst import analyse
        issues_text = "\n".join(f"- {i}" for i in issues)
        retry_result = dict(interpreter_result)
        existing_note = retry_result.get("audit_correction_note", "")
        retry_result["audit_correction_note"] = (
            f"{existing_note}\n\nPREVIOUS ATTEMPT AUDIT FAILURES — fix these:\n{issues_text}"
        ).strip()
        logger.info("Audit RETRY: re-calling analyst with correction context")
        return analyse(retry_result)
    except Exception as e:
        logger.error(f"Audit RETRY failed: {e}")
        return None


# ---------------------------------------------------------------------------
# FLAG: add audit warning to proactive_flags
# ---------------------------------------------------------------------------

def _add_audit_flag(analysis: dict, issues: list[str]) -> dict:
    flags = list(analysis.get("proactive_flags", []))
    summary = "; ".join(issues[:2])
    flags.insert(0, f"⚠️ Audit: figures may be inconsistent — {summary}")
    analysis["proactive_flags"] = flags
    return analysis


# ---------------------------------------------------------------------------
# Table parsing helpers
# ---------------------------------------------------------------------------

def _col_index(headers: list, keyword: str) -> int | None:
    keyword = keyword.lower()
    for i, h in enumerate(headers):
        if keyword in str(h).lower():
            return i
    return None


def _find_total_row(rows: list) -> list | None:
    for row in rows:
        if row and _is_total_row(row):
            return row
    return None


def _is_total_row(row: list) -> bool:
    return bool(row) and str(row[0]).strip().upper() in ("TOTAL", "GRAND TOTAL", "TOTAL:")


def _is_blank_row(row: list) -> bool:
    return not row or all(str(c).strip() == "" for c in row)


def _find_row_amount(rows: list, keyword: str) -> float | None:
    keyword = keyword.lower()
    for row in rows:
        if not row:
            continue
        label = str(row[0]).lower()
        if keyword in label:
            for cell in row[1:]:
                val = _parse_amount(cell)
                if val != 0.0:
                    return val
            return 0.0
    return None


def _extract_number_from_prose(text: str, context: str | None = None) -> float | None:
    if context:
        pos = text.lower().find(context.lower())
        if pos != -1:
            text = text[pos:]
    pattern = r'(?:MYR\s*)?(?:\((\d[\d,]*(?:\.\d+)?)\)|([+-]?\d[\d,]*(?:\.\d+)?))'
    for m in re.finditer(pattern, text):
        if m.group(1):
            return -_parse_amount(m.group(1))
        val = _parse_amount(m.group(2))
        if abs(val) >= 100:
            return val
    return None
