"""
Report Builder — Routes to fixed reports or dynamic query pipeline.

Fixed pipeline: intent → mock_data / qb_agent → slack_formatter
Dynamic pipeline: question → qb_interpreter → qb_analyst → slack_formatter
"""

import logging
from config import Config
import mock_data
import slack_formatter as fmt
from orchestrator import get_help_text

logger = logging.getLogger(__name__)


def _get_qb():
    import qb_agent
    return qb_agent


def build_report(intent_data: dict) -> list[dict]:
    intent = intent_data.get("intent", "unknown")
    route = intent_data.get("route", "fixed")
    logger.info(f"Building report | route={route} | intent={intent} | mock={Config.MOCK_MODE}")
    try:
        if route == "dynamic":
            return _build_dynamic(intent_data)
        if intent == "quarterly_summary":
            return _build_quarterly(intent_data)
        elif intent == "balance_sheet":
            return _build_balance_sheet(intent_data)
        elif intent == "pnl":
            return _build_pnl(intent_data)
        elif intent in ("cash_position", "ar_aging", "ap_aging"):
            return _build_cash_position(intent_data)
        elif intent == "help":
            return fmt.format_help(get_help_text())
        else:
            message = intent_data.get("message", "I'm not sure what you're asking for.")
            return fmt.format_error(message)
    except Exception as e:
        logger.error(f"Report build error: {e}")
        return fmt.format_error("Something went wrong building that report. Please try again.")


def _build_dynamic(intent_data: dict) -> list[dict]:
    from qb_interpreter import interpret_and_fetch
    from qb_analyst import analyse
    question = intent_data.get("original_question", "")
    if not question:
        return fmt.format_error("I lost track of your question. Please try again.")
    interpreter_result = interpret_and_fetch(question)
    analysis = analyse(interpreter_result)
    return fmt.format_dynamic_analysis(analysis)


def _build_quarterly(intent_data: dict) -> list[dict]:
    quarter = intent_data.get("quarter", 4)
    year = intent_data.get("year", 2025)
    if Config.MOCK_MODE:
        data = mock_data.get_quarterly_summary(quarter, year)
        if not data:
            return fmt.format_error(f"No data for Q{quarter} {year}. Try Q1-Q4 2025 or Q1 2026.")
        comparison_data = None
        if intent_data.get("comparison", False):
            comp_q = intent_data.get("comparison_quarter", quarter - 1 if quarter > 1 else 4)
            comp_y = intent_data.get("comparison_year", year if quarter > 1 else year - 1)
            comparison_data = mock_data.get_quarterly_summary(comp_q, comp_y)
        return fmt.format_quarterly_summary(data, comparison_data)
    else:
        qb = _get_qb()
        data = qb.get_quarterly_summary(quarter, year)
        comparison_data = None
        if intent_data.get("comparison", False):
            comp_q = intent_data.get("comparison_quarter", quarter - 1 if quarter > 1 else 4)
            comp_y = intent_data.get("comparison_year", year if quarter > 1 else year - 1)
            try:
                comparison_data = qb.get_quarterly_summary(comp_q, comp_y)
            except Exception as e:
                logger.warning(f"Could not fetch comparison: {e}")
        return fmt.format_quarterly_summary(data, comparison_data)


def _build_balance_sheet(intent_data: dict) -> list[dict]:
    as_of = intent_data.get("as_of_date", None)
    data = mock_data.get_balance_sheet(as_of) if Config.MOCK_MODE else _get_qb().get_balance_sheet(as_of)
    return fmt.format_balance_sheet(data)


def _build_pnl(intent_data: dict) -> list[dict]:
    start = intent_data.get("start_date", "2026-01-01")
    end = intent_data.get("end_date", "2026-01-31")
    data = mock_data.get_pnl(start, end) if Config.MOCK_MODE else _get_qb().get_pnl(start, end)
    return fmt.format_pnl(data)


def _build_cash_position(intent_data: dict) -> list[dict]:
    data = mock_data.get_cash_position() if Config.MOCK_MODE else _get_qb().get_cash_position()
    return fmt.format_cash_position(data)
