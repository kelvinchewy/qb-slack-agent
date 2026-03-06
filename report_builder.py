"""
Report Builder — Takes orchestrator intent + params, fetches data, returns formatted Slack blocks.
In MOCK_MODE, uses mock_data. When QB is connected, will use qb_agent.
"""

import logging
from config import Config
import mock_data
import slack_formatter as fmt
from orchestrator import get_help_text

logger = logging.getLogger(__name__)


def build_report(intent_data: dict) -> list[dict]:
    """
    Main entry point. Takes classified intent from orchestrator,
    fetches the right data, and returns Slack Block Kit blocks.
    """
    intent = intent_data.get("intent", "unknown")
    logger.info(f"Building report for intent: {intent}")

    try:
        if intent == "quarterly_summary":
            return _build_quarterly(intent_data)
        elif intent == "balance_sheet":
            return _build_balance_sheet(intent_data)
        elif intent == "pnl":
            return _build_pnl(intent_data)
        elif intent == "cash_position":
            return _build_cash_position(intent_data)
        elif intent == "ar_aging":
            return _build_cash_position(intent_data)  # AR is part of cash position view
        elif intent == "ap_aging":
            return _build_cash_position(intent_data)  # AP is part of cash position view
        elif intent == "help":
            return fmt.format_help(get_help_text())
        else:
            message = intent_data.get("message", "I'm not sure what you're asking for.")
            return fmt.format_error(message)
    except Exception as e:
        logger.error(f"Report build error: {e}")
        return fmt.format_error("Something went wrong building that report. Please try again.")


def _build_quarterly(intent_data: dict) -> list[dict]:
    """Build quarterly summary report."""
    quarter = intent_data.get("quarter", 4)
    year = intent_data.get("year", 2025)

    if Config.MOCK_MODE:
        data = mock_data.get_quarterly_summary(quarter, year)
        if not data:
            return fmt.format_error(
                f"I don't have data for Q{quarter} {year}. "
                f"Try Q1-Q4 2025 or Q1 2026."
            )

        comparison_data = None
        if intent_data.get("comparison", False):
            comp_q = intent_data.get("comparison_quarter", quarter - 1 if quarter > 1 else 4)
            comp_y = intent_data.get("comparison_year", year if quarter > 1 else year - 1)
            comparison_data = mock_data.get_quarterly_summary(comp_q, comp_y)

        return fmt.format_quarterly_summary(data, comparison_data)
    else:
        # TODO: Sprint 2 — use qb_agent to fetch real data
        return fmt.format_error("QuickBooks integration not connected yet. Running in mock mode.")


def _build_balance_sheet(intent_data: dict) -> list[dict]:
    """Build balance sheet report."""
    as_of = intent_data.get("as_of_date", None)

    if Config.MOCK_MODE:
        data = mock_data.get_balance_sheet(as_of)
        return fmt.format_balance_sheet(data)
    else:
        return fmt.format_error("QuickBooks integration not connected yet.")


def _build_pnl(intent_data: dict) -> list[dict]:
    """Build P&L report."""
    start = intent_data.get("start_date", "2026-01-01")
    end = intent_data.get("end_date", "2026-01-31")

    if Config.MOCK_MODE:
        data = mock_data.get_pnl(start, end)
        return fmt.format_pnl(data)
    else:
        return fmt.format_error("QuickBooks integration not connected yet.")


def _build_cash_position(intent_data: dict) -> list[dict]:
    """Build cash position / AR / AP report."""
    if Config.MOCK_MODE:
        data = mock_data.get_cash_position()
        return fmt.format_cash_position(data)
    else:
        return fmt.format_error("QuickBooks integration not connected yet.")
