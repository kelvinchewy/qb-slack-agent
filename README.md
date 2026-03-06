# QB Slack Agent — Nexbase Finance Agent

Conversational Slack bot that pulls financial data from QuickBooks Online. Ask questions in plain English, get formatted reports.

## Quick Start

1. Clone repo
2. Copy `.env.example` → `.env` and fill in your tokens
3. `pip install -r requirements.txt`
4. `python app.py`

## Architecture

```
Slack message → Orchestrator (Claude) → Report Builder → Slack Block Kit response
                                              ↓
                                    Mock Data (dev) / QuickBooks API (prod)
```

## Sprint Status

- [x] Sprint 1: Slack bot + mock data
- [ ] Sprint 2: QuickBooks OAuth + real data
- [ ] Sprint 3: P&L comparisons + Notion logging
- [ ] Sprint 4: Weekly reports + scheduling

## Environment Variables

See `.env.example` for full list. Minimum for Sprint 1:
- `SLACK_APP_TOKEN` — Socket Mode token (xapp-...)
- `SLACK_BOT_TOKEN` — Bot OAuth token (xoxb-...)  
- `SLACK_SIGNING_SECRET` — App signing secret
- `ANTHROPIC_API_KEY` — Claude API key
- `MOCK_MODE=true` — Use mock data
