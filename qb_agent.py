"""
qb_agent.py — QuickBooks Online API client.

Handles:
- Authenticated API calls using stored OAuth tokens
- Automatic access token refresh before expiry
- Fetching and normalizing QB report data into the same
  shape that report_builder.py / mock_data.py already uses.

Called by report_builder.py when MOCK_MODE=false.
"""

import logging
import os
import time
from datetime import datetime, timedelta

import httpx
from requests.auth import HTTPBasicAuth
import requests

from config import Config

logger = logging.getLogger(__name__)

# ─── QB API Base URLs ─────────────────────────────────────────────────

BASE_URLS = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

# Refresh access token if it expires within this many seconds
REFRESH_BUFFER_SECONDS = 600  # 10 minutes


# ─── Token Manager ────────────────────────────────────────────────────

class TokenManager:
    """
    Manages QB OAuth tokens in memory.
    Reads from Config (Railway env vars) on init.
    Refreshes access token automatically when near expiry or on 401.

    Sets expires_at to 0 on init so the first API call always
    triggers a proactive refresh — safely handles expired tokens on startup.
    """

    def __init__(self):
        self.access_token = Config.QB_ACCESS_TOKEN
        self.refresh_token = Config.QB_REFRESH_TOKEN
        # Force refresh on first call — handles already-expired tokens on startup
        self.expires_at = 0

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        if self._needs_refresh():
            logger.info("Access token expired or near expiry — refreshing...")
            self._refresh()
        return self.access_token

    def force_refresh(self):
        """Force an immediate token refresh — called on 401 responses."""
        logger.info("401 received — forcing token refresh...")
        self.expires_at = 0
        self._refresh()

    def _needs_refresh(self) -> bool:
        return time.time() >= (self.expires_at - REFRESH_BUFFER_SECONDS)

    def _refresh(self):
        """Exchange refresh token for a new access token and persist to Railway."""
        try:
            response = requests.post(
                TOKEN_URL,
                auth=HTTPBasicAuth(Config.QB_CLIENT_ID, Config.QB_CLIENT_SECRET),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
            )

            if response.status_code != 200:
                logger.error(f"Token refresh failed: {response.status_code} — {response.text}")
                raise Exception("QB token refresh failed. Re-run qb_auth.py to re-authorize.")

            tokens = response.json()
            self.access_token = tokens["access_token"]
            self.refresh_token = tokens.get("refresh_token", self.refresh_token)
            self.expires_at = time.time() + tokens.get("expires_in", 3600)

            logger.info("✅ QB access token refreshed successfully.")

            # Persist new tokens to Railway so they survive container restarts
            _persist_tokens_to_railway(self.access_token, self.refresh_token)

        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            raise


# ─── Railway Token Persistence ───────────────────────────────────────

def _persist_tokens_to_railway(access_token: str, refresh_token: str):
    """
    Write new QB tokens back to Railway environment variables.
    This ensures tokens survive container restarts.
    Requires RAILWAY_API_TOKEN and RAILWAY_SERVICE_ID env vars.
    Fails silently if Railway API is not configured — tokens still work in memory.
    """
    railway_token = os.environ.get("RAILWAY_API_TOKEN", "")
    service_id = os.environ.get("RAILWAY_SERVICE_ID", "")
    environment_id = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")

    if not railway_token or not service_id:
        logger.warning("Railway API not configured — tokens updated in memory only. "
                       "Set RAILWAY_API_TOKEN and RAILWAY_SERVICE_ID to persist across restarts.")
        return

    try:
        # Railway GraphQL API to update env vars
        mutation = """
        mutation UpdateServiceVariables($serviceId: String!, $environmentId: String!, $variables: ServiceVariables!) {
            serviceVariablesUpsert(
                serviceId: $serviceId,
                environmentId: $environmentId,
                variables: $variables
            )
        }
        """
        variables = {
            "serviceId": service_id,
            "environmentId": environment_id,
            "variables": {
                "QB_ACCESS_TOKEN": access_token,
                "QB_REFRESH_TOKEN": refresh_token,
            }
        }

        resp = requests.post(
            "https://backboard.railway.app/graphql/v2",
            headers={
                "Authorization": f"Bearer {railway_token}",
                "Content-Type": "application/json",
            },
            json={"query": mutation, "variables": variables},
            timeout=10,
        )

        if resp.status_code == 200 and "errors" not in resp.json():
            logger.info("✅ Tokens persisted to Railway env vars successfully.")
        else:
            logger.warning(f"Railway token persist failed: {resp.status_code} — {resp.text[:200]}")

    except Exception as e:
        logger.warning(f"Railway token persist error (non-fatal): {e}")


# Singleton token manager — shared across all requests
_token_manager = TokenManager()


# ─── QB API Client ────────────────────────────────────────────────────

class QBClient:
    """
    Low-level QuickBooks API client.
    Handles auth headers, base URL, and error handling.
    """

    def __init__(self):
        self.base_url = BASE_URLS.get(Config.QB_ENVIRONMENT, BASE_URLS["sandbox"])
        self.company_id = Config.QB_COMPANY_ID

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {_token_manager.get_access_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def get_report(self, report_name: str, params: dict = None) -> dict:
        """Fetch a QB report by name with optional query params. Retries once on 401."""
        url = f"{self.base_url}/v3/company/{self.company_id}/reports/{report_name}"
        try:
            response = httpx.get(url, headers=self._headers(), params=params or {}, timeout=15)
            if response.status_code == 401:
                logger.warning(f"401 on {report_name} — refreshing token and retrying...")
                _token_manager.force_refresh()
                response = httpx.get(url, headers=self._headers(), params=params or {}, timeout=15)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"QB API error {e.response.status_code} for {report_name}: {e.response.text}")
            raise
        except httpx.TimeoutException:
            logger.error(f"QB API timeout for {report_name}")
            raise Exception("QuickBooks API timed out. Please try again.")

    def query(self, sql: str) -> dict:
        """Run a QB query (SELECT statements). Retries once on 401."""
        url = f"{self.base_url}/v3/company/{self.company_id}/query"
        try:
            response = httpx.get(
                url,
                headers=self._headers(),
                params={"query": sql, "minorversion": "65"},
                timeout=15,
            )
            if response.status_code == 401:
                logger.warning("401 on query — refreshing token and retrying...")
                _token_manager.force_refresh()
                response = httpx.get(
                    url,
                    headers=self._headers(),
                    params={"query": sql, "minorversion": "65"},
                    timeout=15,
                )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"QB query error {e.response.status_code}: {e.response.text}")
            raise


# Singleton client
_client = QBClient()


# ─── Public Interface for qb_interpreter ─────────────────────────────

def get_report(report_name: str, params: dict = None) -> dict:
    """Public wrapper — fetch a QB report by name."""
    return _client.get_report(report_name, params)


def query(sql: str) -> dict:
    """Public wrapper — run a QB SQL query."""
    return _client.query(sql)


# ─── Report Helpers ───────────────────────────────────────────────────

def _find_row(rows: list, row_name: str) -> float:
    """
    Recursively search QB report rows for a named summary value.
    QB reports use nested row structures — this flattens the search.
    """
    for row in rows:
        # Check this row's header/label
        header = row.get("Header", {})
        if header.get("ColData"):
            col_data = header["ColData"]
            if col_data and col_data[0].get("value", "").lower() == row_name.lower():
                # Look for summary value in this section
                summary = row.get("Summary", {})
                if summary.get("ColData"):
                    for col in summary["ColData"]:
                        val = col.get("value", "")
                        if val and val not in ("", "0"):
                            try:
                                return float(val)
                            except ValueError:
                                pass

        # Check rows within this row (QB nests sections)
        if "Rows" in row and row["Rows"].get("Row"):
            result = _find_row(row["Rows"]["Row"], row_name)
            if result is not None:
                return result

        # Check summary rows
        if row.get("type") == "Section":
            summary = row.get("Summary", {})
            if summary.get("ColData"):
                col_data = summary["ColData"]
                if col_data and col_data[0].get("value", "").lower() == row_name.lower():
                    for col in col_data[1:]:
                        val = col.get("value", "")
                        if val:
                            try:
                                return float(val)
                            except ValueError:
                                pass

    return None


def _extract_pnl_rows(report_data: dict) -> dict:
    """
    Parse raw QB ProfitAndLoss report JSON into a flat dict.
    Handles QB's deeply nested row/section structure.
    """
    rows = report_data.get("Rows", {}).get("Row", [])

    def get_val(name):
        val = _find_row(rows, name)
        return val or 0.0

    # Core P&L values — QB uses these standard section names
    total_income = get_val("Total Income") or get_val("Total Revenue") or get_val("Income")
    total_cogs = get_val("Total Cost of Goods Sold") or get_val("Cost of Goods Sold") or get_val("COGS")
    gross_profit = get_val("Gross Profit")
    total_expenses = get_val("Total Expenses") or get_val("Total Operating Expenses")
    net_income = get_val("Net Income") or get_val("Net Profit")

    # If gross profit not a direct row, calculate it
    if not gross_profit and total_income and total_cogs:
        gross_profit = total_income - total_cogs

    gross_margin = (gross_profit / total_income * 100) if total_income else 0
    net_margin = (net_income / total_income * 100) if total_income else 0

    return {
        "revenue": {"total": total_income, "mining_revenue": total_income, "hosting_revenue": 0},
        "cogs": {"total": total_cogs, "electricity": 0, "facility_lease": 0, "equipment_depreciation": 0, "pool_fees": 0},
        "gross_profit": gross_profit,
        "gross_margin_pct": round(gross_margin, 1),
        "operating_expenses": {"total": total_expenses, "salaries": 0, "insurance": 0, "maintenance": 0, "software_subscriptions": 0, "travel": 0},
        "net_income": net_income,
        "net_margin_pct": round(net_margin, 1),
    }


# ─── Public API — matches mock_data.py interface ──────────────────────

def get_quarterly_summary(quarter: int, year: int) -> dict:
    """
    Fetch real QB P&L for a given quarter.
    Returns data in same shape as mock_data.get_quarterly_summary().
    """
    quarter_dates = {
        1: ("01-01", "03-31"),
        2: ("04-01", "06-30"),
        3: ("07-01", "09-30"),
        4: ("10-01", "12-31"),
    }

    if quarter not in quarter_dates:
        raise ValueError(f"Invalid quarter: {quarter}")

    start_suffix, end_suffix = quarter_dates[quarter]
    start_date = f"{year}-{start_suffix}"
    end_date = f"{year}-{end_suffix}"

    logger.info(f"Fetching QB P&L: Q{quarter} {year} ({start_date} → {end_date})")

    raw = _client.get_report("ProfitAndLoss", {
        "start_date": start_date,
        "end_date": end_date,
        "minorversion": "65",
    })

    data = _extract_pnl_rows(raw)
    data["period"] = f"Q{quarter} {year}"
    data["start_date"] = start_date
    data["end_date"] = end_date

    return data


def get_balance_sheet(as_of_date: str = None) -> dict:
    """
    Fetch real QB Balance Sheet.
    Returns data in same shape as mock_data.get_balance_sheet().
    """
    date = as_of_date or datetime.now().strftime("%Y-%m-%d")
    logger.info(f"Fetching QB Balance Sheet as of {date}")

    raw = _client.get_report("BalanceSheet", {
        "date": date,
        "minorversion": "65",
    })

    rows = raw.get("Rows", {}).get("Row", [])

    def get_val(name):
        return _find_row(rows, name) or 0.0

    total_assets = get_val("Total Assets") or get_val("TOTAL ASSETS")
    total_current_assets = get_val("Total Current Assets")
    total_fixed_assets = get_val("Total Fixed Assets") or get_val("Total Non-current Assets")
    cash = get_val("Total Bank Accounts") or get_val("Cash and Cash Equivalents") or get_val("Bank")
    ar = get_val("Accounts Receivable (A/R)") or get_val("Accounts Receivable")
    total_liabilities = get_val("Total Liabilities") or get_val("TOTAL LIABILITIES")
    total_current_liabilities = get_val("Total Current Liabilities")
    total_long_term = get_val("Total Long-Term Liabilities") or get_val("Total Non-current Liabilities")
    ap = get_val("Accounts Payable (A/P)") or get_val("Accounts Payable")
    total_equity = get_val("Total Equity") or get_val("TOTAL EQUITY")

    current_ratio = (total_current_assets / total_current_liabilities) if total_current_liabilities else 0
    debt_to_equity = (total_liabilities / total_equity) if total_equity else 0

    return {
        "as_of_date": date,
        "assets": {
            "total": total_assets,
            "current_assets": {
                "total": total_current_assets,
                "cash_and_bank": cash,
                "accounts_receivable": ar,
                "btc_holdings": 0,
                "prepaid_expenses": 0,
            },
            "fixed_assets": {
                "total": total_fixed_assets,
                "mining_equipment": total_fixed_assets,
                "accumulated_depreciation": 0,
                "leasehold_improvements": 0,
                "office_equipment": 0,
            },
        },
        "liabilities": {
            "total": total_liabilities,
            "current_liabilities": {
                "total": total_current_liabilities,
                "accounts_payable": ap,
                "accrued_expenses": 0,
                "current_portion_debt": 0,
            },
            "long_term_liabilities": {
                "total": total_long_term,
                "equipment_financing": total_long_term,
                "facility_deposit": 0,
            },
        },
        "equity": {
            "total": total_equity,
            "owner_equity": total_equity,
            "retained_earnings": 0,
        },
        "ratios": {
            "current_ratio": round(current_ratio, 2),
            "debt_to_equity": round(debt_to_equity, 2),
            "quick_ratio": round(current_ratio, 2),
        },
    }


def get_pnl(start_date: str, end_date: str) -> dict:
    """
    Fetch real QB P&L for a custom date range.
    Returns data in same shape as mock_data.get_pnl().
    """
    logger.info(f"Fetching QB P&L: {start_date} → {end_date}")

    raw = _client.get_report("ProfitAndLoss", {
        "start_date": start_date,
        "end_date": end_date,
        "minorversion": "65",
    })

    data = _extract_pnl_rows(raw)
    data["period"] = f"{start_date} to {end_date}"
    data["start_date"] = start_date
    data["end_date"] = end_date

    return data


def get_cash_position() -> dict:
    """
    Fetch real QB cash, AR aging, AP aging.
    Returns data in same shape as mock_data.get_cash_position().
    """
    logger.info("Fetching QB cash position, AR aging, AP aging...")

    today = datetime.now().strftime("%Y-%m-%d")

    # Fetch all three reports
    try:
        ar_raw = _client.get_report("AgedReceivables", {"minorversion": "65"})
    except Exception:
        ar_raw = {}

    try:
        ap_raw = _client.get_report("AgedPayables", {"minorversion": "65"})
    except Exception:
        ap_raw = {}

    # Fetch bank account balances via query
    cash_total = 0.0
    try:
        bank_data = _client.query("SELECT * FROM Account WHERE AccountType='Bank' AND Active=true MAXRESULTS 20")
        accounts = bank_data.get("QueryResponse", {}).get("Account", [])
        cash_total = sum(float(acc.get("CurrentBalance", 0)) for acc in accounts)
    except Exception as e:
        logger.warning(f"Could not fetch bank balances: {e}")

    # Parse AR aging
    ar_rows = ar_raw.get("Rows", {}).get("Row", []) if ar_raw else []
    ar_total = _find_row(ar_rows, "Total Accounts Receivable") or _find_row(ar_rows, "TOTAL") or 0.0

    # Parse AP aging
    ap_rows = ap_raw.get("Rows", {}).get("Row", []) if ap_raw else []
    ap_total = _find_row(ap_rows, "Total Accounts Payable") or _find_row(ap_rows, "TOTAL") or 0.0

    return {
        "as_of_date": today,
        "cash_balances": {
            "total": cash_total,
            "operating_account": cash_total,
            "payroll_account": 0,
            "savings_reserve": 0,
        },
        "btc_holdings": {
            "btc_amount": 0,
            "usd_value": 0,
        },
        "accounts_receivable": {
            "total": ar_total,
            "current": ar_total,
            "30_days": 0,
            "60_days": 0,
            "90_plus_days": 0,
            "top_outstanding": [],
        },
        "accounts_payable": {
            "total": ap_total,
            "current": ap_total,
            "30_days": 0,
            "60_days": 0,
            "90_plus_days": 0,
            "top_upcoming": [],
        },
    }