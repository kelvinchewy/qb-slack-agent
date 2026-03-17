# QuickBooks Slack Agent — Product Requirements Document

**Project:** QB Finance Agent
**Owner:** Kelvin (kelvin@hashing.com)
**Version:** 4.3
**Last Updated:** March 16, 2026
**Status:** Sprint 3 in progress — Multi-currency, month-by-month breakdown, Business Line P&L

---

## 1. Overview

A conversational Slack agent that connects to QuickBooks Online, allowing non-finance team members (C-suite, operations, technical staff) to retrieve financial data using plain English or slash commands. The agent interprets questions, pulls real data from QuickBooks, and formats it into readable Slack messages. It also exposes an HTTP API so external agents in the multi-agent system can query it programmatically.

**Design Philosophy:** Make financial data as easy to access as asking a colleague. No logins, no dashboards, no exports — just ask in Slack or use a slash command.

---

## 2. Business Context

**Company:** NEXBASE TECHNOLOGY SDN. BHD.

One P&L business line (Mining) and one revenue-only customer (Northstar/Hosting):

### 2.1 Hosting — Northstar revenue only (not a P&L segment)
- Provide mining infrastructure and electricity to external miners
- Primary customer: **NORTHSTAR MANAGEMENT (HK) LIMITED** — invoiced monthly
- Revenue = only **Services** line items on Northstar invoices (Facility Management, O&M fees)
  `Billable Expense Income` line items are pass-through costs, not revenue — excluded
- Utility-AA costs (electricity from S And E Trading) are classified under **Others**, not Hosting
- Hosting has no cost segment in P&L — Utility-AA is a lagging value that doesn't pair with invoice periods

### 2.2 Mining (Nexbase)
- Company mines BTC using its own ASICs (~200 machines across 2 Singapore sites)
- Revenue split into two buckets in QB:
  - **Realised** — actual BTC sales via LUNO, recorded as Sales Receipts in MYR
  - **Un-Realised** — monthly Journal Entries marking BTC market value at month-end
- Primary cost: Electricity from S And E Trading Sdn Bhd, allocated to **Utility - Nexbase** accounts
- BTC quantity and wallet-level tracking = handled by a separate agent (out of scope)

### 2.3 Account Classification Logic

**MINING**
| Type | QB Account / Pattern |
|------|---------------------|
| Revenue | `Revenue:Realised` (actual BTC sales via LUNO) |
| Revenue | `Revenue:Un-Realised` (monthly BTC mark-to-market Journal Entries) |
| Costs | Any account containing `Nexbase` (e.g. `Utility - Nexbase`, `Utilities - Nexbase`) — preferred |
| Costs | **Fallback:** if no Nexbase utility found, use any account whose name starts with `Utilit` (matches `Utility`, `Utilities`) and does NOT contain `AA` or `Nexbase`. Show in table, include in Net. Flag in key_findings. |
| Costs | `Rent or lease` |

Mining P&L shows **only** these account types. All other accounts that appear in QB (Un-realised fair value losses, Amortisation, Management fees, Interest expense, Other expenses, etc.) are excluded from Mining and reported under Others. This keeps Mining focused on operational economics — electricity, rent, and BTC revenue only.

**Mining Net = Revenue − Utility(Nexbase or fallback) − Rent or lease.** Always computed arithmetically from the table rows — never sourced from QB's P&L "Net Income" figure (which includes all accounts and will produce wrong totals).

**HOSTING (REVENUE ONLY — not a P&L segment)**
| Type | Source | QB Account / Pattern |
|------|--------|---------------------|
| Revenue | **Invoice query only** | Northstar invoices — sum of `Services` line items × `ExchangeRate` (MYR equivalent). `Billable Expense Income` line items are pass-throughs and excluded. |
| Costs | **Not tracked in P&L** | Utility-AA costs are classified under Others (see below) |

> **Why hosting has no cost segment:** Utility-AA costs are a lagging value — bills arrive after the period closes and do not align reliably with the Northstar revenue period. Hosting costs are reported under Others. To see hosting revenue, query Northstar invoices directly.
>
> **Why Invoice query for hosting revenue?** Northstar invoices contain multiple line item types. Only `Services` line items represent actual hosting revenue. `Billable Expense Income` lines are customer pass-throughs (e.g. electricity recharged) and are excluded. Sum the `Amount` field on each `Services` line and multiply by `ExchangeRate` on the invoice to get MYR.

**OTHERS**
| Type | QB Account / Pattern |
|------|---------------------|
| Revenue | Any revenue account NOT in Mining revenue (future revenue streams) |
| Costs | Everything not classified as Mining costs — **including Utility-AA** — normal operating expenses (Utility - AA, Amortisation, Supplies & Materials, Maintenance fees, Commissions, Internet, Subscriptions, Bank charges, Freight & delivery, Exchange Gain/Loss, Professional fees, etc.) |

**Accrual flagging:** Any transaction of type `Journal Entry` is marked **(accrued)** in output. Bills, Invoices, and Sales Receipts are actual — no flag.

**Important:** Accounts like Amortisation, Maintenance fees, Internet, and Subscriptions are **Others** costs — they are NOT Mining costs even if they relate to mining operations. Only Utility - Nexbase and Rent or lease are classified as Mining costs.

### 2.4 Multi-Currency Handling

QB multi-currency is enabled. This creates mixed-currency data across business lines:

| Business Line | Revenue Currency | Cost Currency |
|---|---|---|
| Hosting | USD (Northstar invoices) | MYR (Utility - AA) |
| Mining | MYR (LUNO BTC sales + mark-to-market) | MYR (Utility - Nexbase, Rent) |

**Default behaviour:** Report all amounts in their native QB currency (MYR from P&L report, USD from invoice queries). No conversion unless the user explicitly requests one.

**On-demand conversion:** User appends `in USD` or `in MYR` to any query. The system fetches QB's own recorded exchange rate via the ExchangeRate API and converts all figures consistently.

**Exchange rate source:** `GET /v3/company/{id}/exchangerate?sourcecurrencycode=USD&asofdate=YYYY-MM-DD`
- Uses QB's own rate — consistent with how QB recorded the original transactions
- `as_of_date` = last day of the queried period
- Rate interpretation: 1 USD = Rate MYR (e.g. Rate = 4.450 means 1 USD = MYR 4.450)
- Always shown in output footnote: *"Converted at QB rate: 1 USD = MYR X (as of YYYY-MM-DD)"*

**Known issue — Northstar invoice exchange rates in QB:** Northstar invoices are recorded in USD. If the exchange rate stored on the invoice in QB is incorrect (e.g. inverted), the MYR revenue figure in the P&L report will be wrong. This is a bookkeeping fix — correct the exchange rate field on each Northstar invoice in QB directly. The agent reports whatever QB returns; it does not validate exchange rates.

**Conversion rules:**
- MYR → USD: divide by rate
- USD → MYR: multiply by rate
- Mixed-currency P&L (hosting): convert everything to one currency before computing net
- If no ExchangeRate result available: never guess — report in original currency and flag

---

## 3. Users & Access

| User Type | Examples | Access Level |
|-----------|----------|--------------|
| C-Suite | CEO, COO | All reports, all periods |
| Operations | Site managers, technicians | All reports, all periods |
| Finance | Bookkeeper, accountant | All reports (they already have QB access) |

**v1:** Open to anyone in the Slack workspace.
**v2:** Slack user group restrictions + sensitive data redaction per role.

---

## 4. Accounting Conventions

| Term | Meaning | QB Entity | Direction |
|------|---------|-----------|-----------|
| **Invoice** | Document issued to customers requesting payment | `Invoice` (AR) | Money owed TO us |
| **Bill** | Document received from vendors requesting payment | `Bill` (AP) | Money we OWE |

The agent never guesses — "bills from S And E" queries Bills (AP), "invoices to Northstar" queries Invoices (AR).

**SQL safety rules** — these fields cause 400 errors if used in WHERE clauses:
- `VendorRef.name` on Bill — NOT filterable
- `AccountRef.name` on Bill — NOT filterable
- `CustomerRef.name` on Invoice — NOT filterable

Always fetch by date range. `TotalAmt` IS safe to filter. Analyst filters by name from returned data.

---

## 5. How to Use — Slash Commands

Type `/` in any Slack channel or DM to see all available commands. No `@mention` needed.

### 5.1 Command Reference

| Command | Default (no params) | Example with params |
|---------|--------------------|--------------------|
| `/nb-expenses` | All vendors, past 3 months | `/nb-expenses S And E past 6 months` |
| `/invoices` | All customers, past 3 months | `/invoices Northstar last quarter` |
| `/vendors` | All vendors ranked by spend, past 3 months | `/vendors past 6 months` |
| `/summary` | Last completed month, all lines | `/summary last quarter` |
| `/balance` | Balance sheet as of today | No params needed |
| `/pnl` | Mining + Others, last completed month | `/pnl mining last quarter` |
| `/hosting` | Northstar invoice revenue, last completed month | `/hosting last quarter` |
| `/finance` | — | `/finance what's our cash position` |

### 5.2 Command Detail

#### `/nb-expenses [vendor or all] [period]`
Shows vendor expenses — what we owe and what we paid. Combines QB **Bills** (AP liability) and **Purchases** (immediate vendor payments) to give a complete picture across both recording methods.

**Vendor scope:** Only transactions where the payee is a QB Vendor are included (`VendorRef` on Bills, `EntityRef.type == "Vendor"` on Purchases). Non-vendor payees (employees, customers, blank petty cash entries) are excluded automatically — they don't match any vendor in the list.

Always fetches: (1) ALL currently unpaid Bills regardless of age, (2) recent Bills in the date range, (3) recent Purchases with a QB Vendor payee in the date range. Deduplicates Bills by Id.

```
/nb-expenses                             → all vendors, past 3 months + all currently unpaid
/nb-expenses S And E past 6 months       → S And E drill-down, 6 months + all currently unpaid S&E bills
/nb-expenses top 5 last quarter          → top 5 vendors by total billed
/nb-expenses others past 3 months        → vendors in the Others bucket only
```

Output — specific vendor:
```
S AND E TRADING SDN BHD — Past 3 Months + All Unpaid

  Date        Vendor              Amount (MYR)   Status
  ----------  ------------------  ------------   -------
  2026-01-28  S And E Trading     49,200.00      Overdue
  2026-02-28  S And E Trading     49,200.00      Unpaid
  2026-01-31  S And E Trading         30.32      Paid

  UNPAID TOTAL                    98,400.00
  PAID TOTAL                          30.32
  GRAND TOTAL                     98,430.32
```

Output — all / top N:
```
TOP VENDORS BY SPEND — Past 3 Months

  #   Vendor                              Total Billed   Bills
  1   S And E Trading Sdn Bhd             RM 626,058     4
  2   NORTHSTAR MANAGEMENT (HK) LIMITED   RM 198,417     2
  3   VINTECH PLT                         RM  19,062     1
  ...
```

#### `/invoices [customer or all] [period]`
Shows AR invoices — money customers owe us.

```
/invoices                           → all customers, past 3 months
/invoices Northstar last quarter    → Northstar drill-down
/invoices all last quarter          → all customers listed
```

Output — specific customer:
```
NORTHSTAR MANAGEMENT (HK) LIMITED
Jan–Mar 2026

  #1009   12.01.2026   RM113,299.84
  #1010   12.02.2026   RM119,341.84
  #1011   16.03.2026   RM121,335.37
  ──────────────────────────────────
  Total                RM353,977.05
```

#### `/vendors [period]`
All vendors ranked by total billed. Always aggregate — no vendor filter.

```
/vendors                            → past 3 months
/vendors last quarter               → last quarter
```

#### `/summary [period]`
Top-level P&L grid for Mining and Others. One number per cell — no line item detail.

```
/summary last quarter

              Mining        Others        Total
Revenue       RM1,676,568   —             RM1,676,568
Costs         RM  199,397   RM 102,113    RM  301,510
Net           RM1,477,171   RM -102,113   RM1,374,058
```

Accruals are included but not broken out. Use `/pnl` for line-item detail.
Hosting revenue is not shown here — use `/hosting` or `/invoices Northstar`.

#### `/balance`
Balance sheet as of today. No params.

```
/balance

BALANCE SHEET — 16 Mar 2026

Assets:       RM 5,080,000
  Current:    RM 2,100,000
  Fixed:      RM 2,980,000

Liabilities:  RM 0
Equity:       RM 5,080,000
```

#### `/pnl [mining | others | all] [period]`
Full P&L for Mining and Others with accrual flagging. Hosting is not available via `/pnl` — use `/hosting` for Northstar invoice revenue. Defaults to `all` + last completed month.

```
/pnl mining last quarter

━━━ MINING ━━━
Revenue
  Revenue:Realised (BTC Sales - LUNO)    RM1,185,977   (actual)
  Revenue:Un-Realised (BTC mark-to-mkt)  RM  490,591   (accrued)
  Total Revenue                          RM1,676,568

Costs
  Utility - Nexbase electricity          RM     99   (actual)
  Utility - Nexbase accrual              RM199,297   (accrued)
  Rent or lease                          RM 44,000   (actual)
  Total Costs                            RM243,397

Net Mining                               RM1,433,171
⚠️ BTC quantity tracking handled by separate agent
```

```
/pnl others last quarter

━━━ OTHERS ━━━
Revenue
  (none — future revenue streams)

Costs
  Utility - AA accrual           RM 89,583   (accrued)
  Amortisation expense           RM113,638   (accrued)
  Maintenance fees               RM  6,494   (actual)
  Supplies and Materials - COGS  RM  6,457   (actual)
  Commissions and fees           RM    857   (actual)
  Internet                       RM    338   (actual)
  Subscriptions                  RM    246   (actual)
  Bank charges                   RM    161   (actual)
  Freight and delivery - COGS    RM     96   (actual)
  Exchange Gain or Loss          RM    -93   (actual)
  Total Others Costs             RM217,777

Net Others                       RM-217,777
```

`/pnl all` shows both Mining and Others blocks followed by a combined total.

#### Table Formatting Rules (all report types)

- **No Notes column** in any table — not in P&L, bills, invoices, or hosting tables. All observations (zero months, anomalies, fallback warnings, best/worst month) go in the written key_findings and direct_answer text below the table.
- **Net computed arithmetically** — Mining Net = Revenue − Utility − Rent, derived from the table rows. Never taken from QB's P&L "Net Income" field.
- **Month-by-month tables** (bills, invoices, hosting): `Month | Amount | # Records` only. No extra columns.

#### P&L Detail Table Format

Every `/pnl` response always shows individual line items — never a single collapsed row. Columns: **Account | Amount (MYR) | Type | % of Segment Total**

**Mining example:**
```
Account                  Amount (MYR)   Type       % of Total
Revenue:Realised         245,000        actual     29.5%
Revenue:Un-Realised      586,970        (accrued)  70.5%
Utility - Nexbase        620,879        (accrued)  73.8%
Rent or lease            220,000        actual     26.2%
─────────────────────────────────────────────────────────
Net                      -208,909
```

**Others example:**
```
Account                        Amount (MYR)  Type       % of Total
Utility - AA                    89,663       (accrued)  41.2%
Amortisation expense           113,638       (accrued)  52.2%
Maintenance fees                 6,494       actual      3.0%
Supplies and Materials - COGS    6,457       actual      3.0%
Commissions and fees               857       actual      0.4%
Internet                           338       actual      0.2%
─────────────────────────────────────────────────────────
Net Others                    -217,447
```

#### `/hosting [period]`
Northstar invoice revenue only. No costs — this is a revenue query, not a P&L.

```
/hosting last quarter

NORTHSTAR REVENUE — Q1 2026

  Invoice   Date         Amount (MYR)
  #1009     12.01.2026   RM113,299.84
  #1010     12.02.2026   RM119,341.84
  #1011     16.03.2026   RM121,335.37
  ──────────────────────────────────
  Total                  RM353,977.05
```

#### `/finance [anything]`
Free-form natural language. Same as @mentioning the bot.

```
/finance what's our cash position
/finance compare Jan vs Feb expenses
/finance flag anything unusual this month
```

### 5.3 Vendor Name Clarification

If you spell a vendor name ambiguously, the bot asks back instead of returning zero:

```
/nb-expenses TM past 6 months

🔍 Found 2 vendors matching "TM":
[TM Technology Services Sdn Bhd]  [Thomas Ung Agency Sdn Bhd]
```

Tap the button — query runs immediately.
- 1 confident match → runs without asking
- 2–3 close matches → shows buttons
- 0 matches → shows full vendor list as text

### 5.4 Natural Language (alternative to slash commands)

All slash commands have natural language equivalents via `@Nexbase Finance Agent` in `#ask-finance` or direct DM:

```
@Nexbase Finance Agent show me bills from S And E past 6 months
@Nexbase Finance Agent how did we do last quarter
@Nexbase Finance Agent P&L for hosting this year
```

---

## 6. Report Types & QB API Mapping

### 6.1 Vendor Expenses (`/nb-expenses`) — ✅ Sprint 3
**QB entities:** `Bill` (AP liability — unpaid or paid) and `Purchase` (immediate vendor payment — always paid).

**Vendor scope:** Only QB Vendor payees are shown. `VendorRef` on Bills is always a vendor. For Purchases, only include records where `EntityRef.type == "Vendor"` — this excludes petty cash, employee reimbursements, and any non-vendor payees that would never appear in the vendor list.

**Three calls always generated:**
1. `SELECT * FROM Bill WHERE Balance > '0' ORDERBY DueDate ASC MAXRESULTS 100` — ALL currently unpaid Bills, any age
2. `SELECT * FROM Bill WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100` — recent Bills (paid + unpaid)
3. `SELECT * FROM Purchase WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100` — recent Purchases (analyst filters to Vendor payees only)

**Analyst processing:**
- Merges calls 1 and 2, deduplicating by Bill `Id`
- Appends Purchases from call 3 where `EntityRef.type == "Vendor"`
- Filters to `resolved_vendors` if a specific vendor was requested
- Status: `Balance > 0` + `DueDate < today` → Overdue; `Balance > 0` → Unpaid; `Balance = 0` → Paid; Purchase → always Paid
- Sort: Overdue/Unpaid first (by DueDate ASC), then Paid (by TxnDate DESC)
- Reports Unpaid Total and Paid Total separately, then Grand Total

**Detail table columns:** Date | Vendor (truncated to 28 chars) | Amount (MYR) | Status

### 6.2 Invoices (AR) — ✅ Sprint 3
**QB API:** `SELECT * FROM Invoice WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100`
- Never filter by CustomerRef.name in SQL — analyst filters from results

### 6.3 Vendor Rankings — ✅ Sprint 3
Chain: ProfitAndLoss report + Bill query for same period. Analyst groups Bills by VendorRef.name, sums TotalAmt, ranks descending.

### 6.4 Summary Grid — 🔄 Sprint 3
**QB API:** ProfitAndLoss report. Analyst classifies accounts into Mining / Others (no Hosting column — hosting is not a P&L segment).

### 6.5 Balance Sheet — ✅ Sprint 1
**QB API:** `GET /v3/company/{id}/reports/BalanceSheet?date=YYYY-MM-DD`

### 6.6 P&L by Business Line — 🔄 Sprint 3
**P&L segments: Mining and Others only.** Hosting is not a P&L segment.

**Mining + Others:** ProfitAndLoss report for period. Accounts classified per Section 2.3. Utility-AA goes to Others. Journal Entries flagged as (accrued).

**Hosting revenue (separate):** `SELECT * FROM Invoice WHERE TxnDate >= 'X' AND TxnDate <= 'Y'` → filter to Northstar (`CustomerRef.name` contains "NORTHSTAR"), sum `Services` line items only (`Amount × ExchangeRate` per invoice → MYR). `Billable Expense Income` lines excluded. No ProfitAndLoss call.

### 6.7 Large Transactions — ✅ Sprint 3
**QB API:** `SELECT * FROM Bill WHERE TxnDate >= 'X' AND TxnDate <= 'Y' AND TotalAmt > 'N' ORDERBY TotalAmt DESC MAXRESULTS 100`

### 6.8 New Vendor Detection — ✅ Sprint 3
Two Bill queries: current period + prior period (same length). Analyst compares vendor sets.

### 6.9 BillPayment — ✅ Sprint 3
**QB API:** `SELECT * FROM BillPayment WHERE TxnDate >= 'X' AND TxnDate <= 'Y' ORDERBY TxnDate DESC MAXRESULTS 100`
Records when money actually left the account.

### 6.10 Currency Conversion — ✅ Sprint 3
**QB API:** `GET /v3/company/{id}/exchangerate?sourcecurrencycode=USD&asofdate=YYYY-MM-DD`
- Triggered only when user specifies output currency ("in USD", "in MYR")
- Chained alongside the primary data call(s)
- Rate used for all conversions in that response — labelled in footnote
- Default (no currency specified): no ExchangeRate call, amounts as-is from QB

### 6.11 Anomaly Detection — Sprint 5
Historical baseline engine. Flags vendor payments >2x baseline, duplicate payments, category spikes.

### 6.11 Payment Forecasting — Sprint 6
Open bills by due date + recurring transactions + cash position = projected cash flow.

---

## 7. Architecture

### 7.1 System Diagram

```
                            ┌──────────────────────────────────────┐
                            │         Railway Service               │
                            │                                       │
  Slash command             │  ┌────────────┐                      │
  @mention / DM             │  │  app.py     │    ┌─────────────┐  │
  ─────────────────────────►│  │  (Bolt)     │───►│ Orchestrator │  │
                            │  └────────────┘    └──────┬────────┘  │
                            │                           │           │
  HTTP (POST /query)        │  ┌────────────┐           ▼           │
  ─────────────────────────►│  │  app.py     │  ┌───────────────┐   │
   + X-API-Key header       │  │  (Flask)    │  │interpret_and_ │   │
                            │  └────────────┘  │fetch()        │   │
                            │                  │ Step 0: intent│   │
                            │                  │ Step 0.5: name│   │
                            │                  │ resolution    │   │
                            │                  │ Step 1: plan  │   │
                            │                  │ Step 2: QB API│   │
                            │                  └───────┬───────┘   │
                            │                          ▼           │
                            │                  ┌───────────────┐   │
                            │                  │ analyse()     │   │
                            │                  │ Business line │   │
                            │                  │ classification│   │
                            │                  │ Accrual flags │   │
                            │                  └───────┬───────┘   │
                            │                          ▼           │
                            │                  ┌───────────────┐   │
                            │                  │ Slack Block   │   │
                            │                  │ Kit / JSON    │   │
                            │                  └───────────────┘   │
                            └──────────────────────────────────────┘
```

**Entry points:**
- **Slash commands** (`/nb-expenses`, `/invoices`, `/vendors`, `/summary`, `/balance`, `/pnl`, `/hosting`, `/finance`) — converted to natural language queries, fed into same pipeline
- **@mention / DM** — natural language, same pipeline
- **HTTP POST /query** — agent-to-agent, same pipeline, JSON response

### 7.2 Vendor/Customer Cache

- **Loaded at startup** in a background thread before first query
- **Refreshed every 24 hours** automatically
- Holds full vendor list + full customer list from QB
- Used for name resolution (fuzzy match user input → exact QB name)
- Used for clarification buttons when match is ambiguous
- `refresh_entity_cache()` available to force refresh after adding new vendors in QB

### 7.3 Component Details

#### `app.py` — Entry point
- Slack Bolt (Socket Mode) + Flask (background thread)
- Slash command handlers: `/nb-expenses`, `/invoices`, `/vendors`, `/summary`, `/balance`, `/pnl`, `/hosting`, `/finance`
- @mention handler + DM handler
- Interactive button handler (clarification responses)
- Flask routes: `/health`, `/query`, `/auth`, `/callback`, `/auth-status`
- Startup: warms entity cache in background thread

#### `orchestrator.py` — Routes to `dynamic` or `help`

#### `qb_interpreter.py` — interpret_and_fetch()
- Step 0: Classify intent (RETRIEVAL vs FORECAST_TREND)
- Step 0.5: Detect entity name → resolve against cached vendor/customer list
- Step 1: Generate QB API call plan (Sonnet, with full entity context injected)
- Step 2: Execute QB API calls
- Returns `resolved_vendors`, `resolved_customers` at top level for analyst

#### `qb_analyst.py` — analyse()
- Receives raw QB data + resolved entity names
- Applies business line classification (Section 2.3)
- Flags Journal Entries as (accrued)
- Outputs structured CFO narrative: `direct_answer`, `key_findings`, `proactive_flags`, `detail_table`

#### `qb_agent.py` — QB API client
- OAuth 2.0 with automatic token refresh (proactive + on 401)
- After every successful refresh, writes both tokens to Railway env vars via GraphQL API
- Token expiry: access token 1 hour, refresh token 101 days (rolls on every refresh)

#### OAuth
| Action | URL |
|--------|-----|
| Re-authorize QuickBooks | `https://qb-slack-agent-production.up.railway.app/auth` |
| Check token status | `https://qb-slack-agent-production.up.railway.app/auth-status` |

---

## 8. Tech Stack

| Component | Technology |
|-----------|-----------|
| Runtime | Python 3.11+ |
| Hosting | Railway (single service) |
| Slack SDK | slack-bolt (Socket Mode) |
| HTTP API | Flask (background thread) |
| QB API | Direct REST via httpx / requests |
| AI Layer | Anthropic Claude Sonnet |
| Token Storage | Railway env vars — auto-persisted after every refresh |
| Scheduling | APScheduler (Sprint 7) |

### 8.1 Project Structure
```
qb-slack-agent/
├── app.py                  # Entry point — Bolt + Flask + 7 slash command handlers
├── orchestrator.py         # Intent classification
├── qb_agent.py             # QB API client + OAuth + Railway token persistence
├── qb_auth.py              # Legacy local OAuth (not used in production)
├── qb_interpreter.py       # interpret_and_fetch() — intent → entity resolve → QB calls
├── qb_analyst.py           # analyse() — QB data → business line P&L → CFO narrative
├── report_builder.py       # Pipeline orchestrator
├── slack_formatter.py      # Block Kit helpers — native table block for data tables
├── mock_data.py            # Mock QB responses (MOCK_MODE=true)
├── config.py               # Environment variable management
├── requirements.txt
├── railway.json
├── .github/workflows/mirror.yml
└── README.md
```

---

## 9. Setup Guide

### 9.1 Slack App

1. [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From scratch
2. Bot Token Scopes: `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `im:history`, `im:write`, `users:read`, `commands`
3. Enable **Socket Mode** first → copy `SLACK_APP_TOKEN` (`xapp-...`)
4. Event Subscriptions → Enable → `app_mention`, `message.im` → Save → Reinstall App
5. **Slash Commands** → Create Command for each of the 7 commands (Section 5.1)
   - Request URL: not needed with Socket Mode — leave blank or use Railway URL
6. Copy `SLACK_BOT_TOKEN` (`xoxb-...`) and `SLACK_SIGNING_SECRET`

### 9.2 QuickBooks App

1. [developer.intuit.com](https://developer.intuit.com) → Production credentials
2. Redirect URI: `https://qb-slack-agent-production.up.railway.app/callback`
3. After deploy: visit `/auth` to authorize

### 9.3 Environment Variables

| Variable | Source | Status |
|----------|--------|--------|
| `SLACK_APP_TOKEN` | Slack App → Socket Mode | ✅ |
| `SLACK_BOT_TOKEN` | Slack App → OAuth & Permissions | ✅ |
| `SLACK_SIGNING_SECRET` | Slack App → Basic Information | ✅ |
| `QB_CLIENT_ID` | Intuit Developer → Keys & Credentials | ✅ |
| `QB_CLIENT_SECRET` | Intuit Developer → Keys & Credentials | ✅ |
| `QB_COMPANY_ID` | `9341454299625819` (production) | ✅ |
| `QB_REDIRECT_URI` | `https://qb-slack-agent-production.up.railway.app/callback` | ✅ |
| `QB_ENVIRONMENT` | `production` | ✅ |
| `QB_ACCESS_TOKEN` | Auto-managed via `/auth` + auto-refresh | ✅ |
| `QB_REFRESH_TOKEN` | Auto-managed via `/auth` + auto-refresh | ✅ |
| `QB_API_KEY` | Self-generated, for HTTP endpoint auth | ✅ |
| `ANTHROPIC_API_KEY` | Anthropic Console | ✅ |
| `RAILWAY_API_TOKEN` | Railway → Account Settings → Tokens | ✅ |
| `RAILWAY_SERVICE_ID` | Auto-injected by Railway | Auto |
| `RAILWAY_ENVIRONMENT_ID` | Auto-injected by Railway | Auto |
| `RAILWAY_PROJECT_ID` | Auto-injected by Railway | Auto |
| `MOCK_MODE` | `false` | ✅ |
| `LOG_LEVEL` | `INFO` | ✅ |
| `PORT` | Auto-set by Railway | Auto |

---

## 10. HTTP API Reference

**Base URL:** `https://qb-slack-agent-production.up.railway.app`

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | None | Uptime check |
| `GET` | `/auth` | None | Start QB OAuth flow (browser) |
| `GET` | `/callback` | None | QB OAuth callback |
| `GET` | `/auth-status` | None | Token state + Railway config debug |
| `POST` | `/query` | `X-API-Key` | Agent-to-agent query endpoint |

---

## 11. Sprint Plan

### Sprint 1 ✅ COMPLETE — Hello World
Bot responds in Slack with mock reports. Quarterly summary, balance sheet, help.

### Sprint 2 ✅ COMPLETE — Real Data + Infrastructure
Live QB data, HTTP `/query` endpoint, Railway token persistence, production QB connected.

### Sprint 3 🔄 IN PROGRESS — Vendor/Transaction + Slash Commands + Business Line P&L

**Completed:**
- [x] Entity name resolution — vendor/customer cache, fuzzy matching
- [x] VendorRef.name SQL bug fixed — all Bill queries use date-range-only
- [x] Resolved vendor/customer names passed to analyst explicitly
- [x] Sprint 3 QB query types: large transactions, new vendors, BillPayment
- [x] Default date range for vendor/invoice queries → 3 months

**Remaining:**
- [ ] **Step 1 — Token persistence fix**
  - Startup health check: warn if token is expired at boot
  - Retry logic for Railway GraphQL persistence call
- [ ] **Step 2 — Vendor/customer cache on startup**
  - Load at boot in background thread, not lazily
  - 24h refresh cycle
- [ ] **Step 3 — 7 slash commands in app.py**
  - Register all 7 in Slack app settings
  - Each parses text → constructs natural language query → same pipeline
  - Clarification buttons for ambiguous vendor/customer names
  - Interactive button handler for clarification responses
- [ ] **Step 4 — Business line P&L logic**
  - Account classification config in analyst (AA = hosting, Nexbase = mining, else = others)
  - `/pnl` planner rules + formatter (per-line blocks with accrual flags)
  - `/summary` formatter (grid layout: Hosting / Mining / Others / Total)
  - Others drill-down: `/pnl others` → by account category, `/nb-expenses others` → by vendor

**Exit criteria:** All 7 slash commands work. `/pnl mining`, `/pnl others`, `/pnl all` return correct segmented results with accrual flagging. `/hosting` returns Northstar Services invoice revenue. Vendor clarification buttons work.

---

### Sprint 4 — Period Comparisons
- Month-to-month P&L comparison
- YTD views, trailing 12-month
- Side-by-side Slack formatting with top movers

### Sprint 5 — Anomaly Detection
- Historical baseline per vendor (6-12 months)
- Flags: >2x spend, duplicate payments, spend spikes
- Scheduled weekly anomaly scan

### Sprint 6 — Payment Forecasting
- Open bills by due date
- Cash flow projection: current cash ± upcoming AP/AR
- Recurring transaction schedule

### Sprint 7 — Scheduling + Hardening
- Automated report pushes (APScheduler):
  - Monday 9AM SGT: weekly summary to `#ask-finance`
  - 1st of month: previous month P&L
- Access controls (Slack user group restrictions)
- Rate limiting, error monitoring

---

## 12. Security & Guardrails

| Concern | Mitigation |
|---------|-----------|
| Data exposure | Read-only — zero write access to QuickBooks |
| Token security | OAuth tokens in Railway encrypted env vars, never logged |
| Token refresh | Proactive 10 min before expiry + forced on 401 |
| Token rotation | QB rotates refresh token on every refresh — both persisted to Railway automatically |
| Startup token check | Warns in logs if token is expired at boot — admin visits `/auth` to fix |
| Re-authorization | Visit `/auth` on Railway URL — no local tools needed |
| API endpoint | Protected by `QB_API_KEY` / `X-API-Key` header |
| Error leakage | Never expose raw errors, stack traces, or token values |
| Data storage | No financial data stored — real-time queries only |

---

## 13. v2 Roadmap (Post-Sprint 7)

- **Mining KPIs:** Revenue per ASIC, cost per BTC mined, power cost ratio (requires mining pool + utility data from separate agent)
- **Budget vs Actual:** Compare planned budgets against QB actuals (targets from Notion)
- **BTC quantity integration:** Connect to BTC tracking agent for mining-side unit economics
- **Multi-company:** Support multiple QB entities if business expands
- **Dashboard artifact:** Periodic HTML dashboard pinned to Slack
- **Natural follow-ups:** "drill into that" / "show me more detail on line 3"
- **Notion logging:** Deferred until data quality is stable

---

## 14. Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| Mar 2026 | Monolith architecture for v1 | Small team, single Railway service |
| Mar 2026 | Socket Mode for Slack | No public URL needed |
| Mar 2026 | Mock data first, real data Sprint 2 | Validate pipeline before live data |
| Mar 2026 | Claude Sonnet for orchestration | Fast + cheap for classification |
| Mar 2026 | Open access for v1 | Small team, low risk |
| Mar 2026 | Mining KPIs deferred to v2 | Requires non-QB data sources |
| Mar 2026 | Notion logging deferred | Finalize data quality first |
| Mar 2026 | Socket Mode before Event Subscriptions | Slack won't save Events without Socket Mode on first |
| Mar 2026 | Flask in background thread | Same process as Socket Mode — simple deployment |
| Mar 2026 | SSH deploy key for GitHub mirror | `kelvinhashing` is personal account — PATs fail |
| Mar 2026 | Disable GitHub Actions on mirror repo | Prevents workflow re-running on mirror |
| Mar 2026 | API key on HTTP endpoint | Multi-agent coordinator access |
| Mar 2026 | OAuth callback on Railway `/auth` | Intuit blocks localhost for production apps |
| Mar 2026 | Token persistence via Railway GraphQL | QB rotates refresh token — must persist both or they expire on restart |
| Mar 2026 | Entity cache loaded at startup | Lazy loading caused silent thread hangs on first query |
| Mar 2026 | Planner receives full entity lists | Sonnet resolves fuzzy names in-context — more reliable than separate step |
| Mar 2026 | VendorRef.name not filterable in SQL | Causes 400 Bad Request — same as AccountRef.name and CustomerRef.name |
| Mar 2026 | Strict accounting terminology | Invoice = AR, Bill = AP — no guessing from word choice |
| Mar 2026 | Thinking message posted immediately | No feedback during 8-15s processing; updates in-place |
| Mar 2026 | 7 slash commands added | Faster UX for common queries — no @mention needed |
| Mar 2026 | Default date range 3 months for vendor queries | "Current month" too narrow — most vendor queries need history |
| Mar 2026 | Business line P&L via account name classification | QB already segments via account names (AA = hosting, Nexbase = mining) |
| Mar 2026 | Mining costs = Utility-Nexbase + Rent or lease only | All other opex (amortisation, maintenance, internet, etc.) goes to Others — keeps Mining P&L focused on direct operational costs |
| Mar 2026 | Others revenue = catch-all for future streams | No other revenue today; bucket exists for when new revenue lines are added |
| Mar 2026 | P&L always shows line item breakdown, never single net row | Users need to see individual accounts to understand the composition — collapsed tables are not useful |
| Mar 2026 | Forecast/trend pipeline removed from scope | Was causing misclassification of bill/vendor queries as FORECAST_TREND; deferred to later sprint |
| Mar 2026 | Accrual basis — Journal Entries flagged as (accrued) | Company runs accrual accounting; users need to distinguish estimates from actuals |
| Mar 2026 | Others = single bucket unless drilled into | Keeps summary clean; `/pnl others` or `/nb-expenses others` reveals detail on demand |
| Mar 2026 | Clarification buttons for ambiguous vendor names | Return zero results is unhelpful — ask back with options instead |
| Mar 2026 | Multi-currency via QB ExchangeRate API, not live FX | Consistency — same rate QB used when recording transactions; no external dependency |
| Mar 2026 | Default currency = as-is from QB, conversion on-demand | Avoids silent conversion errors; user opts in explicitly with "in USD" / "in MYR" |
| Mar 2026 | Month-by-month breakdown = one QB call per calendar month | Analyst needs separate reports per month to build per-month rows; single aggregate call loses monthly granularity |
| Mar 2026 | Month-by-month row filter bypass in formatter | Formatter row filter designed for account-name rows; month rows ("Oct 2025") don't contain business line keywords and were being stripped |
| Mar 2026 | Combined P&L uses per-account rows per section, not summary labels | "MINING REVENUE" / "HOSTING COSTS" labels are meaningless — actual QB account names give context |
| Mar 2026 | Revenue queries always route to ProfitAndLoss, never Invoice | Mining revenue (Revenue:Realised, Un-Realised) lives in P&L accounts; routing to Invoice returns zero |
| Mar 2026 | Hosting revenue sourced from Invoice query, Services lines only | Northstar invoices contain two line item types: `Services` (actual hosting fee) and `Billable Expense Income` (pass-through recharges, not revenue). Only `Services` lines count. Sum each Services line Amount × ExchangeRate per invoice → MYR. HomeTotalAmt includes Billable Expense Income and must NOT be used. |
| Mar 2026 | Hosting removed as P&L segment — revenue query only | Utility-AA costs are lagging (bills arrive after period close) so hosting cost vs revenue comparison in P&L is unreliable. Hosting = Invoice query for Northstar revenue only. Utility-AA classified under Others. P&L segments are Mining and Others only. |
| Mar 2026 | "COMBINED NET" added to formatter row filter passthrough | Combined multi-line P&L net row was being silently stripped by business-line keyword filter |
| Mar 2026 | max_tokens raised to 6000 | Prompt growth from detail table specs + currency rules exceeded 4000 token output budget |
| Mar 2026 | Table rendering upgraded to Slack native `table` block | Monospace padding in section blocks broke alignment on columns with varying digit counts (e.g. MYR 1,200 vs MYR 110,000); native table block provides right-aligned numeric columns with no manual padding |

---

## 15. References

- [QuickBooks Online API Docs](https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/account)
- [QuickBooks Report API](https://developer.intuit.com/app/developer/qbo/docs/api/accounting/most-commonly-used/profitandloss)
- [QuickBooks Query API](https://developer.intuit.com/app/developer/qbo/docs/learn/explore-the-quickbooks-online-api/data-queries)
- [Slack Bolt for Python](https://slack.dev/bolt-python/tutorial/getting-started)
- [Slack Block Kit Builder](https://app.slack.com/block-kit-builder)
- [Anthropic Claude API Docs](https://docs.anthropic.com/)
- [Railway GraphQL API](https://docs.railway.app/reference/public-api)

---

*Sprints 1–2 complete. Sprint 3 in progress — 4 remaining steps before Sprint 4.*