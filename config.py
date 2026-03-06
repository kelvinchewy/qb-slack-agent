"""
Configuration management for QB Slack Agent.
Loads environment variables with sensible defaults.
"""

import os


class Config:
    # Slack
    SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
    SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
    SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")

    # QuickBooks
    QB_CLIENT_ID = os.environ.get("QB_CLIENT_ID", "")
    QB_CLIENT_SECRET = os.environ.get("QB_CLIENT_SECRET", "")
    QB_COMPANY_ID = os.environ.get("QB_COMPANY_ID", "")
    QB_REDIRECT_URI = os.environ.get("QB_REDIRECT_URI", "http://localhost:8080/callback")
    QB_ENVIRONMENT = os.environ.get("QB_ENVIRONMENT", "sandbox")
    QB_ACCESS_TOKEN = os.environ.get("QB_ACCESS_TOKEN", "")
    QB_REFRESH_TOKEN = os.environ.get("QB_REFRESH_TOKEN", "")

    # Notion
    NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
    NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

    # Anthropic
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

    # App
    MOCK_MODE = os.environ.get("MOCK_MODE", "true").lower() == "true"
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

    @classmethod
    def validate_slack(cls):
        """Check that required Slack tokens are set."""
        missing = []
        if not cls.SLACK_APP_TOKEN:
            missing.append("SLACK_APP_TOKEN")
        if not cls.SLACK_BOT_TOKEN:
            missing.append("SLACK_BOT_TOKEN")
        if not cls.SLACK_SIGNING_SECRET:
            missing.append("SLACK_SIGNING_SECRET")
        if missing:
            raise ValueError(f"Missing required Slack env vars: {', '.join(missing)}")

    @classmethod
    def validate_anthropic(cls):
        """Check that Anthropic API key is set."""
        if not cls.ANTHROPIC_API_KEY:
            raise ValueError("Missing ANTHROPIC_API_KEY")

    @classmethod
    def validate_quickbooks(cls):
        """Check that QuickBooks credentials are set."""
        missing = []
        if not cls.QB_CLIENT_ID:
            missing.append("QB_CLIENT_ID")
        if not cls.QB_CLIENT_SECRET:
            missing.append("QB_CLIENT_SECRET")
        if not cls.QB_COMPANY_ID:
            missing.append("QB_COMPANY_ID")
        if missing:
            raise ValueError(f"Missing required QuickBooks env vars: {', '.join(missing)}")
