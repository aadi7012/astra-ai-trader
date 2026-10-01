from datetime import datetime, timezone
import math

from local_ai import calculate_indicators

MAX_BACKTEST_CANDLES = 1000
INDICATOR_WARMUP = 50


def validate_backtest_settings(
    starting_cash,
    quote_per_trade,
    daily_drawdown_stop,
    stop_loss_pct,
    take_profit_pct,
    fee_bps,
    slippage_bps,
):
    values = {
        "starting_cash": starting_cash,
        "quote_per_trade": quote_per_trade,
        "daily_drawdown_stop": daily_drawdown_stop,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
    }
    parsed = {}
    for name, value in values.items():
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name.replace('_', ' ').title()} must be a number.") from error
        if isinstance(value, bool) or not math.isfinite(number):
            raise ValueError(f"{name.replace('_', ' ').title()} must be finite.")
        parsed[name] = number

    if parsed["starting_cash"] <= 0:
        raise ValueError("Starting cash must be positive.")
    if parsed["quote_per_trade"] <= 0 or parsed["quote_per_trade"] > parsed["starting_cash"]:
        raise ValueError("Trade size must be positive and no larger than starting cash.")
    if (
        parsed["daily_drawdown_stop"] <= 0
        or parsed["daily_drawdown_stop"] >= parsed["starting_cash"]
    ):
        raise ValueError("Daily drawdown stop must be positive and below starting cash.")
    if not 0 < parsed["stop_loss_pct"] < 100:
        raise ValueError("Stop-loss percentage must be between 0 and 100.")
    if parsed["take_profit_pct"] <= 0:
        raise ValueError("Take-profit percentage must be positive.")
    if not 0 <= parsed["fee_bps"] <= 1000:
        raise ValueError("Fee must be between 0 and 1,000 basis points.")
    if not 0 <= parsed["slippage_bps"] < 10000:
        raise ValueError("Slippage must be between 0 and 10,000 basis points.")
    fee_rate = parsed["fee_bps"] / 10000
    if parsed["quote_per_trade"] * (1 + fee_rate) > parsed["starting_cash"]:
        raise ValueError("Starting cash must cover one trade and its estimated entry fee.")
    return parsed


def _validated_candles(candles):
    if not isinstance(candles, list) or len(candles) <= INDICATOR_WARMUP:
        raise ValueError(
            f"At least {INDICATOR_WARMUP + 1} historical candles are required."
        )
    normalized = []
    previous_close_time = -1
    for row in candles[-MAX_BACKTEST_CANDLES:]:
        try:
            open_price = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            close_time = int(row["close_time"])
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError("Historical candle data is malformed.") from error
        if (
            any(not math.isfinite(value) or value <= 0 for value in (open_price, high, low, close))
            or high < max(open_price, close)
            or low > min(open_price, close)
            or close_time <= previous_close_time
        ):
            raise ValueError("Historical candle data contains invalid or unordered OHLC values.")
        previous_close_time = close_time
        normalized.append(
            {
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "close_time": close_time,
            }
        )
    return normalized


def run_backtest(
    candles,
    *,
    starting_cash=1000.0,
    quote_per_trade=10.0,
    daily_drawdown_stop=5.0,
    stop_loss_pct=2.0,
    take_profit_pct=4.0,
    fee_bps=10.0,
    slippage_bps=5.0,
):
    settings = validate_backtest_settings(
        starting_cash,
        quote_per_trade,
        daily_drawdown_stop,
        stop_loss_pct,
        take_profit_pct,
        fee_bps,
        slippage_bps,
    )
    rows = _validated_candles(candles)
    fee_rate = settings["fee_bps"] / 10000
    slippage_rate = settings["slippage_bps"] / 10000
    cash = settings["starting_cash"]
    position = None
    pending_action = None
    buys_halted = False
    daily_day = None
    daily_high_water = cash
    max_drawdown = 0.0
    equity_high_water = cash
    fees_paid = 0.0
    trades = []
    stop_exits = 0
    target_exits = 0
    signal_exits = 0
    period_end_exits = 0

    def close_position(price, exit_time, reason):
        nonlocal cash, position, fees_paid, stop_exits, target_exits
        nonlocal signal_exits, period_end_exits
        if position is None:
            raise RuntimeError("Backtest attempted to close a missing position.")
        gross = position["quantity"] * price
        fee = gross * fee_rate
        proceeds = gross - fee
        pnl = proceeds - position["cost"]
        cash += proceeds
        fees_paid += fee
        trades.append(
            {
                "entry_time": position["entry_time"],
                "exit_time": exit_time,
                "entry_price": position["entry_price"],
                "exit_price": price,
                "quantity": position["quantity"],
                "pnl_usdt": pnl,
                "exit_reason": reason,
            }
        )
        if reason == "stop-loss":
            stop_exits += 1
        elif reason == "take-profit":
            target_exits += 1
        elif reason == "signal":
            signal_exits += 1
        elif reason == "period-end":
            period_end_exits += 1
        position = None

    for index in range(INDICATOR_WARMUP, len(rows)):
        bar = rows[index]
        close_time = bar["close_time"]
        date = datetime.fromtimestamp(close_time / 1000, timezone.utc).date()
        traded_this_bar = False

        if pending_action == "BUY" and position is None and not buys_halted:
            fill_price = bar["open"] * (1 + slippage_rate)
            entry_fee = settings["quote_per_trade"] * fee_rate
            total_cost = settings["quote_per_trade"] + entry_fee
            if cash >= total_cost:
                position = {
                    "quantity": settings["quote_per_trade"] / fill_price,
                    "entry_price": fill_price,
                    "entry_time": close_time,
                    "cost": total_cost,
                    "stop_price": fill_price * (1 - settings["stop_loss_pct"] / 100),
                    "target_price": fill_price * (1 + settings["take_profit_pct"] / 100),
                }
                cash -= total_cost
                fees_paid += entry_fee
                traded_this_bar = True
            pending_action = None
        elif pending_action == "SELL" and position is not None:
            close_position(
                bar["open"] * (1 - slippage_rate),
                close_time,
                "signal",
            )
            traded_this_bar = True
            pending_action = None
        else:
            pending_action = None

        if position is not None:
            stop_hit = bar["low"] <= position["stop_price"]
            target_hit = bar["high"] >= position["target_price"]
            if stop_hit:
                raw_price = (
                    min(bar["open"], position["stop_price"])
                    if bar["open"] <= position["stop_price"]
                    else position["stop_price"]
                )
                close_position(
                    raw_price * (1 - slippage_rate), close_time, "stop-loss"
                )
                traded_this_bar = True
            elif target_hit:
                raw_price = (
                    max(bar["open"], position["target_price"])
                    if bar["open"] >= position["target_price"]
                    else position["target_price"]
                )
                close_position(
                    raw_price * (1 - slippage_rate), close_time, "take-profit"
                )
                traded_this_bar = True

        equity = cash + (
            position["quantity"] * bar["close"] if position is not None else 0.0
        )
        if date != daily_day:
            daily_day = date
            daily_high_water = equity
        else:
            daily_high_water = max(daily_high_water, equity)
        if daily_high_water - equity >= settings["daily_drawdown_stop"]:
            buys_halted = True
            pending_action = None
        equity_high_water = max(equity_high_water, equity)
        max_drawdown = max(max_drawdown, equity_high_water - equity)

        if index == len(rows) - 1 or traded_this_bar or buys_halted:
            continue
        indicators = calculate_indicators(rows[: index + 1])
        uptrend = indicators["ema20"] > indicators["ema50"]
        rsi = indicators["rsi14"]
        if position is None:
            if uptrend and 40 <= rsi <= 68:
                pending_action = "BUY"
        elif (uptrend and rsi >= 70) or (not uptrend and rsi > 30):
            pending_action = "SELL"

    last_close = rows[-1]["close"]
    if position is not None:
        close_position(
            last_close * (1 - slippage_rate),
            rows[-1]["close_time"],
            "period-end",
        )
    final_equity = cash
    equity_high_water = max(equity_high_water, final_equity)
    max_drawdown = max(max_drawdown, equity_high_water - final_equity)

    first_open = rows[INDICATOR_WARMUP]["open"]
    benchmark_entry = first_open * (1 + slippage_rate)
    benchmark_fee = settings["quote_per_trade"] * fee_rate
    benchmark_quantity = settings["quote_per_trade"] / benchmark_entry
    benchmark_cash = settings["starting_cash"] - settings["quote_per_trade"] - benchmark_fee
    benchmark_exit = rows[-1]["close"] * (1 - slippage_rate)
    benchmark_proceeds = benchmark_quantity * benchmark_exit
    benchmark_exit_fee = benchmark_proceeds * fee_rate
    benchmark_equity = benchmark_cash + benchmark_proceeds - benchmark_exit_fee
    wins = sum(1 for trade in trades if trade["pnl_usdt"] > 0)
    return {
        "starting_cash_usdt": settings["starting_cash"],
        "final_equity_usdt": final_equity,
        "return_pct": (final_equity / settings["starting_cash"] - 1) * 100,
        "max_drawdown_usdt": max_drawdown,
        "max_drawdown_pct": max_drawdown / equity_high_water * 100
        if equity_high_water > 0
        else 0.0,
        "closed_trades": len(trades),
        "win_rate_pct": wins / len(trades) * 100 if trades else 0.0,
        "fees_paid_usdt": fees_paid,
        "buy_hold_fees_paid_usdt": benchmark_fee + benchmark_exit_fee,
        "stop_exits": stop_exits,
        "take_profit_exits": target_exits,
        "signal_exits": signal_exits,
        "period_end_exits": period_end_exits,
        "daily_drawdown_halted": buys_halted,
        "buy_hold_equity_usdt": benchmark_equity,
        "buy_hold_return_pct": (benchmark_equity / settings["starting_cash"] - 1) * 100,
        "candles": len(rows),
        "start_close_time": rows[0]["close_time"],
        "end_close_time": rows[-1]["close_time"],
        "strategy": "EMA20/EMA50 + RSI technical proxy (not historical Ollama decisions)",
        "trades": trades,
    }
