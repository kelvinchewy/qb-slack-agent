# QB Slack Agent — Nexbase Finance

A conversational Slack bot that connects to QuickBooks Online and answers financial questions in plain English. Ask it anything about your finances and it pulls live data from QuickBooks, interprets it with Claude (Anthropic), and responds with formatted reports directly in Slack.

---

## What It Does

Mention the bot or DM it with any financial question:

> "What's our cash position?"
> "Show me P&L for Q1"
> "Who owes us money?"
> "Any unusual expenses lately?"

It figures out what to pull from QuickBooks, fetches the data, and replies with a clean Slack-formatted report — no commands needed.

---

## Architecture

```
Slack message
     ↓
Orchestrator (intent classifier)
     ↓
Report Builder (Claude via Anthropic API)
     ↓
qb_interpreter.py → qb_agent.py → QuickBooks Online API
     ↓
slack_formatter.py → Slack Block Kit response
```

- **`app.py`** — Slack Bolt app entry point (Socket Mode)
- **`orchestrator.py`** — Routes messages; sends everything to Claude's dynamic pipeline
- **`report_builder.py`** — Calls Claude to interpret the query and build a response
- **`qb_interpreter.py`** — Translates Claude's intent into QB API calls
- **`qb_agent.py`** — QuickBooks API client with automatic token refresh
- **`qb_auth.py`** — One-time OAuth flow to get your QB tokens
- **`slack_formatter.py`** — Formats responses into Slack Block Kit
- **`mock_data.py`** — Fake data for local development without QB credentials
- **`config.py`** — Loads and validates all environment variables

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/kelvinchewy/qb-slack-agent.git
cd qb-slack-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Fill in the values (see **Environment Variables** below).

### 3. Get QuickBooks OAuth tokens

Run the one-time auth flow to authorize QuickBooks and get your tokens:

```bash
python3 qb_auth.py
```

This opens a browser, walks you through QuickBooks OAuth, and prints your `QB_ACCESS_TOKEN`, `QB_REFRESH_TOKEN`, and `QB_COMPANY_ID`. Copy those into your `.env` (and Railway if deploying).

> The refresh token lasts 101 days of inactivity. Re-run `qb_auth.py` if it expires.

### 4. Run the bot

```bash
python3 app.py
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SLACK_APP_TOKEN` | Yes | Socket Mode token (`xapp-...`) |
| `SLACK_BOT_TOKEN` | Yes | Bot OAuth token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | Yes | App signing secret |
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `QB_CLIENT_ID` | Yes | QuickBooks app client ID |
| `QB_CLIENT_SECRET` | Yes | QuickBooks app client secret |
| `QB_COMPANY_ID` | Yes | QuickBooks company/realm ID |
| `QB_ACCESS_TOKEN` | Yes | QB OAuth access token (from `qb_auth.py`) |
| `QB_REFRESH_TOKEN` | Yes | QB OAuth refresh token (from `qb_auth.py`) |
| `QB_ENVIRONMENT` | Yes | `sandbox` or `production` |
| `QB_REDIRECT_URI` | No | Default: `http://localhost:8080/callback` |
| `MOCK_MODE` | No | `true` to use fake data locally (default: `true`) |
| `LOG_LEVEL` | No | `INFO`, `DEBUG`, etc. (default: `INFO`) |
| `NOTION_API_KEY` | No | For Notion logging (future) |
| `NOTION_DATABASE_ID` | No | For Notion logging (future) |

---

## Development vs Production

Set `MOCK_MODE=true` in your `.env` to run locally without real QuickBooks credentials — the bot will use `mock_data.py` instead of hitting the API.

Set `MOCK_MODE=false` with real QB tokens for live data.

---

## Deploying to Railway

1. Push your code to GitHub
2. Create a new Railway project linked to the repo
3. Add all environment variables from the table above in Railway's settings
4. Railway will auto-deploy on every push to `main`

The `railway.json` in this repo configures the build and start commands.

---

## What You Can Ask

- **Reports** — "Balance sheet", "P&L for Q1", "How did we do last quarter?"
- **Cash** — "What's our cash position?", "What bills are due?"
- **AR/AP** — "Who owes us money?", "What do we owe vendors?"
- **Vendor lookups** — "Show me all bills from PowerGrid this year"
- **Analysis** — "Anything I should be worried about?", "Unusual expenses lately?"
- **Forecasting** — "Cashflow next 30 days?", "Upcoming large bills?"

---

## Token Refresh

The bot automatically refreshes the QB access token before it expires (every ~60 minutes). No manual action needed as long as your `QB_REFRESH_TOKEN` is valid (101 days of inactivity). If it expires, re-run `qb_auth.py` and update your tokens.
