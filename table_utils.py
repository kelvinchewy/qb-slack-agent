"""
table_utils.py — Shared table-parsing utilities.

Used by qb_analyst.py (arithmetic post-processor) and qb_auditor.py (audit checks).
"""

from typing import Any


def parse_amount(s: Any) -> float:
    """Parse cell values like '1,234,567', '-88,538', '+164,952', '(88,538)', 'MYR 1,234' to float."""
    s = str(s).strip().replace("MYR", "").replace(",", "").replace("+", "").strip()
    # Accounting parentheses notation: (88,538) → -88538
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    if not s or s in ("-", "—", "–", ""):
        return 0.0
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def fmt_int(v: float) -> str:
    """Format as plain comma-separated integer (no currency prefix)."""
    return f"{int(round(v)):,}"
