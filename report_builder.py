"""
Report Builder — Routes all queries through the dynamic pipeline.
Fixed report formatters removed. Everything goes via qb_interpreter → qb_analyst.
"""

import logging
import slack_formatter as fmt
from orchestrator import get_help_text

logger = logging.getLogger(__name__)


def build_report(intent_data: dict) -> list[dict]:
    intent = intent_data.get("intent", "unknown")
    route = intent_data.get("route", "dynamic")

    logger.info(f"Building report | intent={intent}")

    try:
        if intent == "help":
            return fmt.format_help(get_help_text())

        # Everything goes through dynamic pipeline
        return _build_dynamic(intent_data)

    except Exception as e:
        logger.error(f"Report build error: {e}")
        return fmt.format_error("Something went wrong. Please try again.")


def _build_dynamic(intent_data: dict) -> list[dict]:
    from qb_interpreter import interpret_and_fetch
    from qb_analyst import analyse
    from qb_auditor import audit

    question = intent_data.get("original_question", "")
    if not question:
        return fmt.format_error("I lost track of your question. Please try again.")

    interpreter_result = interpret_and_fetch(question)
    analysis = analyse(interpreter_result)
    analysis = audit(analysis, interpreter_result)
    return fmt.format_dynamic_analysis(analysis)