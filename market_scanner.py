import math
import time

from binance_testnet import BinanceTestnetError
from local_ai import calculate_indicators


MAX_SCAN_SYMBOLS = 20
UNIVERSE_REFRESH_SECONDS = 15 * 60
EXCLUDED_BASE_ASSETS = frozenset(
    {"BUSD", "DAI", "FDUSD", "TUSD", "USDC", "USDP", "USDT"}
)
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S", "5L", "5S")


class MarketScannerError(RuntimeError):
    pass


def _finite_nonnegative(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise MarketScannerError(f"Binance returned an invalid {label}.") from error
    if not math.isfinite(number) or number < 0:
        raise MarketScannerError(f"Binance returned an invalid {label}.")
    return number


def select_top_usdt_spot_symbols(exchange_info, ticker_rows, limit=MAX_SCAN_SYMBOLS):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Market-scan limit must be an integer from 1 to 100.")
    if not isinstance(exchange_info, dict) or not isinstance(
        exchange_info.get("symbols"), list
    ):
        raise MarketScannerError("Binance returned invalid Spot exchange information.")
    if not isinstance(ticker_rows, list):
        raise MarketScannerError("Binance returned invalid 24-hour market statistics.")

    ticker_volumes = {}
    for row in ticker_rows:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
            continue
        try:
            ticker_volumes[row["symbol"]] = _finite_nonnegative(
                row.get("quoteVolume"), f"{row['symbol']} 24-hour quote volume"
            )
        except MarketScannerError:
            continue

    allowed_symbols = {}
    for item in exchange_info["symbols"]:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        base_asset = item.get("baseAsset")
        if (
            item.get("status") != "TRADING"
            or item.get("quoteAsset") != "USDT"
            or item.get("isSpotTradingAllowed") is not True
            or not isinstance(symbol, str)
            or not isinstance(base_asset, str)
            or base_asset in EXCLUDED_BASE_ASSETS
            or base_asset.endswith(LEVERAGED_SUFFIXES)
        ):
            continue
        quote_volume = ticker_volumes.get(symbol, 0)
        if quote_volume > 0:
            allowed_symbols[symbol] = quote_volume

    ranked = sorted(allowed_symbols.items(), key=lambda item: (-item[1], item[0]))
    return ranked[:limit]


def score_spot_candidate(symbol, candles, quote_volume):
    closed = candles
    if not isinstance(closed, list) or len(closed) < 50:
        raise ValueError("At least 50 closed candles are required.")
    indicators = calculate_indicators(closed)
    volumes = []
    for candle in closed:
        volume = _finite_nonnegative(candle.get("volume"), f"{symbol} candle volume")
        volumes.append(volume)

    price = indicators["last_price"]
    ema20 = indicators["ema20"]
    ema50 = indicators["ema50"]
    rsi = indicators["rsi14"]
    momentum = (price / float(closed[-4]["close"])) - 1
    volume_baseline = sum(volumes[-23:-3]) / 20
    recent_volume = sum(volumes[-3:]) / 3
    if not math.isfinite(volume_baseline) or not math.isfinite(recent_volume):
        raise ValueError(f"{symbol} candle volume totals are invalid.")
    volume_ratio = recent_volume / volume_baseline if volume_baseline > 0 else 0
    trend_pct = (ema20 / ema50) - 1
    eligible = (
        price > ema20 > ema50
        and 40 <= rsi <= 68
        and momentum > 0
    )
    reasons = []
    if price <= ema20 or ema20 <= ema50:
        reasons.append("trend filter")
    if not 40 <= rsi <= 68:
        reasons.append("RSI filter")
    if momentum <= 0:
        reasons.append("momentum filter")

    trend_score = min(30.0, max(0.0, trend_pct * 1000))
    momentum_score = min(30.0, max(0.0, momentum * 1000))
    volume_score = min(20.0, max(0.0, (volume_ratio - 1) * 20))
    rsi_score = min(20.0, max(0.0, 20 - abs(rsi - 54)))
    return {
        "symbol": symbol,
        "quote_volume": _finite_nonnegative(quote_volume, f"{symbol} 24-hour quote volume"),
        "score": round(trend_score + momentum_score + volume_score + rsi_score, 2),
        "rsi": round(rsi, 2),
        "momentum": momentum,
        "status": "CANDIDATE" if eligible else "Filtered: " + ", ".join(reasons),
        "eligible": eligible,
        "indicators": indicators,
    }


class SpotMarketScanner:
    def __init__(self, client, limit=MAX_SCAN_SYMBOLS):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Market-scan limit must be an integer from 1 to 100.")
        self.client = client
        self.limit = limit
        self._cached_universe = []
        self._universe_expires_at = 0

    def _universe(self):
        now = time.monotonic()
        if now >= self._universe_expires_at:
            exchange_info = self.client.spot_exchange_info()
            ticker_rows = self.client.ticker_24hr()
            self._cached_universe = select_top_usdt_spot_symbols(
                exchange_info, ticker_rows, self.limit
            )
            if not self._cached_universe:
                raise MarketScannerError(
                    "No eligible, liquid USDT Spot pairs were returned by Binance."
                )
            self._universe_expires_at = now + UNIVERSE_REFRESH_SECONDS
        return self._cached_universe

    def scan(self, interval, now_ms=None):
        symbols = self._universe()
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        rows = []
        errors = []
        for symbol, quote_volume in symbols:
            try:
                candles = self.client.klines(symbol, interval=interval, limit=120)
                closed = [
                    candle
                    for candle in candles
                    if candle.get("close_time", now_ms) < now_ms
                ]
                candidate = score_spot_candidate(symbol, closed, quote_volume)
                rows.append(candidate)
            except (
                BinanceTestnetError,
                KeyError,
                IndexError,
                TypeError,
                ValueError,
                OverflowError,
            ) as error:
                errors.append(f"{symbol}: {error}")
                rows.append(
                    {
                        "symbol": symbol,
                        "quote_volume": quote_volume,
                        "score": None,
                        "rsi": None,
                        "momentum": None,
                        "status": f"Data unavailable: {error}"[:100],
                        "eligible": False,
                        "indicators": None,
                    }
                )

        if rows and len(errors) == len(rows):
            details = "; ".join(errors[:3])
            raise MarketScannerError(
                f"Could not fetch valid candle data for any scanned pair: {details}"
            )
        rows.sort(
            key=lambda row: (
                not row["eligible"],
                -(row["score"] or 0),
                -row["quote_volume"],
                row["symbol"],
            )
        )
        candidates = [row for row in rows if row["eligible"]]
        return {
            "markets": rows,
            "candidate": candidates[0] if candidates else None,
            "errors": errors,
            "status": (
                f"Top {len(rows)} liquid USDT Spot pairs scanned; "
                f"{len(candidates)} passed the technical filters."
            ),
        }
