"""
Slack Block Kit formatter helpers.
Converts financial data into clean, readable Slack messages.
"""


def fmt_currency(amount: int | float) -> str:
    """Format number as currency: $1,234,567"""
    if amount < 0:
        return f"-${abs(amount):,.0f}"
    return f"${amount:,.0f}"


def fmt_pct(value: float) -> str:
    """Format as percentage: 27.7%"""
    return f"{value:.1f}%"


def fmt_delta(current: float, previous: float) -> str:
    """Format a comparison delta with arrow."""
    if previous == 0:
        return "N/A"
    change_pct = ((current - previous) / abs(previous)) * 100
    arrow = "📈" if change_pct > 0 else "📉" if change_pct < 0 else "➡️"
    sign = "+" if change_pct > 0 else ""
    return f"{arrow} {sign}{change_pct:.1f}%"


def section(text: str) -> dict:
    """Create a simple section block."""
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def divider() -> dict:
    """Create a divider block."""
    return {"type": "divider"}


def header(text: str) -> dict:
    """Create a header block."""
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def fields_section(field_pairs: list[tuple[str, str]]) -> dict:
    """Create a section with field columns. field_pairs = [(label, value), ...]"""
    fields = []
    for label, value in field_pairs:
        fields.append({"type": "mrkdwn", "text": f"*{label}*\n{value}"})
    return {"type": "section", "fields": fields}


def context(text: str) -> dict:
    """Create a context block (small text)."""
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


# ─── Report Formatters ───────────────────────────────────────────────


def format_quarterly_summary(data: dict, comparison_data: dict = None) -> list[dict]:
    """Format quarterly summary as Slack Block Kit blocks."""
    blocks = []

    period = data["period"]
    blocks.append(header(f"📊 {period} Quarterly Summary"))
    blocks.append(divider())

    # Revenue section
    rev = data["revenue"]
    cogs = data["cogs"]
    revenue_line = fmt_currency(rev["total"])
    if comparison_data:
        prev_rev = comparison_data["revenue"]["total"]
        revenue_line += f"  {fmt_delta(rev['total'], prev_rev)}"

    blocks.append(fields_section([
        ("Revenue", revenue_line),
        ("COGS", fmt_currency(cogs["total"])),
        ("Gross Profit", f"{fmt_currency(data['gross_profit'])}  ({fmt_pct(data['gross_margin_pct'])} margin)"),
        ("Net Income", f"{fmt_currency(data['net_income'])}  ({fmt_pct(data['net_margin_pct'])} margin)"),
    ]))

    blocks.append(divider())

    # Revenue breakdown
    blocks.append(section(
        f"*Revenue Breakdown*\n"
        f"  Mining Revenue: {fmt_currency(rev['mining_revenue'])}\n"
        f"  Hosting Revenue: {fmt_currency(rev.get('hosting_revenue', 0))}"
    ))

    # Top COGS
    blocks.append(section(
        f"*Cost Breakdown (Top Items)*\n"
        f"  Electricity: {fmt_currency(cogs['electricity'])}\n"
        f"  Facility Lease: {fmt_currency(cogs['facility_lease'])}\n"
        f"  Equipment Depreciation: {fmt_currency(cogs['equipment_depreciation'])}\n"
        f"  Pool Fees: {fmt_currency(cogs['pool_fees'])}"
    ))

    # OpEx
    opex = data["operating_expenses"]
    blocks.append(section(
        f"*Operating Expenses: {fmt_currency(opex['total'])}*\n"
        f"  Salaries: {fmt_currency(opex['salaries'])}\n"
        f"  Maintenance: {fmt_currency(opex['maintenance'])}\n"
        f"  Insurance: {fmt_currency(opex['insurance'])}\n"
        f"  Software: {fmt_currency(opex['software_subscriptions'])}\n"
        f"  Travel: {fmt_currency(opex['travel'])}"
    ))

    # QoQ comparison if available
    if comparison_data:
        prev = comparison_data
        blocks.append(divider())
        blocks.append(section(
            f"*vs {prev['period']}*\n"
            f"  Revenue: {fmt_delta(rev['total'], prev['revenue']['total'])}  "
            f"({fmt_currency(prev['revenue']['total'])} → {fmt_currency(rev['total'])})\n"
            f"  Gross Margin: {fmt_pct(prev['gross_margin_pct'])} → {fmt_pct(data['gross_margin_pct'])}\n"
            f"  Net Income: {fmt_delta(data['net_income'], prev['net_income'])}  "
            f"({fmt_currency(prev['net_income'])} → {fmt_currency(data['net_income'])})"
        ))

    # Mock mode indicator
    blocks.append(context("⚠️ _Mock data — QuickBooks not connected yet_"))

    return blocks


def format_balance_sheet(data: dict) -> list[dict]:
    """Format balance sheet as Slack Block Kit blocks."""
    blocks = []

    blocks.append(header(f"📋 Balance Sheet — {data['as_of_date']}"))
    blocks.append(divider())

    # Assets
    assets = data["assets"]
    current = assets["current_assets"]
    fixed = assets["fixed_assets"]

    blocks.append(section(
        f"*Assets: {fmt_currency(assets['total'])}*\n\n"
        f"  _Current Assets: {fmt_currency(current['total'])}_\n"
        f"    Cash & Bank: {fmt_currency(current['cash_and_bank'])}\n"
        f"    Accounts Receivable: {fmt_currency(current['accounts_receivable'])}\n"
        f"    BTC Holdings: {fmt_currency(current['btc_holdings'])}\n"
        f"    Prepaid Expenses: {fmt_currency(current['prepaid_expenses'])}\n\n"
        f"  _Fixed Assets: {fmt_currency(fixed['total'])}_\n"
        f"    Mining Equipment: {fmt_currency(fixed['mining_equipment'])}\n"
        f"    Accum. Depreciation: {fmt_currency(fixed['accumulated_depreciation'])}\n"
        f"    Leasehold Improvements: {fmt_currency(fixed['leasehold_improvements'])}"
    ))

    blocks.append(divider())

    # Liabilities
    liabs = data["liabilities"]
    current_l = liabs["current_liabilities"]
    longterm = liabs["long_term_liabilities"]

    blocks.append(section(
        f"*Liabilities: {fmt_currency(liabs['total'])}*\n\n"
        f"  _Current: {fmt_currency(current_l['total'])}_\n"
        f"    Accounts Payable: {fmt_currency(current_l['accounts_payable'])}\n"
        f"    Accrued Expenses: {fmt_currency(current_l['accrued_expenses'])}\n"
        f"    Current Debt: {fmt_currency(current_l['current_portion_debt'])}\n\n"
        f"  _Long-term: {fmt_currency(longterm['total'])}_\n"
        f"    Equipment Financing: {fmt_currency(longterm['equipment_financing'])}\n"
        f"    Facility Deposit: {fmt_currency(longterm['facility_deposit'])}"
    ))

    blocks.append(divider())

    # Equity
    equity = data["equity"]
    blocks.append(section(
        f"*Equity: {fmt_currency(equity['total'])}*\n"
        f"  Owner Equity: {fmt_currency(equity['owner_equity'])}\n"
        f"  Retained Earnings: {fmt_currency(equity['retained_earnings'])}"
    ))

    blocks.append(divider())

    # Ratios
    ratios = data["ratios"]
    blocks.append(fields_section([
        ("Current Ratio", f"{ratios['current_ratio']:.1f}x"),
        ("Debt/Equity", f"{ratios['debt_to_equity']:.2f}"),
    ]))

    blocks.append(context("⚠️ _Mock data — QuickBooks not connected yet_"))

    return blocks


def format_cash_position(data: dict) -> list[dict]:
    """Format cash position as Slack Block Kit blocks."""
    blocks = []

    blocks.append(header(f"💰 Cash Position — {data['as_of_date']}"))
    blocks.append(divider())

    cash = data["cash_balances"]
    btc = data["btc_holdings"]
    total_liquid = cash["total"] + btc["usd_value"]

    blocks.append(fields_section([
        ("Cash & Bank", fmt_currency(cash["total"])),
        ("BTC Holdings", f"{btc['btc_amount']} BTC ({fmt_currency(btc['usd_value'])})"),
        ("Total Liquid", fmt_currency(total_liquid)),
    ]))

    blocks.append(divider())

    # Bank accounts
    blocks.append(section(
        f"*Bank Accounts*\n"
        f"  Operating: {fmt_currency(cash['operating_account'])}\n"
        f"  Payroll: {fmt_currency(cash['payroll_account'])}\n"
        f"  Savings Reserve: {fmt_currency(cash['savings_reserve'])}"
    ))

    blocks.append(divider())

    # AR summary
    ar = data["accounts_receivable"]
    blocks.append(section(
        f"*Accounts Receivable: {fmt_currency(ar['total'])}*\n"
        f"  Current: {fmt_currency(ar['current'])}  |  "
        f"30d: {fmt_currency(ar['30_days'])}  |  "
        f"60d: {fmt_currency(ar['60_days'])}  |  "
        f"90d+: {fmt_currency(ar['90_plus_days'])}"
    ))

    # Top AR
    top_ar = ar["top_outstanding"]
    ar_lines = "\n".join(
        f"  • {item['customer']}: {fmt_currency(item['amount'])} ({item['days']} days)"
        for item in top_ar
    )
    blocks.append(section(f"*Top Outstanding Invoices*\n{ar_lines}"))

    blocks.append(divider())

    # AP summary
    ap = data["accounts_payable"]
    blocks.append(section(
        f"*Accounts Payable: {fmt_currency(ap['total'])}*\n"
        f"  Current: {fmt_currency(ap['current'])}  |  "
        f"30d: {fmt_currency(ap['30_days'])}  |  "
        f"60d: {fmt_currency(ap['60_days'])}"
    ))

    # Top AP
    top_ap = ap["top_upcoming"]
    ap_lines = "\n".join(
        f"  • {item['vendor']}: {fmt_currency(item['amount'])} (due in {item['due_in_days']} days)"
        for item in top_ap
    )
    blocks.append(section(f"*Upcoming Bills*\n{ap_lines}"))

    blocks.append(context("⚠️ _Mock data — QuickBooks not connected yet_"))

    return blocks


def format_pnl(data: dict) -> list[dict]:
    """Format P&L statement as Slack Block Kit blocks."""
    blocks = []

    blocks.append(header(f"📈 Profit & Loss — {data['period']}"))
    blocks.append(divider())

    rev = data["revenue"]
    cogs = data["cogs"]
    opex = data["operating_expenses"]

    blocks.append(fields_section([
        ("Revenue", fmt_currency(rev["total"])),
        ("COGS", fmt_currency(cogs["total"])),
        ("Gross Profit", f"{fmt_currency(data['gross_profit'])}  ({fmt_pct(data['gross_margin_pct'])})"),
        ("Net Income", f"{fmt_currency(data['net_income'])}  ({fmt_pct(data['net_margin_pct'])})"),
    ]))

    blocks.append(divider())

    blocks.append(section(
        f"*Operating Expenses: {fmt_currency(opex['total'])}*\n"
        f"  Salaries: {fmt_currency(opex['salaries'])}\n"
        f"  Maintenance: {fmt_currency(opex['maintenance'])}\n"
        f"  Insurance: {fmt_currency(opex['insurance'])}\n"
        f"  Software: {fmt_currency(opex['software_subscriptions'])}\n"
        f"  Travel: {fmt_currency(opex['travel'])}"
    ))

    blocks.append(context("⚠️ _Mock data — QuickBooks not connected yet_"))

    return blocks


def format_help(help_text: str) -> list[dict]:
    """Format help message."""
    return [
        header("👋 Nexbase Finance Agent"),
        divider(),
        section(help_text),
        context("_Powered by QuickBooks + Claude_"),
    ]


def format_error(message: str) -> list[dict]:
    """Format error/unknown message."""
    return [
        section(f"🤔 {message}"),
        context("_Type \"help\" to see what I can do_"),
    ]
