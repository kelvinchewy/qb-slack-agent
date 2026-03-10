"""
Slack Block Kit formatter helpers.
Converts financial data into clean, readable Slack messages.
"""


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


# ─── Dynamic Analysis Formatter ──────────────────────────────────────

COMPLETENESS_EMOJI = {
    "complete": "🟢",
    "partial": "🟡",
    "incomplete": "🔴",
}


def format_dynamic_analysis(analysis: dict) -> list[dict]:
    """
    Formats qb_analyst output into tight CFO-readable Slack blocks.

    Layout order:
      1. Header (question + completeness badge)
      2. Direct answer — 2 sentences max
      3. Detail table — breakdown before prose for scannability
      4. Key findings — bullet points
      5. Proactive flags — warnings
      6. Footer — completeness + data note + source
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

    # Error state
    if error and not direct_answer:
        return format_error(f"Couldn't complete that query: {error}")

    # ── 1. Header ─────────────────────────────────────────────────────
    q_display = question[:70] + ("..." if len(question) > 70 else "")
    badge = COMPLETENESS_EMOJI.get(data_completeness, "")
    blocks.append(header(f"🔍 {q_display}" + (f"  {badge}" if badge else "")))
    blocks.append(divider())

    # ── 2. Direct answer (2 sentences max) ────────────────────────────
    blocks.append(section(direct_answer))

    # ── 3. Detail table ───────────────────────────────────────────────
    if has_detail_table and detail_table:
        hdrs = detail_table.get("headers", [])
        rows = detail_table.get("rows", [])
        if hdrs and rows:
            blocks.append(divider())
            blocks.extend(_render_table(hdrs, rows))

    # ── 4. Key findings ───────────────────────────────────────────────
    if key_findings:
        blocks.append(divider())
        blocks.append(section("\n".join(f"• {f}" for f in key_findings)))

    # ── 5. Flags ──────────────────────────────────────────────────────
    if proactive_flags:
        blocks.append(divider())
        blocks.append(section("\n".join(f"⚠️ {f}" for f in proactive_flags)))

    # ── 6. Footer ─────────────────────────────────────────────────────
    footer_parts = []
    if data_completeness == "partial":
        footer_parts.append("🟡 Partial data — some accounts may be missing")
    elif data_completeness == "incomplete":
        footer_parts.append("🔴 Incomplete data — treat with caution")
    if data_note:
        footer_parts.append(f"ℹ️ {data_note}")
    footer_parts.append("_QuickBooks Online · The Hashing Company_")
    blocks.append(context("  ·  ".join(footer_parts)))

    return blocks


def _render_table(headers: list, rows: list) -> list[dict]:
    """
    Renders a data table as Slack section blocks.
    Groups rows into chunks to stay within Slack's block limits.
    """
    blocks = []

    # Column widths for monospace-style alignment
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(str(cell)))

    # Header row
    header_line = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    separator = "  ".join("-" * col_widths[i] for i in range(len(headers)))

    # Render in chunks of 15 rows per block (Slack text limit)
    chunk_size = 15
    for chunk_start in range(0, len(rows), chunk_size):
        chunk = rows[chunk_start:chunk_start + chunk_size]
        lines = []

        if chunk_start == 0:
            lines.append(f"`{header_line}`")
            lines.append(f"`{separator}`")

        for row in chunk:
            row_line = "  ".join(str(row[i]).ljust(col_widths[i]) if i < len(row) else "" for i in range(len(headers)))
            lines.append(f"`{row_line}`")

        blocks.append(section("\n".join(lines)))

    return blocks



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
