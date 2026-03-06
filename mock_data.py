"""
Mock QuickBooks data for development.
Realistic Bitcoin mining company financials for The Hashing Company.
Used when MOCK_MODE=true (Sprint 1 development).
"""

from datetime import datetime


def get_quarterly_summary(quarter: str, year: int) -> dict:
    """
    Returns mock P&L data for a given quarter.
    Simulates ~200 ASIC operation with 2 sites.
    """
    data = {
        "Q1_2025": {
            "period": "Q1 2025",
            "start_date": "2025-01-01",
            "end_date": "2025-03-31",
            "revenue": {
                "total": 712_400,
                "mining_revenue": 685_200,
                "hosting_revenue": 27_200,
            },
            "cogs": {
                "total": 534_800,
                "electricity": 412_600,
                "facility_lease": 72_000,
                "equipment_depreciation": 38_200,
                "pool_fees": 12_000,
            },
            "gross_profit": 177_600,
            "gross_margin_pct": 24.9,
            "operating_expenses": {
                "total": 89_400,
                "salaries": 54_000,
                "insurance": 12_600,
                "maintenance": 14_800,
                "software_subscriptions": 4_200,
                "travel": 3_800,
            },
            "net_income": 88_200,
            "net_margin_pct": 12.4,
        },
        "Q2_2025": {
            "period": "Q2 2025",
            "start_date": "2025-04-01",
            "end_date": "2025-06-30",
            "revenue": {
                "total": 768_900,
                "mining_revenue": 738_100,
                "hosting_revenue": 30_800,
            },
            "cogs": {
                "total": 548_200,
                "electricity": 418_900,
                "facility_lease": 72_000,
                "equipment_depreciation": 38_200,
                "pool_fees": 19_100,
            },
            "gross_profit": 220_700,
            "gross_margin_pct": 28.7,
            "operating_expenses": {
                "total": 91_200,
                "salaries": 54_000,
                "insurance": 12_600,
                "maintenance": 16_200,
                "software_subscriptions": 4_200,
                "travel": 4_200,
            },
            "net_income": 129_500,
            "net_margin_pct": 16.8,
        },
        "Q3_2025": {
            "period": "Q3 2025",
            "start_date": "2025-07-01",
            "end_date": "2025-09-30",
            "revenue": {
                "total": 801_300,
                "mining_revenue": 769_500,
                "hosting_revenue": 31_800,
            },
            "cogs": {
                "total": 562_100,
                "electricity": 425_400,
                "facility_lease": 72_000,
                "equipment_depreciation": 38_200,
                "pool_fees": 26_500,
            },
            "gross_profit": 239_200,
            "gross_margin_pct": 29.9,
            "operating_expenses": {
                "total": 93_800,
                "salaries": 56_000,
                "insurance": 12_600,
                "maintenance": 17_400,
                "software_subscriptions": 4_200,
                "travel": 3_600,
            },
            "net_income": 145_400,
            "net_margin_pct": 18.1,
        },
        "Q4_2025": {
            "period": "Q4 2025",
            "start_date": "2025-10-01",
            "end_date": "2025-12-31",
            "revenue": {
                "total": 847_200,
                "mining_revenue": 812_800,
                "hosting_revenue": 34_400,
            },
            "cogs": {
                "total": 612_400,
                "electricity": 468_200,
                "facility_lease": 72_000,
                "equipment_depreciation": 38_200,
                "pool_fees": 34_000,
            },
            "gross_profit": 234_800,
            "gross_margin_pct": 27.7,
            "operating_expenses": {
                "total": 98_600,
                "salaries": 58_000,
                "insurance": 12_600,
                "maintenance": 18_600,
                "software_subscriptions": 4_800,
                "travel": 4_600,
            },
            "net_income": 136_200,
            "net_margin_pct": 16.1,
        },
        "Q1_2026": {
            "period": "Q1 2026",
            "start_date": "2026-01-01",
            "end_date": "2026-03-31",
            "revenue": {
                "total": 892_100,
                "mining_revenue": 854_600,
                "hosting_revenue": 37_500,
            },
            "cogs": {
                "total": 628_700,
                "electricity": 478_500,
                "facility_lease": 75_000,
                "equipment_depreciation": 40_200,
                "pool_fees": 35_000,
            },
            "gross_profit": 263_400,
            "gross_margin_pct": 29.5,
            "operating_expenses": {
                "total": 101_200,
                "salaries": 60_000,
                "insurance": 13_200,
                "maintenance": 18_200,
                "software_subscriptions": 5_200,
                "travel": 4_600,
            },
            "net_income": 162_200,
            "net_margin_pct": 18.2,
        },
    }

    key = f"Q{quarter}_{year}" if isinstance(quarter, int) else f"{quarter}_{year}"
    return data.get(key, None)


def get_balance_sheet(as_of_date: str = None) -> dict:
    """Returns mock balance sheet data."""
    return {
        "as_of_date": as_of_date or datetime.now().strftime("%Y-%m-%d"),
        "assets": {
            "total": 2_140_000,
            "current_assets": {
                "total": 890_000,
                "cash_and_bank": 340_000,
                "accounts_receivable": 210_000,
                "btc_holdings": 285_000,
                "prepaid_expenses": 55_000,
            },
            "fixed_assets": {
                "total": 1_250_000,
                "mining_equipment": 1_680_000,
                "accumulated_depreciation": -520_000,
                "leasehold_improvements": 62_000,
                "office_equipment": 28_000,
            },
        },
        "liabilities": {
            "total": 780_000,
            "current_liabilities": {
                "total": 180_000,
                "accounts_payable": 95_000,
                "accrued_expenses": 42_000,
                "current_portion_debt": 43_000,
            },
            "long_term_liabilities": {
                "total": 600_000,
                "equipment_financing": 480_000,
                "facility_deposit": 120_000,
            },
        },
        "equity": {
            "total": 1_360_000,
            "owner_equity": 1_000_000,
            "retained_earnings": 360_000,
        },
        "ratios": {
            "current_ratio": 4.94,
            "debt_to_equity": 0.57,
            "quick_ratio": 4.64,
        },
    }


def get_pnl(start_date: str, end_date: str) -> dict:
    """Returns mock P&L for a custom date range."""
    return {
        "period": f"{start_date} to {end_date}",
        "start_date": start_date,
        "end_date": end_date,
        "revenue": {
            "total": 287_400,
            "mining_revenue": 275_800,
            "hosting_revenue": 11_600,
        },
        "cogs": {
            "total": 205_300,
            "electricity": 156_200,
            "facility_lease": 25_000,
            "equipment_depreciation": 13_400,
            "pool_fees": 10_700,
        },
        "gross_profit": 82_100,
        "gross_margin_pct": 28.6,
        "operating_expenses": {
            "total": 33_700,
            "salaries": 20_000,
            "insurance": 4_400,
            "maintenance": 6_100,
            "software_subscriptions": 1_700,
            "travel": 1_500,
        },
        "net_income": 48_400,
        "net_margin_pct": 16.8,
    }


def get_cash_position() -> dict:
    """Returns mock cash and AR/AP snapshot."""
    return {
        "as_of_date": datetime.now().strftime("%Y-%m-%d"),
        "cash_balances": {
            "total": 340_000,
            "operating_account": 245_000,
            "payroll_account": 62_000,
            "savings_reserve": 33_000,
        },
        "btc_holdings": {
            "btc_amount": 3.42,
            "usd_value": 285_000,
        },
        "accounts_receivable": {
            "total": 210_000,
            "current": 142_000,
            "30_days": 48_000,
            "60_days": 15_000,
            "90_plus_days": 5_000,
            "top_outstanding": [
                {"customer": "CloudHash Inc", "amount": 62_000, "days": 12},
                {"customer": "MinePool Co", "amount": 48_000, "days": 34},
                {"customer": "BitFacility Ltd", "amount": 38_000, "days": 8},
            ],
        },
        "accounts_payable": {
            "total": 95_000,
            "current": 58_000,
            "30_days": 27_000,
            "60_days": 10_000,
            "90_plus_days": 0,
            "top_upcoming": [
                {"vendor": "PowerGrid Energy", "amount": 38_000, "due_in_days": 5},
                {"vendor": "CoolSys HVAC", "amount": 22_000, "due_in_days": 12},
                {"vendor": "NetSecure IT", "amount": 14_000, "due_in_days": 18},
            ],
        },
    }
