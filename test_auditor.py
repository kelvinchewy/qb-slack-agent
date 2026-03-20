"""
test_auditor.py — Tests the auditor logic with hardcoded analysis data.
No QB connection or Slack required.

Run with: source venv/bin/activate && python test_auditor.py
"""

import json
from dotenv import load_dotenv
load_dotenv()

from qb_auditor import audit

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def run_test(name: str, analysis: dict) -> None:
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print('='*60)

    original_answer = analysis.get("direct_answer", "")
    original_findings = list(analysis.get("key_findings", []))

    result = audit(analysis)

    answer_changed = result.get("direct_answer") != original_answer
    findings_changed = result.get("key_findings") != original_findings
    flags = result.get("proactive_flags", [])

    if answer_changed:
        print(f"✅ direct_answer FIXED")
        print(f"   BEFORE: {original_answer}")
        print(f"   AFTER:  {result.get('direct_answer')}")
    elif flags and any("⚠️ Audit" in f for f in flags):
        print(f"⚠️  FLAGGED (could not fix)")
    else:
        print(f"✅ CLEAN (no issues found)")

    if findings_changed:
        print(f"\nkey_findings changed:")
        for f in result.get("key_findings", []):
            print(f"  • {f}")


# ---------------------------------------------------------------------------
# Test 1: pnl_by_line — prose says wrong net (should be caught and fixed)
# ---------------------------------------------------------------------------

run_test("pnl_by_line: prose has wrong NET RESULT figure", {
    "report_type": "pnl_by_line",
    "has_detail_table": True,
    "error": None,
    "direct_answer": "Mining NET RESULT for January 2025 was MYR 183,975. Utility costs were the primary expense.",
    "key_findings": [
        "Utility - Nexbase accounted for 84% of total costs.",
        "Revenue:Realised was MYR 280,000.",
    ],
    "proactive_flags": [],
    "detail_table": {
        "headers": ["Account", "Amount (MYR)", "Type", "% of Total"],
        "rows": [
            ["Revenue:Realised", "280,000", "actual", "62.2%"],
            ["Revenue:Un-Realised", "170,000", "(accrued)", "37.8%"],
            ["", "", "", ""],
            ["Utility - Nexbase", "245,048", "(accrued)", "82.9%"],
            ["Rent or lease", "40,000", "actual", "13.5%"],
            ["", "", "", ""],
            ["NET RESULT", "164,952", "", ""],
        ]
    },
    "business_lines": {
        "mining": {"revenue": 450000, "costs": 285048, "net": 164952},
        "others": {"revenue": 0, "costs": 0, "net": 0},
        "total": {"revenue": 450000, "costs": 285048, "net": 164952},
    },
    "currency": "MYR",
})

# ---------------------------------------------------------------------------
# Test 2: pnl_by_line — prose is correct (should be CLEAN)
# ---------------------------------------------------------------------------

run_test("pnl_by_line: prose is correct (expect CLEAN)", {
    "report_type": "pnl_by_line",
    "has_detail_table": True,
    "error": None,
    "direct_answer": "Mining NET RESULT for January 2025 was MYR 164,952. Utility costs were the primary expense.",
    "key_findings": [
        "Utility - Nexbase accounted for 82.9% of total costs.",
        "Revenue:Realised was MYR 280,000.",
    ],
    "proactive_flags": [],
    "detail_table": {
        "headers": ["Account", "Amount (MYR)", "Type", "% of Total"],
        "rows": [
            ["Revenue:Realised", "280,000", "actual", "62.2%"],
            ["Revenue:Un-Realised", "170,000", "(accrued)", "37.8%"],
            ["", "", "", ""],
            ["Utility - Nexbase", "245,048", "(accrued)", "82.9%"],
            ["Rent or lease", "40,000", "actual", "13.5%"],
            ["", "", "", ""],
            ["NET RESULT", "164,952", "", ""],
        ]
    },
    "business_lines": {
        "mining": {"revenue": 450000, "costs": 285048, "net": 164952},
        "others": {"revenue": 0, "costs": 0, "net": 0},
        "total": {"revenue": 450000, "costs": 285048, "net": 164952},
    },
    "currency": "MYR",
})

# ---------------------------------------------------------------------------
# Test 3: pnl_by_line — negative net but prose says "profit" (sign error)
# ---------------------------------------------------------------------------

run_test("pnl_by_line: net negative but prose says profit (expect FIX)", {
    "report_type": "pnl_by_line",
    "has_detail_table": True,
    "error": None,
    "direct_answer": "Mining generated a profit of MYR -45,231 in March 2025.",
    "key_findings": ["Utility costs exceeded revenue."],
    "proactive_flags": [],
    "detail_table": {
        "headers": ["Account", "Amount (MYR)", "Type", "% of Total"],
        "rows": [
            ["Revenue:Realised", "200,000", "actual", "100%"],
            ["", "", "", ""],
            ["Utility - Nexbase", "245,231", "(accrued)", "100%"],
            ["", "", "", ""],
            ["NET RESULT", "-45,231", "", ""],
        ]
    },
    "business_lines": {
        "mining": {"revenue": 200000, "costs": 245231, "net": -45231},
        "others": {"revenue": 0, "costs": 0, "net": 0},
        "total": {"revenue": 200000, "costs": 245231, "net": -45231},
    },
    "currency": "MYR",
})

# ---------------------------------------------------------------------------
# Test 4: summary_grid — total.net ≠ mining.net + others.net
# ---------------------------------------------------------------------------

run_test("summary_grid: total.net is wrong (expect FIX)", {
    "report_type": "summary_grid",
    "has_detail_table": True,
    "error": None,
    "direct_answer": "Total net for Q1 2025 was MYR 200,000 across mining and others.",
    "key_findings": ["Mining contributed the majority of net income."],
    "proactive_flags": [],
    "detail_table": {
        "headers": ["Segment", "Revenue", "Costs", "Net"],
        "rows": [
            ["Mining", "450,000", "285,048", "164,952"],
            ["Others", "50,000", "69,000", "-19,000"],
            ["TOTAL", "500,000", "354,048", "200,000"],
        ]
    },
    "business_lines": {
        "mining": {"revenue": 450000, "costs": 285048, "net": 164952},
        "others": {"revenue": 50000, "costs": 69000, "net": -19000},
        "total": {"revenue": 500000, "costs": 354048, "net": 200000},
    },
    "currency": "MYR",
})

# ---------------------------------------------------------------------------
# Test 5: error state — should be skipped (no audit)
# ---------------------------------------------------------------------------

run_test("error state: should skip audit", {
    "report_type": "standard",
    "has_detail_table": False,
    "error": "Failed to fetch data from QuickBooks.",
    "direct_answer": "Could not retrieve data.",
    "key_findings": [],
    "proactive_flags": [],
    "detail_table": None,
    "business_lines": None,
    "currency": "MYR",
})

# ---------------------------------------------------------------------------
# Test 6: Others P&L where direct_answer says "mining" — reproduces original bug
# ---------------------------------------------------------------------------

run_test("Others P&L: prose says 'mining' but scope is others (expect FIX)", {
    "report_type": "pnl_by_line",
    "has_detail_table": True,
    "error": None,
    "direct_answer": "Mining NET RESULT for Q1 2025 was a loss of MYR -27,565. Operational costs exceeded revenue.",
    "key_findings": [
        "Mining generated a net loss of MYR -27,565.",
        "Management fees were the primary cost at MYR 45,000.",
    ],
    "proactive_flags": [],
    "detail_table": {
        "headers": ["Account", "Amount (MYR)", "Type", "% of Total"],
        "rows": [
            ["Revenue:Service Income", "120,000", "actual", "100%"],
            ["", "", "", ""],
            ["Management Fees", "45,000", "actual", "60.5%"],
            ["Admin Expenses", "29,400", "actual", "39.5%"],
            ["", "", "", ""],
            ["NET RESULT", "-27,565", "", ""],
        ]
    },
    "business_lines": {
        "mining": {"revenue": 0, "costs": 0, "net": 0},
        "others": {"revenue": 120000, "costs": 74400, "net": -27565},
        "total": {"revenue": 120000, "costs": 74400, "net": -27565},
    },
    "currency": "MYR",
},
)

print(f"\n{'='*60}")
print("All tests complete.")
