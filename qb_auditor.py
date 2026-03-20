"""
qb_auditor.py — Output audit layer (Sprint 6).

Sits between analyse() and format_dynamic_analysis() in report_builder.py.
Uses Haiku (fast, cheap) to check whether analyst prose contradicts table data.

Verdicts:
  CLEAN  → pass through unchanged
  FIX    → auditor rewrites direct_answer + key_findings only (table untouched)
  RETRY  → re-call analyst (Sonnet) with error context injected; max 1 retry
  FLAGGED → retry also failed; deliver with ⚠️ note in proactive_flags

The Python post-processor in qb_analyst.py already handles arithmetic.
The auditor handles qualitative claims, figure quoting, and cross-field consistency.
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
# Public entry point
# ---------------------------------------------------------------------------

def audit(analysis: dict, interpreter_result: dict | None = None) -> dict:
    """
    Run the audit pipeline on the analyst output.

    Args:
        analysis:          JSON dict produced by qb_analyst.analyse()
        interpreter_result: raw QB data (needed for RETRY — re-call analyst)

    Returns:
        Audited (possibly mutated) analysis dict.
    """
    # Skip audit if the analysis itself errored — nothing useful to check
    if analysis.get("error") or not analysis.get("has_detail_table"):
        logger.info("Audit: skipped (error or no table)")
        return analysis

    report_type = analysis.get("report_type", "standard")

    findings = _run_checks(analysis, report_type)

    if not findings:
        logger.info("Audit: CLEAN")
        return analysis

    verdict, issues = _decide_verdict(findings)
    logger.info(f"Audit: {verdict.upper()} — {len(issues)} issue(s)")

    if verdict == "fix":
        return _fix_prose(analysis, issues)

    if verdict == "retry" and interpreter_result is not None:
        retried = _retry_analyst(interpreter_result, issues)
        if retried is not None:
            retry_findings = _run_checks(retried, report_type)
            if not retry_findings:
                logger.info("Audit: RETRY → CLEAN on second attempt")
                return retried
            # Retry also failed → fix what we can and flag
            logger.warning("Audit: RETRY → still failing, falling back to FIX + FLAG")
            retried = _fix_prose(retried, retry_findings)
            return _add_audit_flag(retried, retry_findings)

    # RETRY without interpreter_result, or retry failed completely
    fixed = _fix_prose(analysis, issues)
    return _add_audit_flag(fixed, issues)


# ---------------------------------------------------------------------------
# Check dispatch
# ---------------------------------------------------------------------------

def _run_checks(analysis: dict, report_type: str) -> list[str]:
    """Run all applicable checks and return list of issue strings."""
    issues: list[str] = []

    if report_type == "pnl_by_line":
        issues.extend(_check_pnl_by_line(analysis))
    elif report_type == "pnl_monthly":
        issues.extend(_check_pnl_monthly(analysis))
    elif report_type == "summary_grid":
        issues.extend(_check_summary_grid(analysis))
    else:
        # standard — could be balance sheet, bills, invoices
        issues.extend(_check_standard(analysis))

    return issues


# ---------------------------------------------------------------------------
# Check: pnl_by_line (single-period P&L)
# ---------------------------------------------------------------------------

def _check_pnl_by_line(analysis: dict) -> list[str]:
    issues: list[str] = []
    bl = analysis.get("business_lines", {})
    mining = bl.get("mining", {})
    table = analysis.get("detail_table", {})
    rows = table.get("rows", [])
    direct = analysis.get("direct_answer", "")

    # 1. business_lines.mining.net vs NET RESULT row in detail_table
    table_net = _find_net_result_in_rows(rows)
    bl_net = mining.get("net")
    if table_net is not None and bl_net is not None:
        if abs(table_net - bl_net) > 1:
            issues.append(
                f"business_lines.mining.net ({bl_net:,.0f}) does not match "
                f"NET RESULT row in detail_table ({table_net:,.0f}). "
                f"Prose badges show wrong number."
            )

    # 2. direct_answer net figure matches bl_net
    if bl_net is not None:
        prose_net = _extract_number_from_prose(direct)
        if prose_net is not None and abs(prose_net - bl_net) > 1:
            issues.append(
                f"direct_answer mentions {prose_net:,.0f} but business_lines.mining.net "
                f"is {bl_net:,.0f}. Prose must copy from table."
            )

    # 3. Sign: if net < 0, prose must say "loss" not "profit"
    if bl_net is not None and bl_net < 0:
        if re.search(r'\bprofit\b', direct, re.IGNORECASE) and not re.search(r'\bloss\b', direct, re.IGNORECASE):
            issues.append(
                f"Net is negative ({bl_net:,.0f}) but direct_answer says 'profit'. Should say 'loss'."
            )

    # 4. % figures in key_findings must match % of Total column in table
    issues.extend(_check_pct_consistency(analysis))

    return issues


# ---------------------------------------------------------------------------
# Check: pnl_monthly (month-by-month P&L)
# ---------------------------------------------------------------------------

def _check_pnl_monthly(analysis: dict) -> list[str]:
    issues: list[str] = []
    bl = analysis.get("business_lines", {})
    mining = bl.get("mining", {})
    table = analysis.get("detail_table", {})
    headers = table.get("headers", [])
    rows = table.get("rows", [])
    direct = analysis.get("direct_answer", "")

    total_row = _find_total_row(rows)
    net_col = _col_index(headers, "net")

    # 1. direct_answer total net matches TOTAL row Net cell
    if total_row and net_col is not None:
        total_net = _parse_amount(total_row[net_col])
        prose_net = _extract_number_from_prose(direct)
        if prose_net is not None and abs(prose_net - total_net) > 1:
            issues.append(
                f"direct_answer mentions {prose_net:,.0f} but TOTAL row Net is "
                f"{total_net:,.0f}. Prose must copy from table."
            )

    # 2. Best/worst month claim must match actual best/worst in Net column
    if net_col is not None:
        data_rows = [r for r in rows if not _is_total_row(r) and not _is_blank_row(r)]
        if data_rows:
            nets = [(r, _parse_amount(r[net_col])) for r in data_rows if len(r) > net_col]
            if nets:
                worst_row, worst_val = min(nets, key=lambda x: x[1])
                best_row, best_val = max(nets, key=lambda x: x[1])
                # Check prose mentions the correct worst month value
                for label in ["worst", "lowest"]:
                    if label in direct.lower():
                        prose_worst = _extract_number_from_prose(direct, context=label)
                        if prose_worst is not None and abs(prose_worst - worst_val) > 1:
                            issues.append(
                                f"direct_answer worst-month figure {prose_worst:,.0f} does not match "
                                f"actual worst month in Net column ({worst_val:,.0f})."
                            )
                        break

    # 3. TOTAL row internal consistency: Revenue − Costs = Net
    if total_row:
        rev_col = _col_index(headers, "revenue")
        cost_col = _col_index(headers, "total costs") or _col_index(headers, "total cost") or _col_index(headers, "costs")
        if net_col is not None and rev_col is not None and cost_col is not None:
            t_rev = _parse_amount(total_row[rev_col])
            t_cost = _parse_amount(total_row[cost_col])
            t_net = _parse_amount(total_row[net_col])
            computed = t_rev - t_cost
            if abs(computed - t_net) > 1:
                issues.append(
                    f"TOTAL row inconsistent: Revenue {t_rev:,.0f} − Costs {t_cost:,.0f} "
                    f"= {computed:,.0f} but Net column shows {t_net:,.0f}."
                )

    return issues


# ---------------------------------------------------------------------------
# Check: balance sheet (standard report with Assets/Liabilities/Equity)
# ---------------------------------------------------------------------------

def _check_standard(analysis: dict) -> list[str]:
    issues: list[str] = []
    table = analysis.get("detail_table", {})
    rows = table.get("rows", [])
    direct = analysis.get("direct_answer", "")

    # Detect balance sheet by looking for Assets + Liabilities rows
    row_labels = [str(r[0]).lower() if r else "" for r in rows]
    has_assets = any("asset" in l for l in row_labels)
    has_liab = any("liabilit" in l for l in row_labels)
    has_equity = any("equity" in l for l in row_labels)

    if has_assets and (has_liab or has_equity):
        issues.extend(_check_balance_sheet(rows, direct))
        return issues

    # Bills/invoices: check Grand Total = Unpaid + Paid
    issues.extend(_check_bills_totals(rows, direct))

    return issues


def _check_balance_sheet(rows: list, direct: str) -> list[str]:
    issues: list[str] = []
    assets = _find_row_amount(rows, "total assets") or _find_row_amount(rows, "assets")
    liabilities = _find_row_amount(rows, "total liabilities") or _find_row_amount(rows, "liabilit")
    equity = _find_row_amount(rows, "total equity") or _find_row_amount(rows, "equity")

    # 1. Accounting identity: Assets = Liabilities + Equity
    if assets is not None and liabilities is not None and equity is not None:
        rhs = liabilities + equity
        if abs(assets - rhs) > 1:
            issues.append(
                f"Balance sheet equation violated: Assets {assets:,.0f} ≠ "
                f"Liabilities {liabilities:,.0f} + Equity {equity:,.0f} = {rhs:,.0f}. "
                f"LLM likely misread QB JSON sections — requires RETRY."
            )

    # 2. direct_answer asset figure matches table
    if assets is not None:
        prose_val = _extract_number_from_prose(direct)
        if prose_val is not None and abs(prose_val - assets) > assets * 0.02:
            issues.append(
                f"direct_answer mentions {prose_val:,.0f} but table total assets = {assets:,.0f}."
            )

    return issues


def _check_bills_totals(rows: list, direct: str) -> list[str]:
    issues: list[str] = []
    unpaid = _find_row_amount(rows, "unpaid total")
    paid = _find_row_amount(rows, "paid total")
    grand = _find_row_amount(rows, "grand total")

    if unpaid is not None and paid is not None and grand is not None:
        computed = unpaid + paid
        if abs(computed - grand) > 1:
            issues.append(
                f"Grand Total {grand:,.0f} ≠ Unpaid {unpaid:,.0f} + Paid {paid:,.0f} = {computed:,.0f}."
            )

    # direct_answer should lead with unpaid amount
    if unpaid is not None and unpaid > 0:
        prose_val = _extract_number_from_prose(direct)
        if prose_val is not None and abs(prose_val - unpaid) > unpaid * 0.02:
            issues.append(
                f"direct_answer leads with {prose_val:,.0f} but UNPAID TOTAL = {unpaid:,.0f}."
            )

    return issues


# ---------------------------------------------------------------------------
# Check: summary_grid
# ---------------------------------------------------------------------------

def _check_summary_grid(analysis: dict) -> list[str]:
    issues: list[str] = []
    bl = analysis.get("business_lines", {})
    mining = bl.get("mining", {})
    others = bl.get("others", {})
    total = bl.get("total", {})
    direct = analysis.get("direct_answer", "")

    m_net = mining.get("net", 0) or 0
    o_net = others.get("net", 0) or 0
    t_net = total.get("net", 0) or 0

    # 1. total.net = mining.net + others.net
    computed = m_net + o_net
    if abs(computed - t_net) > 1:
        issues.append(
            f"total.net ({t_net:,.0f}) ≠ mining.net ({m_net:,.0f}) + others.net ({o_net:,.0f}) "
            f"= {computed:,.0f}."
        )

    # 2. direct_answer figure matches total.net
    prose_val = _extract_number_from_prose(direct)
    if prose_val is not None and abs(prose_val - t_net) > 1:
        issues.append(
            f"direct_answer mentions {prose_val:,.0f} but total.net = {t_net:,.0f}."
        )

    return issues


# ---------------------------------------------------------------------------
# % consistency check (shared for pnl_by_line)
# ---------------------------------------------------------------------------

def _check_pct_consistency(analysis: dict) -> list[str]:
    """Check that % figures in key_findings match % of Total column in detail_table."""
    issues: list[str] = []
    table = analysis.get("detail_table", {})
    headers = table.get("headers", [])
    rows = table.get("rows", [])
    findings = analysis.get("key_findings", [])

    pct_col = _col_index(headers, "% of total") or _col_index(headers, "%")
    if pct_col is None:
        return issues

    # Build set of (rounded) % values that appear in the table
    table_pcts: set[float] = set()
    for row in rows:
        if len(row) > pct_col:
            val = row[pct_col]
            # e.g. "82.9%" → 82.9
            m = re.search(r'([\d.]+)%', str(val))
            if m:
                table_pcts.add(round(float(m.group(1)), 1))

    if not table_pcts:
        return issues

    for finding in findings:
        # Extract all % mentions from the finding text
        for m in re.finditer(r'([\d.]+)%', finding):
            pct = round(float(m.group(1)), 1)
            # Allow ±0.5 rounding tolerance
            if not any(abs(pct - t) <= 0.5 for t in table_pcts):
                issues.append(
                    f"key_findings mentions {pct}% but no matching value found in '% of Total' "
                    f"column (table has: {sorted(table_pcts)}). Copy exact value from table."
                )

    return issues


# ---------------------------------------------------------------------------
# Verdict decision
# ---------------------------------------------------------------------------

def _decide_verdict(issues: list[str]) -> tuple[str, list[str]]:
    """
    Return (verdict, issues).
    RETRY if any issue mentions 'RETRY' or 'balance sheet equation' (structural).
    FIX otherwise.
    """
    retry_keywords = ["retry", "balance sheet equation", "misread qb json"]
    for issue in issues:
        if any(kw in issue.lower() for kw in retry_keywords):
            return "retry", issues
    return "fix", issues


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
        # Strip markdown code fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        patch = json.loads(raw)
        analysis["direct_answer"] = patch.get("direct_answer", analysis["direct_answer"])
        analysis["key_findings"] = patch.get("key_findings", analysis["key_findings"])
        logger.info("Audit FIX: prose rewritten by Haiku")
    except Exception as e:
        logger.error(f"Audit FIX failed: {e}")
        # Return original — better than crashing

    return analysis


# ---------------------------------------------------------------------------
# RETRY: re-call Sonnet analyst with error context
# ---------------------------------------------------------------------------

def _retry_analyst(interpreter_result: dict, issues: list[str]) -> dict | None:
    """Re-call the Sonnet analyst with audit findings injected as context."""
    try:
        from qb_analyst import analyse
        issues_text = "\n".join(f"- {i}" for i in issues)
        # Inject audit findings into interpreter_result context
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
    summary = "; ".join(issues[:2])  # Show first 2 issues in the flag
    flags.insert(0, f"⚠️ Audit: figures may be inconsistent — {summary}")
    analysis["proactive_flags"] = flags
    return analysis


# ---------------------------------------------------------------------------
# Table parsing helpers
# ---------------------------------------------------------------------------

def _col_index(headers: list, keyword: str) -> int | None:
    """Find the first header column whose lower-case text contains keyword."""
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


def _find_net_result_in_rows(rows: list) -> float | None:
    """Find the NET RESULT row and return its amount value."""
    net_labels = {"NET RESULT", "NET RESULT:", "MINING NET", "OTHERS NET", "COMBINED NET"}
    for row in rows:
        if not row:
            continue
        label = str(row[0]).strip().upper()
        if label in net_labels or "NET RESULT" in label:
            # Amount is typically in column 1
            for cell in row[1:]:
                val = _parse_amount(cell)
                if val != 0.0:
                    return val
            return 0.0
    return None


def _find_row_amount(rows: list, keyword: str) -> float | None:
    """Find first row whose label contains keyword (case-insensitive), return its amount.
    Returns 0.0 if the row is found but all cells are zero/empty. Returns None if row not found.
    """
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
            return 0.0  # Row found, all cells zero
    return None  # Row not found


def _extract_number_from_prose(text: str, context: str | None = None) -> float | None:
    """
    Extract the first (or context-adjacent) significant number from prose text.
    Handles MYR prefix, commas, parentheses (negative), and sign prefix.
    Returns None if no number found.
    """
    if context:
        # Find the number closest after the context keyword
        pos = text.lower().find(context.lower())
        if pos != -1:
            text = text[pos:]

    # Match: optional MYR, optional sign, digits with optional commas, optional decimals
    # Also match parenthesised numbers like (528,000)
    pattern = r'(?:MYR\s*)?(?:\((\d[\d,]*(?:\.\d+)?)\)|([+-]?\d[\d,]*(?:\.\d+)?))'
    for m in re.finditer(pattern, text):
        if m.group(1):  # parenthesised = negative
            return -_parse_amount(m.group(1))
        val = _parse_amount(m.group(2))
        if abs(val) >= 100:  # ignore tiny numbers (percentages, counts)
            return val
    return None
