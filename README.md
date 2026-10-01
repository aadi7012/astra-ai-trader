# Astra AI Trader

A Windows desktop prototype for local-AI-assisted Binance Spot market analysis
and user-authorized automation. It has separate Testnet and production API
clients; **LIVE mode sends real Binance Spot orders** after explicit in-app
confirmation. This is not investment advice or a profitability claim.
Automated trading can lose some or all of the funds in the account.

> **Read before running:** This project has not demonstrated a profitable
> strategy and is not production-ready. Start with Testnet. Passing tests and
> an AI BUY signal do not predict a profitable fill. LIVE mode can lose real
> funds.

## Start here

- **[Step-by-step setup and user guide](USER_GUIDE.md)** — Windows setup,
  Ollama, Testnet, dashboard, automation, exits, troubleshooting, and LIVE
  account restrictions.
- **Requirements:** Windows 10/11, Python 3.10+ with Tkinter when running from
  source, and a local Ollama model (`qwen2.5:3b` by default).
- **Quick run from source:** install Ollama, run `ollama pull qwen2.5:3b`,
  then in PowerShell run `python .\app.py`.
- **Test the code:** `python -m unittest discover -v -s . -p "test_*.py"`.
- **Build a Windows executable or installer:** see the build sections below.

### Scope and publishing

This repository contains the Windows desktop application only. It does not
implement Binance Margin or Futures order execution. Public visibility does
not imply endorsement by Binance or Ollama.

There is intentionally **no license file**. The source may be viewed on
GitHub, but no permission to copy, modify, redistribute, or use it is granted
by this publication. Contact the repository owner to request permission.

## Current status

- Binance Spot Testnet public market-data reads have been exercised.
- Signed account and order flows have unit coverage, but production account
  access and LIVE orders have not been verified against an independent
  production account.
- Automated tests cover the local trading, ledger, UI-helper and exchange-client
  behavior; run `python -m unittest discover -q -s . -p "test_*.py"` to see the
  current count.
- The local Ollama integration and configured model response validation have
  been exercised.
- No reviewed or forward paper-trading results demonstrate strategy
  profitability. The new candle-based technical-proxy backtest is approximate
  and does not evaluate historical Ollama decisions. Model-reported confidence
  is not calibrated.

Do not consider this application production-ready solely because tests pass.
Do not enter credentials for an account or region where Binance access is not
authorized. Review Binance's current terms, local rules, and tax obligations.

## What the app does

- Native Tkinter desktop UI; no web or mobile surface.
- Dashboard includes a fetched Binance candlestick/volume chart with selectable
  interval, optional 60-second refresh, price/period-change metrics, live
  account-balance table, and CSV snapshot export.
- The dashboard's **Backtest** tool fetches up to 1,000 public production Spot
  candles for the selected symbol/interval and compares a deterministic
  EMA/RSI technical-rule proxy with a same-notional buy-and-hold benchmark.
  Starting capital, order size, drawdown stop, exchange-exit percentages,
  taker-fee estimate, and slippage are editable. It sends no orders and does
  not replay historical Ollama decisions; results omit order-book depth,
  partial fills, and exact historical commissions. If one candle touches both
  exits, the simulator assumes the stop-loss came first. Results are not a
  profitability forecast.
- Primary account/trading controls stay in a top-level toolbar above the tabs
  and the default window size adapts to the available screen.
- Connection settings and activity logs share one **Connection & Activity**
  tab.
- The dashboard is vertically scrollable (mouse wheel or the scrollbar), so
  the chart, account balances, and open bot-managed positions remain reachable
  on shorter displays.
- Dashboard status indicators animate subtly; activity log can be cleared or
  copied. Chart/account refreshes are data-only operations and never submit
  orders.
- With **Live prices · ~1 sec** enabled, one public Binance batched ticker
  request approximately once per second refreshes the selected chart price,
  current candle shape, scanner prices, and bot-managed unrealized P/L. Actual
  update frequency depends on network/API response time. This does not poll
  account balances or trade history every second; refresh those separately.
- Live quotes update display only. AI entries and signals still evaluate on
  newly closed candles at the selected interval; the local model is not called
  once per second. When attached, Binance-hosted OCO stop/target orders can
  trigger independently of the desktop app, but fills can slip and are not
  guaranteed profit.
- Analysis/trading steps show an indeterminate activity animation and a
  stage-based dashboard progress bar. Model-reported confidence is displayed
  separately and is not a true probability or calibrated progress estimate.
- Dashboard lists bot-managed open positions with estimated gross unrealized
  P/L (current market price × tracked quantity − tracked cost; fees are
  excluded). Exchange-exit realized P/L is estimated from the order-list
  cumulative quote totals; commissions may be incomplete. Account refresh
  updates all open positions; chart refresh updates a matching symbol using the
  latest closed candle.
- Dashboard can load up to 1,000 recent fills for the selected pair using
  Binance's signed, read-only trade-history endpoint and export the fills to
  CSV. FIFO realized P/L is an estimate limited to fetched history: sells with
  missing cost basis or fees paid in a third asset show as unavailable.
- Optional desktop popups with a system sound alert on BUY/SELL signals,
  executed fills, reconciled fills, and explicit automation risk stops. The
  **Desktop alerts** toolbar toggle turns them on or off; alerts never place or
  cancel orders.
- Fetches Binance Spot candles on the selected interval; computes EMA20, EMA50,
  and RSI14.
- Requests a BUY/SELL/HOLD opinion from a local Ollama model and validates the
  returned schema. Non-local AI endpoints are rejected. The first request after
  starting Ollama may take several minutes while the model loads; analysis allows
  up to ten minutes and reports a specific timeout if Ollama remains unresponsive.
- Supports Testnet market orders and a distinct Binance production Spot client.
- Automatic Spot mode refreshes the 20 highest-24-hour-quote-volume eligible
  USDT pairs every 15 minutes, excluding stablecoin bases and common leveraged
  token suffixes. It fetches up to 120 candles per pair, ranks trend/momentum/
  volume/RSI conditions, and asks local Ollama to assess only the highest-ranked
  technically eligible candidate. The ranking score is not a probability.
  Only one bot-managed position is allowed; while it is open, the scanner pauses
  new entries and the bot manages that position's exits. The universe is based
  on the currently selected Binance Spot environment, so Testnet availability
  and activity can differ from production.
- Scanning is candle-based, not tick-by-tick: the fastest configured interval
  is one minute, and API/model latency adds further delay. No scanner or AI
  decision can guarantee a profitable fill or prevent losses.
- Binance Margin and Futures order execution are not implemented. The app must
  not be used for live derivatives trading; separate market-specific paper
  models, risk controls, and explicit authorization are required before adding
  those products.
- A model SELL signal with no bot-managed position is converted to HOLD; the
  bot sends no sell order and continues its normal cycle.
- Automatic analysis timeframe is configurable independently of the chart
  (1m, 5m, 15m, 1h, or 4h). The bot analyzes only newly closed candles and
  schedules its next cycle for the next candle boundary. Stop-loss and
  take-profit OCO percentages are separately configurable. Shorter timeframes
  do not imply faster inference or profitable trades; model latency, fees,
  slippage, and market risk remain.
- Starts automation off. Testnet automation requires confirmation; LIVE
  automation requires a warning confirmation plus typing `LIVE` each run.
- LIVE trades require reading and Binance's Spot/Margin trading permission,
  withdrawals disabled, and API-key IP restrictions enabled. The app invokes
  Spot endpoints only; Binance may group Spot and Margin access in one API-key
  permission, so the app cannot certify that the key is Spot-only.
- LIVE mode requires user-entered maximum buy size and maximum intraday
  peak-to-current equity drawdown values in USDT; these risk fields are
  deliberately blank when switching to LIVE.
- Stop-loss and take-profit percentages are editable. After a market buy fills,
  the app submits a Binance Spot OCO sell list using market-triggered
  `STOP_LOSS` and `TAKE_PROFIT` legs, with prices derived from the tracked
  average entry. Defaults are examples, not financial advice. The chart/table
  show the exchange list only as **active** after Binance confirms it.
- Protective-order state and cumulative exit fills are persisted. Ambiguous
  submissions, failed verification, partial fills, or a missing exit list halt
  automated trading and are displayed as unprotected/unknown/partial rather
  than being presented as safe. On account connection the app re-checks the
  recorded exit list. **Verify exits**, **Protect open position**, and
  **Close managed position** provide explicit, confirmation-gated recovery
  actions; closing first cancels by Binance's numeric order-list ID and verifies
  the bot-managed exit list before sending any market sell. Since Binance's
  order-list query returns child-order identifiers, the app separately queries
  both child orders to reconcile their live status and cumulative fills.
- The bot's Pause control stops future automation cycles but intentionally
  leaves exchange-managed exits working on Binance. Review/cancel those orders
  separately if you do not want them to remain active.
- Before LIVE orders, values all nonzero free and locked Spot balances in USDT.
  An asset that cannot be valued blocks trading. It checks account-equity
  drawdown from the persisted UTC-day high-water mark; the dashboard separately
  shows loss from the day-start baseline and drawdown from the high-water mark.
- At the daily loss limit, automation stops and blocks further orders. It
  **does not automatically sell** an open position; the exchange-managed
  position exits remain separate from that account-wide stop.
- Buy amounts cannot exceed free USDT or the entered per-trade cap. Only one
  bot-managed position is allowed at a time; sells are limited to that tracked
  position.
- Persists order intent before submit and blocks further orders on ambiguous
  outcomes until the exchange order is reconciled.
- Separates Testnet and LIVE ledgers. LIVE ledger is tied to a SHA-256
  fingerprint of the API key (the API key/secret themselves are not stored).
  Changing the live API key requires resolving the ledger/account association
  first.
- API credentials are never sent to Ollama. When **Remember last connected
  keys** is selected (default), the last successfully connected API credentials
  are encrypted with Windows DPAPI for the current Windows user and stored
  under `%LOCALAPPDATA%\AstraAITrader\credentials.dpapi`. They are decrypted
  only in the app process. Uncheck the option or select **Forget saved keys**
  to remove the saved copy. This protects the file at rest but does not protect
  credentials from malware or a compromised logged-in Windows account.
- Risk/trade ledgers and the single-instance lock are stored under
  `%LOCALAPPDATA%\AstraAITrader`, outside the executable. On first startup, an
  existing source-folder ledger is copied there only when no user-data ledger
  exists, preserving its risk state.
- The **Binance API key setup** button opens Binance's API-management page after
  explaining key setup. **Connect Binance account** validates the selected key
  and shows account type/trading status, permissions returned by Binance, and
  nonzero free/locked asset balances. A read-only key can be used to inspect
  account data; LIVE order automation additionally requires the trading
  permission. Failed API-key checks identify the required flag and the value
  Binance returned without showing allowed IP addresses. Private
  identity/profile fields not exposed by the Spot account endpoint are not
  available. There is no Binance website password login or OAuth flow.

Risk controls reduce certain operational hazards; they cannot guarantee
execution price, prevent all losses, protect against outages or every exchange
failure, or provide a reliable account-equity valuation for unsupported assets.
Binance trigger orders do not guarantee their execution price, may incur
slippage, and can fail/reject; they are not equivalent to a guaranteed stop.
The daily account-equity stop is checked during the five-minute automation
cycle and again immediately before an order, not continuously between checks.
Exchange-managed exits are independent of that account-wide stop.

## Requirements

- Windows 10/11
- Binance account/API credentials authorized for the selected environment

When using the installer, Python/Tkinter are bundled. The installer can install
Ollama and download the default model on first setup. Running from source
instead requires Python 3.10+ with Tcl/Tk and a local Ollama service/model.

## Build a single-file Windows executable

On Windows, install PyInstaller and run:

```powershell
python -m pip install --upgrade pyinstaller
python -m PyInstaller --clean --noconfirm --onefile --windowed --name AstraAITrader `
  --distpath dist_latest --workpath build_latest --specpath build_latest app.py
```

The refreshed distributable is `dist_latest\AstraAITrader.exe`. Python and
Tkinter are bundled. For a guided app + Ollama installation, build the installer
below. API credentials, ledgers, and other per-user state are not embedded in
the executable.

## Build the Windows installer

Install the official Inno Setup 7 compiler, build the executable as above, then
compile `installer\AstraAITrader.iss` with `ISCC.exe`. For example:

```powershell
ISCC.exe /DAppBinaryDir=..\dist_latest /FAstraAITrader-Setup-Updated installer\AstraAITrader.iss
```

The refreshed setup package is written to
`release\AstraAITrader-Setup-Updated.exe`. It installs
the app for the current Windows user and offers to install Ollama and fetch
`qwen2.5:3b` on the finish step. The AI setup downloads Ollama's installer over
HTTPS, checks its Authenticode signature, and shows the official Ollama setup
prompts if it is not already installed. The model download requires internet
access and approximately 2 GB of disk space. A Start Menu shortcut lets you
retry or repair the Ollama/model setup later. Ollama and its model are separate
from the installer package; the setup EXE remains a single installable file.
User credentials and ledgers stay under `%LOCALAPPDATA%\AstraAITrader` and are
not packaged. Keep the installer on a trusted machine and continue to apply
the Binance API-key restrictions documented above. The installer is not
code-signed, so Windows may show an unknown-publisher/SmartScreen warning.

Start Ollama locally with Docker:

```powershell
docker compose --profile ai up -d ollama
docker compose exec ollama ollama pull qwen2.5:3b
```

This downloads a model and consumes disk space. The Ollama port is published
on `127.0.0.1` only. Alternatively, install Ollama natively and run:

```powershell
ollama pull qwen2.5:3b
```

Start the desktop app natively:

```powershell
python .\app.py
```

### Testnet account setup

Create separate Spot Testnet credentials at:

```text
https://testnet.binance.vision/
```

Choose `TESTNET`, enter only those credentials in the Testnet fields, and use
**Connect Binance account** and **Analyze only** before enabling simulated
orders.

### Live account setup

Use the LIVE credential fields only for a Binance production API key that you
are authorized to use. Create a dedicated key for this app. Enable reading and
the trading permission required by Binance, disable withdrawals, and restrict
the key to your trusted IP address. Never enable withdrawals for this app. If
Binance does not report the required reading/trading/withdrawal/IP settings as
expected, the app refuses LIVE automation. Binance's API may represent Spot and
Margin trading in a combined permission; the app itself only calls Spot
endpoints, but cannot guarantee that the key lacks margin access elsewhere.

Select `LIVE`, enter your LIVE credentials in the desktop app (never in chat),
use **Connect Binance account** to validate the key and inspect account data,
set your per-buy and daily account-equity loss limits in USDT, and inspect
analysis before arming automation. A read-only key can connect for profile and
balance inspection, but cannot place orders. Limits are user-configured, but
valid finite positive limits and the app's other safety gates are required;
they cannot be disabled for LIVE orders. The app requires a second confirmation
and the exact word `LIVE` each run. Use amounts you can afford to lose; a daily
stop leaves an open position untouched. A loss limit that is blank, invalid,
or at least the observed starting equity prevents LIVE automation. Credentials
can be remembered using Windows DPAPI for the current Windows account, or the
option can be disabled so they must be entered again after restarting.

The app cannot verify whether Binance services are available to you by
jurisdiction. It does not use withdrawal, margin, futures, or transfer APIs.
It does not accept or store your Binance website password and does not provide
Binance OAuth login; the supported account connection is the API key you
create on Binance and enter locally.

## Tests

Run on Windows:

```powershell
python -m unittest discover -v -s . -p "test_*.py"
```

Or build and run the headless suite in Docker:

```powershell
docker compose run --build --rm tests
```

Tests mock signed account and order responses; Docker does not submit live or
Testnet orders. The desktop window and Ollama run natively/locally, not inside
the test container.
