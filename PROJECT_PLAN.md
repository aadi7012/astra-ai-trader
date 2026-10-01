# Product and phased delivery

## Product

A standalone Windows desktop application for AI-assisted Binance Spot market
analysis and user-authorized automated trading across a bounded liquid-pair
universe. Credentials must be authorized
by the exchange/account owner; the app must never bypass exchange protections
or imply that profit is assured.

## Phase 1 — Testnet execution foundation

**Implemented:** separate pinned Binance Spot Testnet client, public candle
retrieval, signed account/order methods, local Ollama analysis, signal/risk
checks, explicit automation confirmation, managed-position ledger, ambiguous
order reconciliation, and headless Docker tests.

**Verified:** public Testnet candle reads; mocked signed requests; mocked
order/fill and failure paths; unit tests and native UI initialization. Local
Ollama service, configured model inference, and the app's response validation
were also verified. The dashboard has animated process/prediction stages,
active bot-position P/L estimates, balance export, and Windows DPAPI key
storage. It also supports read-only Binance fill history with bounded FIFO
realized-P/L estimates and optional desktop alerts for signals, fills, and risk
stops.

**Not verified:** signed account access or real Testnet orders with user
credentials; a fresh native Ollama installation and model download through the
Windows first-run installer.

**Windows packaging:** a single-file Inno Setup installer now bundles the
desktop app and a first-run bootstrap that verifies the official Ollama
installer signature, installs Ollama if needed, and downloads the default
model. The installer itself was tested by installing and uninstalling in an
isolated directory; model setup was exercised against the already-running
local Ollama service.

## Phase 2 — Live Spot path (implemented, not account-verified)

- Separate hard-coded Binance production Spot endpoint and credential fields.
- LIVE API restrictions required: read and Binance's Spot/Margin trading
  permission enabled, withdrawals disabled, IP restriction enabled. The app
  only calls Spot endpoints; Binance may combine Spot and Margin permissions.
- Separate persisted ledger and account-key fingerprint.
- Per-buy and daily total account-equity loss inputs are blank until the user
  configures them. Bot values all nonzero free/locked assets in USDT and fails
  closed on unpriced assets.
- Equity baseline and intraday high-water drawdown persist per UTC day.
- When the limit is reached, automation halts and sends no further orders;
  managed positions are not force-sold.
- LIVE automation needs a warning confirmation and typed `LIVE` each run.
- Configurable exchange-managed OCO stop-loss/take-profit exits are requested
  after a buy fill. Their status and cumulative fills persist in ledger schema
  v2; unresolved states block automation. Manual position close verifies OCO
  cancellation before sending its market sell.
- Automatic Spot mode scans the 20 highest-24-hour-volume eligible USDT pairs,
  applies closed-candle trend/momentum/volume/RSI filters, and asks local AI to
  review only the top technical candidate. The universe refreshes every
  15 minutes; only one managed position is allowed.
- Scanning is candle-based (one-minute minimum interval), not tick-by-tick.
  Technical scores and model-reported confidence are not calibrated
  probabilities and do not establish expected profit.
- Dashboard displays account-equity estimates, daily loss/drawdown for LIVE,
  per-position exchange-exit status and gross unrealized P/L. Pausing the bot
  leaves already-submitted exchange exits active.

**Acceptance still outstanding:** no live account credentials or permission
response were supplied, no live account was connected, and no production order
was sent or tested. The new multi-pair scanner has unit coverage but has not
been exercised against the selected Binance environment. Binance OCO calls and state recovery are covered by mocked
unit tests, but exchange behavior has not been verified on Testnet or LIVE.
The desktop now includes API-key setup guidance and a connect-and-inspect-
account flow, but account connection still requires the user to create and
enter their own key locally. A live account-equity figure depends on supported
USDT conversion pairs and is checked every five-minute cycle and again before
an order. Triggered market exits can slip and are not guaranteed fills. The
strategy has no measured profitability claim. Failed key checks now report
which required restriction flag did not match without exposing IP values.

## Phase 3 — Evidence and operational hardening (not complete)

- A simulation-only backtest tool is implemented for up to 1,000 public candles.
  It compares a deterministic EMA/RSI proxy (not historical Ollama decisions)
  with buy-and-hold and exposes fee, slippage, exit, and risk assumptions.
- Improve replay with partial fills, a wider range of market regimes, and a
  better model of exchange filters/order-book behavior before relying on results.
- Run paper trading and compare outcomes against non-AI baselines before
  depending on any automated strategy.
- Add integration tests with Binance Testnet, GUI workflow tests, installer
  build/signing, structured audit export, and restart/outage/rate-limit tests.
- Independently review risk controls and recovery behavior before relying on
  this with real funds.
- Build separate margin and futures paper-trading models before considering any
  derivatives order support. Those products are not implemented or enabled.

**Acceptance:** reproducible evidence and tests across strategy and exchange
failure modes, verified by a person with authorized account access. Passing
software tests does not establish profitability or guarantee safety.
