"""
Slack Block Kit formatter helpers.
Converts financial data into clean, readable Slack messages.
"""

from table_utils import parse_amount


def fmt_currency(amount: int | float) -> str:
    if amount < 0:
        return f"-${abs(amount):,.0f}"
    return f"${amount:,.0f}"


def fmt_pct(value: float) -> str:
    return f"{value:.1f}%"


def fmt_delta(current: float, previous: float) -> str:
    if previous == 0:
        return "N/A"
    change_pct = ((current - previous) / abs(previous)) * 100
    arrow = "📈" if change_pct > 0 else "📉" if change_pct < 0 else "➡️"
    sign = "+" if change_pct > 0 else ""
    return f"{arrow} {sign}{change_pct:.1f}%"


def section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def divider() -> dict:
    return {"type": "divider"}


def header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def fields_section(field_pairs: list[tuple[str, str]]) -> dict:
    fields = []
    for label, value in field_pairs:
        fields.append({"type": "mrkdwn", "text": f"*{label}*\n{value}"})
    return {"type": "section", "fields": fields}


def context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


# ─── Shared Formatter Helpers ────────────────────────────────────────

def _footer_block(data_completeness: str, data_note: str) -> dict:
    """Build the standard footer context block used by all report formatters."""
    parts = []
    if data_completeness == "partial":
        parts.append("🟡 Partial data")
    elif data_completeness == "incomplete":
        parts.append("🔴 Incomplete data — treat with caution")
    if data_note:
        parts.append(f"ℹ️ {data_note}")
    parts.append("_QuickBooks Online · The Hashing Company_")
    return context("  ·  ".join(parts))


def _findings_flag_blocks(key_findings: list, proactive_flags: list) -> list[dict]:
    """Build divider+section blocks for key findings and proactive flags."""
    blocks = []
    if key_findings:
        blocks.append(divider())
        blocks.append(section("\n".join(f"• {f}" for f in key_findings)))
    if proactive_flags:
        blocks.append(divider())
        blocks.append(section("\n".join(f"⚠️ {f}" for f in proactive_flags)))
    return blocks


# ─── Dynamic Analysis Formatter ──────────────────────────────────────

COMPLETENESS_EMOJI = {
    "complete": "🟢",
    "partial": "🟡",
    "incomplete": "🔴",
}


def format_dynamic_analysis(analysis: dict) -> list[dict]:
    """
    Formats qb_analyst output into tight CFO-readable Slack blocks.
    Routes to specialist renderers for pnl_by_line and summary_grid.
    Caps at 49 blocks — Slack silently drops messages exceeding 50 blocks.
    """
    report_type = analysis.get("report_type", "standard")

    if report_type == "pnl_by_line":
        blocks = _format_pnl_by_line(analysis)
    elif report_type == "pnl_monthly":
        blocks = _format_pnl_monthly(analysis)
    elif report_type == "summary_grid":
        blocks = _format_summary_grid(analysis)
    else:
        blocks = _format_standard(analysis)

    return blocks[:49]


def _format_standard(analysis: dict) -> list[dict]:
    """
    Standard layout:
      1. Header + completeness badge
      2. Direct answer
      3. Detail table
      4. Key findings
      5. Flags
      6. Footer
    """
    blocks = []
    question = analysis.get("question", "")
    direct_answer = analysis.get("direct_answer", "")
    key_findings = analysis.get("key_findings", [])
    proactive_flags = analysis.get("proactive_flags", [])
    has_detail_table = analysis.get("has_detail_table", False)
    detail_table = analysis.get("detail_table")
    data_note = analysis.get("data_note", "")
    data_completeness = analysis.get("data_completeness", "")
    error = analysis.get("error")

    if error and not direct_answer:
        return format_error(f"Couldn't complete that query: {error}")

    q_display = question[:70] + ("..." if len(question) > 70 else "")
    badge = COMPLETENESS_EMOJI.get(data_completeness, "")
    blocks.append(header(f"🔍 {q_display}" + (f"  {badge}" if badge else "")))
    blocks.append(divider())
    blocks.append(section(direct_answer))

    if has_detail_table and detail_table:
        hdrs = detail_table.get("headers", [])
        rows = detail_table.get("rows", [])
        if hdrs and rows:
            blocks.append(divider())
            blocks.extend(_render_table(hdrs, rows))

    blocks.extend(_findings_flag_blocks(key_findings, proactive_flags))
    blocks.append(_footer_block(data_completeness, data_note))
    return blocks


def _format_pnl_by_line(analysis: dict) -> list[dict]:
    """
    P&L by business line layout.
    Filters to only the requested line(s) based on the question.
    Shows all lines only when question contains "all" or no specific line is mentioned.
    """
    blocks = []
    question = (analysis.get("question") or "").lower()
    direct_answer = analysis.get("direct_answer", "")
    key_findings = analysis.get("key_findings", [])
    proactive_flags = analysis.get("proactive_flags", [])
    detail_table = analysis.get("detail_table")
    data_note = analysis.get("data_note", "")
    data_completeness = analysis.get("data_completeness", "")
    business_lines = analysis.get("business_lines")

    # Determine which lines to show.
    # show_all when no specific line is mentioned, OR "all business lines"/"all lines" is explicit.
    # Using "all" alone is NOT sufficient — "show hosting P&L for all of last month" should not
    # trigger show_all and display mining/others.
    has_specific_line = any(x in question for x in ["mining", "others"])
    show_all = not has_specific_line or "all business lines" in question or "all lines" in question
    show_mining = show_all or "mining" in question
    show_others = show_all or "others" in question

    _q = analysis.get("question", "")
    q_display = _q[:70] + ("..." if len(_q) > 70 else "")
    badge = COMPLETENESS_EMOJI.get(data_completeness, "")
    blocks.append(header(f"📊 {q_display}" + (f"  {badge}" if badge else "")))
    blocks.append(divider())
    blocks.append(section(direct_answer))

    currency = analysis.get("currency", "MYR")

    if business_lines:
        line_configs = [
            ("mining",  "⛏️ MINING",  show_mining),
            ("others",  "📦 OTHERS",  show_others),
        ]
        for key, label, should_show in line_configs:
            if not should_show:
                continue
            line = business_lines.get(key)
            if not line:
                continue
            rev = line.get("revenue", 0)
            costs = line.get("costs", 0)
            net = line.get("net", 0)
            if rev == 0 and costs == 0:
                continue
            net_sign = "+" if net >= 0 else ""
            blocks.append(divider())
            blocks.append(section(
                f"*{label}*\n"
                f"Revenue: `{currency} {rev:,.0f}`   Costs: `{currency} {costs:,.0f}`   "
                f"Net: `{net_sign}{currency} {net:,.0f}`"
            ))

        # Only show combined total when showing all lines
        if show_all:
            total = business_lines.get("total")
            if total:
                blocks.append(divider())
                t_rev = total.get("revenue", 0)
                t_costs = total.get("costs", 0)
                t_net = total.get("net", 0)
                t_sign = "+" if t_net >= 0 else ""
                blocks.append(section(
                    f"*━━━ COMBINED TOTAL ━━━*\n"
                    f"Revenue: `{currency} {t_rev:,.0f}`   Costs: `{currency} {t_costs:,.0f}`   "
                    f"Net: `{t_sign}{currency} {t_net:,.0f}`"
                ))

    # Detail table — filter rows to only show requested line
    if detail_table:
        hdrs = detail_table.get("headers", [])
        rows = detail_table.get("rows", [])
        if hdrs and rows:
            # Row filtering rules:
            # - Monthly tables: never filter — rows are month names, analyst already scoped them.
            # - Combined multi-line tables (contain "mining net"/"hosting net" rows): filter to
            #   keep only the requested section(s).
            # - Single-line tables: never filter — all rows belong to the one requested line,
            #   account names like "Revenue:Realised" don't contain the line keyword.
            is_monthly_table = hdrs and str(hdrs[0]).lower() == "month"
            row_labels = {str(r[0]).lower() for r in rows}
            is_combined_table = bool(row_labels & {"mining net", "hosting net", "others net"})

            if not show_all and not is_monthly_table and is_combined_table:
                requested = []
                if show_mining:
                    requested.append("mining")
                if show_others:
                    requested.append("others")
                filtered_rows = [
                    r for r in rows
                    if any(req in str(r[0]).lower() for req in requested)
                    or str(r[0]).lower() in ("net result", "total", "combined total", "combined net")
                ]
                rows = filtered_rows if filtered_rows else rows

            blocks.append(divider())
            blocks.extend(_render_table(hdrs, rows))

    # MTM adjustment section (single-period)
    if show_mining and business_lines:
        mining = business_lines.get("mining", {})
        blocks.extend(_render_mtm_section(mining, currency))

    if key_findings:
        blocks.append(divider())
        # Filter findings to relevant line only
        if not show_all:
            relevant = [f for f in key_findings if
                        (show_mining and "mining" in f.lower()) or
                        (show_others and "others" in f.lower()) or
                        not any(x in f.lower() for x in ["mining", "others"])]
            key_findings = relevant if relevant else key_findings
        blocks.append(section("\n".join(f"• {f}" for f in key_findings)))

    blocks.extend(_findings_flag_blocks([], proactive_flags))
    blocks.append(_footer_block(data_completeness, data_note))
    return blocks


def _format_pnl_monthly(analysis: dict) -> list[dict]:
    """
    Monthly P&L comparison layout.
    Renders a month-by-month table (Month | Revenue | Costs | Net | Margin)
    followed by totals, key findings, and flags.
    """
    blocks = []
    question = analysis.get("question", "")
    direct_answer = analysis.get("direct_answer", "")
    key_findings = analysis.get("key_findings", [])
    proactive_flags = analysis.get("proactive_flags", [])
    detail_table = analysis.get("detail_table")
    data_note = analysis.get("data_note", "")
    data_completeness = analysis.get("data_completeness", "")
    business_lines = analysis.get("business_lines")
    currency = analysis.get("currency", "MYR")

    q_display = question[:70] + ("..." if len(question) > 70 else "")
    badge = COMPLETENESS_EMOJI.get(data_completeness, "")
    blocks.append(header(f"📅 {q_display}" + (f"  {badge}" if badge else "")))
    blocks.append(divider())
    blocks.append(section(direct_answer))

    if detail_table:
        hdrs = detail_table.get("headers", [])
        rows = detail_table.get("rows", [])
        if hdrs and rows:
            mining = (business_lines or {}).get("mining", {})
            mtm_extra = _build_mtm_inline_rows(hdrs, rows, mining)
            blocks.append(divider())
            blocks.extend(_render_table(hdrs, rows + mtm_extra))

    if business_lines:
        total = business_lines.get("total")
        if total:
            t_rev = total.get("revenue", 0)
            t_costs = total.get("costs", 0)
            t_net = total.get("net", 0)
            t_sign = "+" if t_net >= 0 else ""
            blocks.append(divider())
            if t_costs != 0:
                blocks.append(section(
                    f"*━━━ PERIOD TOTAL ━━━*\n"
                    f"Revenue: `{currency} {t_rev:,.0f}`   Costs: `{currency} {t_costs:,.0f}`   "
                    f"Net: `{t_sign}{currency} {t_net:,.0f}`"
                ))
            else:
                blocks.append(section(
                    f"*━━━ PERIOD TOTAL ━━━*\n"
                    f"Revenue: `{currency} {t_rev:,.0f}`"
                ))

    blocks.extend(_findings_flag_blocks(key_findings, proactive_flags))
    blocks.append(_footer_block(data_completeness, data_note))
    return blocks


def _format_summary_grid(analysis: dict) -> list[dict]:
    """
    Summary grid layout: Mining / Others / Total in a table.
    """
    blocks = []
    question = analysis.get("question", "")
    direct_answer = analysis.get("direct_answer", "")
    key_findings = analysis.get("key_findings", [])
    proactive_flags = analysis.get("proactive_flags", [])
    data_note = analysis.get("data_note", "")
    data_completeness = analysis.get("data_completeness", "")
    business_lines = analysis.get("business_lines")

    q_display = question[:70] + ("..." if len(question) > 70 else "")
    badge = COMPLETENESS_EMOJI.get(data_completeness, "")
    blocks.append(header(f"📊 {q_display}" + (f"  {badge}" if badge else "")))
    blocks.append(divider())
    blocks.append(section(direct_answer))

    currency = analysis.get("currency", "MYR")

    if business_lines:
        blocks.append(divider())
        headers_row = ["", "Mining", "Others", "Total"]
        rows = []
        for metric, label in [("revenue", "Revenue"), ("costs", "Costs"), ("net", "Net")]:
            row = [label]
            for key in ["mining", "others", "total"]:
                val = business_lines.get(key, {}).get(metric, 0)
                row.append(f"{currency} {val:,.0f}" if val != 0 else "—")
            rows.append(row)
        blocks.extend(_render_table(headers_row, rows))

    blocks.extend(_findings_flag_blocks(key_findings, proactive_flags))
    blocks.append(_footer_block(data_completeness, data_note))
    return blocks


def _build_mtm_inline_rows(hdrs: list, rows: list, mining: dict) -> list:
    """
    Returns extra rows to append to the monthly P&L table when MTM mode is active.
    Appends after the TOTAL row:
      (blank)
      ADJUSTMENT
      <month>  <fair_adj>  -  -  -  -
      (blank)
      NET TOTAL  <rev+adj>  <costs...>  <net_adj>
    Returns [] if no adjustment data.
    """
    fair_adj = mining.get("fair_adjustment") or 0
    net_adj = mining.get("net_adjustment") or 0
    fair_adj_rows = mining.get("fair_adjustment_rows") or []
    if not fair_adj or not fair_adj_rows:
        return []

    col_count = len(hdrs)
    rev_col = 1          # Revenue is always col 1 in monthly table
    net_col = col_count - 1  # Net is always last col

    # Find TOTAL row for base values
    total_row = next((r for r in rows if r and str(r[0]).strip().upper() == "TOTAL"), None)

    extra = []
    extra.append([""] * col_count)                              # blank separator
    extra.append(["ADJUSTMENT"] + [""] * (col_count - 1))       # section label

    for r in fair_adj_rows:
        if str(r[0]).strip().upper() == "TOTAL":
            continue
        month_fair = r[1]
        row = [r[0]] + ["-"] * (col_count - 1)
        row[rev_col] = f"{int(round(month_fair)):,}" if month_fair != 0 else "-"
        extra.append(row)

    extra.append([""] * col_count)                              # blank separator

    # NET TOTAL row
    net_total = ["-"] * col_count
    net_total[0] = "NET TOTAL"
    if total_row:
        for i in range(1, col_count):
            base = parse_amount(total_row[i]) if i < len(total_row) else 0
            if i == rev_col:
                net_total[i] = f"{int(round(base + fair_adj)):,}"
            elif i == net_col:
                net_total[i] = f"{int(round(net_adj)):,}"
            else:
                net_total[i] = f"{int(round(base)):,}" if base != 0 else "0"
    else:
        net_total[rev_col] = f"{int(round(fair_adj)):,}"
        net_total[net_col] = f"{int(round(net_adj)):,}"
    extra.append(net_total)
    return extra


def _render_mtm_section(mining: dict, currency: str) -> list[dict]:
    """
    Renders Fair Adjustment + NET ADJUSTMENT as a separate preformatted block.
    For monthly queries: shows a per-month breakdown table (only months with non-zero adjustment).
    For single-period queries: shows a simple 2-row summary.
    Returns [] if no adjustment data is present.
    """
    fair_adj = mining.get("fair_adjustment", 0)
    net_adj = mining.get("net_adjustment", 0)
    if not fair_adj and not net_adj:
        return []

    blocks = [divider()]
    fair_adj_rows = mining.get("fair_adjustment_rows") or []

    if fair_adj_rows:
        # Monthly — show per-month breakdown + total
        headers = ["Month", f"Fair Adjustment ({currency})", f"Net Adjustment ({currency})"]
        rows = [
            [r[0], f"{r[1]:+,.0f}" if r[1] != 0 else "0", f"{r[2]:+,.0f}" if r[2] != 0 else "0"]
            for r in fair_adj_rows
        ]
        blocks.extend(_render_table(headers, rows))
    else:
        # Single period — simple 2-row summary
        rows = [
            ["Fair Adjustment", f"{fair_adj:+,.0f}"],
            ["NET ADJUSTMENT",  f"{net_adj:+,.0f}"],
        ]
        blocks.extend(_render_table(["", f"Amount ({currency})"], rows))

    return blocks


def _render_table(headers: list, rows: list) -> list[dict]:
    """
    Renders a data table as a rich_text_preformatted block.
    Guarantees true monospace rendering across all Slack clients.
    First column left-aligned (labels), remaining columns right-justified (numbers).
    """
    col_count = len(headers)

    # Compute column widths
    col_widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < col_count:
                col_widths[i] = max(col_widths[i], len(str(cell)))

    def _fmt_row(cells: list) -> str:
        parts = []
        for i in range(col_count):
            val = str(cells[i]) if i < len(cells) else ""
            # First column left-justified, rest right-justified
            parts.append(val.ljust(col_widths[i]) if i == 0 else val.rjust(col_widths[i]))
        return "  ".join(parts)

    header_line = _fmt_row(headers)
    separator = "  ".join("-" * col_widths[i] for i in range(col_count))

    lines = [header_line, separator] + [_fmt_row(row) for row in rows]
    text = "\n".join(lines)

    return [{
        "type": "rich_text",
        "elements": [{
            "type": "rich_text_preformatted",
            "elements": [{"type": "text", "text": text}]
        }]
    }]



def format_help(help_text: str) -> list[dict]:
    return [
        header("👋 Nexbase Finance Agent"),
        divider(),
        section(help_text),
        context("_Powered by QuickBooks + Claude · The Hashing Company_"),
    ]


def format_error(message: str) -> list[dict]:
    return [
        section(f"🤔 {message}"),
        context("_Type \"help\" to see what I can do_"),
    ]