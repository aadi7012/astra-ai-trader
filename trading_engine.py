from datetime import datetime, timezone
import math
import time
import uuid
from decimal import Decimal

from binance_permissions import validate_live_api_restrictions
from binance_testnet import (
    BinanceSpotTestnet,
    BinanceTestnetError,
    normalize_market_sell_quantity,
)
from local_ai import OllamaAnalyzer, calculate_indicators
from trade_ledger import TradeLedger

MIN_CONFIDENCE = 0.80
TRADE_INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
}


class TradingEngine:
    def __init__(
        self,
        client: BinanceSpotTestnet,
        analyzer: OllamaAnalyzer,
        ledger: TradeLedger,
        symbol="BTCUSDT",
        max_trade_quote=10.0,
        daily_loss_limit=5.0,
        stop_loss_pct=2.0,
        take_profit_pct=4.0,
        interval="15m",
    ):
        self.client = client
        self.analyzer = analyzer
        self.ledger = ledger
        self.symbol = client.validate_symbol(symbol)
        self.max_trade_quote = float(max_trade_quote)
        self.daily_loss_limit = float(daily_loss_limit)
        self.stop_loss_pct = float(stop_loss_pct)
        self.take_profit_pct = float(take_profit_pct)
        if interval not in TRADE_INTERVAL_SECONDS:
            raise ValueError("Select a supported trading candle interval.")
        self.interval = interval
        self.last_analyzed_close_time = None
        if (
            not math.isfinite(self.max_trade_quote)
            or self.max_trade_quote <= 0
            or not math.isfinite(self.daily_loss_limit)
            or self.daily_loss_limit <= 0
        ):
            raise ValueError("Order and daily loss limits must be finite positive USDT amounts.")
        if (
            not math.isfinite(self.stop_loss_pct)
            or not 0 < self.stop_loss_pct < 100
            or not math.isfinite(self.take_profit_pct)
            or self.take_profit_pct <= 0
        ):
            raise ValueError(
                "Protective stop-loss must be between 0 and 100%; take-profit must be positive."
            )

    def seconds_until_next_candle(self):
        interval_seconds = TRADE_INTERVAL_SECONDS[self.interval]
        return max(1, int(interval_seconds - (time.time() % interval_seconds)) + 1)

    @staticmethod
    def _balance(account, asset):
        for row in account.get("balances", []):
            if row.get("asset") == asset:
                balance = float(row.get("free", 0))
                if not math.isfinite(balance) or balance < 0:
                    raise ValueError(f"Binance returned an invalid free balance for {asset}.")
                return balance
        return 0.0

    def _technical_guard(self, action, indicators, position_quantity):
        rsi = indicators["rsi14"]
        uptrend = indicators["ema20"] > indicators["ema50"]
        if action == "BUY":
            if not uptrend:
                return "Buy blocked: EMA20 is not above EMA50."
            if not 40 <= rsi <= 68:
                return "Buy blocked: RSI14 is outside the configured 40–68 band."
            if position_quantity > 0:
                return "Buy blocked: a bot-managed position is already open."
        elif action == "SELL":
            if uptrend and rsi < 70:
                return "Sell blocked: no configured exit condition is present."
            if not uptrend and rsi <= 30:
                return "Sell blocked: oversold market; wait rather than sell into weakness."
        return None

    @staticmethod
    def _parse_order_list(
        response, symbol, client_order_id, expected_order_list_id=None
    ):
        if (
            not isinstance(response, dict)
            or response.get("symbol") != symbol
            or response.get("listClientOrderId") != client_order_id
            or (
                (response.get("listStatusType"), response.get("listOrderStatus"))
                not in (
                    ("EXEC_STARTED", "EXECUTING"),
                    ("ALL_DONE", "ALL_DONE"),
                    ("REJECT", "ALL_DONE"),
                )
            )
            or isinstance(response.get("orderListId"), bool)
            or not isinstance(response.get("orderListId"), int)
            or response.get("orderListId") < 0
            or (
                expected_order_list_id is not None
                and response.get("orderListId") != expected_order_list_id
            )
            or not isinstance(response.get("orders"), list)
            or len(response["orders"]) != 2
            or not isinstance(response.get("orderReports"), list)
            or len(response["orderReports"]) != 2
        ):
            raise RuntimeError(
                "Binance returned an invalid exit-list status; the position is not considered protected."
            )
        if any(
            not isinstance(order, dict)
            or order.get("symbol") != symbol
            or not isinstance(order.get("orderId"), int)
            or not isinstance(order.get("clientOrderId"), str)
            for order in response["orders"]
        ):
            raise RuntimeError("Binance returned malformed protective-exit order identifiers.")
        order_client_ids = {order["clientOrderId"] for order in response["orders"]}
        report_client_ids = set()
        for report in response["orderReports"]:
            if (
                not isinstance(report, dict)
                or report.get("symbol") != symbol
                or report.get("side") != "SELL"
                or not isinstance(report.get("clientOrderId"), str)
            ):
                raise RuntimeError("Binance returned malformed protective-exit order details.")
            report_client_ids.add(report["clientOrderId"])
        if len(order_client_ids) != 2 or report_client_ids != order_client_ids:
            raise RuntimeError(
                "Binance returned child-order details that do not match the exit list."
            )
        return response

    def _apply_order_list_fills(self, response):
        symbol = self.symbol
        position = self.ledger.position(symbol)
        for report in response["orderReports"]:
            try:
                executed = float(report.get("executedQty", 0))
                quote = float(report.get("cummulativeQuoteQty", 0))
            except (TypeError, ValueError) as error:
                raise RuntimeError("Binance returned invalid exit fill totals.") from error
            if not math.isfinite(executed) or not math.isfinite(quote) or executed < 0 or quote < 0:
                raise RuntimeError("Binance returned invalid exit fill totals.")
            if executed > 0:
                self.ledger.record_protection_report(
                    symbol,
                    report["clientOrderId"],
                    executed,
                    quote,
                )
                position = self.ledger.position(symbol)
        return position

    def _reconcile_protection(self):
        position = self.ledger.position(self.symbol)
        if position["quantity"] <= 0:
            return None
        protection = position.get("protection", {"status": "unprotected"})
        status = protection.get("status")
        if status == "unprotected":
            raise RuntimeError(
                "This bot-managed position has no confirmed Binance protective exits. "
                "Use Protect open position or Close managed position before restarting automation."
            )
        client_id = protection.get("client_order_id")
        if not client_id:
            self.ledger.set_protection(
                self.symbol, {**protection, "status": "unknown"}
            )
            raise RuntimeError(
                "Protective-exit state has no exchange identifier; verify Binance order history manually."
            )
        try:
            response = self.client.order_list_status(client_id)
        except Exception as error:
            self.ledger.set_protection(
                self.symbol, {**protection, "status": "unknown"}
            )
            raise RuntimeError(
                f"Could not verify Binance protective exits ({error}). Automation is halted."
            ) from error
        try:
            response = self._parse_order_list(
                response, self.symbol, client_id, protection.get("order_list_id")
            )
            position = self._apply_order_list_fills(response)
        except Exception as error:
            current = self.ledger.position(self.symbol).get("protection", protection)
            self.ledger.set_protection(
                self.symbol, {**current, "status": "unknown"}
            )
            raise RuntimeError(
                f"Binance exit state could not be safely reconciled ({error}); automation is halted."
            ) from error
        if response["listStatusType"] == "REJECT":
            self.ledger.set_protection(
                self.symbol, {**protection, "status": "unprotected"}
            )
            raise RuntimeError(
                "Binance reports the exit list was rejected; the tracked position is unprotected."
            )
        if response["listStatusType"] == "EXEC_STARTED":
            protection = self.ledger.position(self.symbol).get("protection", protection)
            any_fills = any(
                float(report.get("executedQty", 0)) > 0
                for report in response["orderReports"]
            )
            protection["status"] = "partial" if any_fills else "active"
            self.ledger.set_protection(self.symbol, protection)
            if any_fills:
                raise RuntimeError(
                    "A Binance protective exit has partially filled. The ledger was updated; "
                    "automation is halted for manual review."
                )
            return None

        if response["listStatusType"] != "ALL_DONE":
            raise RuntimeError(
                "Binance protective exits were rejected or ended unexpectedly; automation is halted."
            )
        remaining = self.ledger.position(self.symbol)["quantity"]
        if remaining <= 1e-10:
            self.ledger.clear_protection(self.symbol)
            raise RuntimeError(
                "A Binance protective exit filled and closed the tracked position. Automation is halted."
            )
        self.ledger.set_protection(
            self.symbol,
            {
                **self.ledger.position(self.symbol).get("protection", protection),
                "status": "unprotected",
            },
        )
        raise RuntimeError(
            "The Binance exit list ended with some tracked quantity still open. "
            "Exchange protection is no longer active; automation is halted."
        )

    def _assert_no_open_symbol_orders(self, symbol):
        order_lists = self.client.open_order_lists()
        open_orders = self.client.open_orders(symbol)
        if not isinstance(order_lists, list) or not isinstance(open_orders, list):
            raise RuntimeError("Binance returned invalid open-order data; refusing to change exposure.")
        symbol_lists = [
            item
            for item in order_lists
            if isinstance(item, dict) and item.get("symbol") == symbol
        ]
        if symbol_lists or open_orders:
            raise RuntimeError(
                f"Open Binance order(s) exist for {symbol}. Review or cancel them on Binance "
                "before changing the bot-managed position."
            )

    def _submit_protection(self):
        position = self.ledger.position(self.symbol)
        if position["quantity"] <= 0:
            raise RuntimeError("There is no bot-managed position to protect.")
        rules = self.client.symbol_rules(self.symbol)
        if rules["quote_asset"] != "USDT":
            raise RuntimeError("Exchange-managed exits are supported only for USDT-quoted pairs.")
        account = self.client.account()
        free_base = self._balance(account, rules["base_asset"])
        if free_base + 1e-10 < position["quantity"]:
            raise RuntimeError(
                "Free exchange balance is below the tracked position quantity. "
                "Reconcile account balances before attaching exits."
            )
        self._assert_no_open_symbol_orders(self.symbol)
        market = Decimal(str(self.client.ticker_price(self.symbol)))
        entry = Decimal(str(position["cost_quote"])) / Decimal(str(position["quantity"]))
        stop = entry * (Decimal("1") - Decimal(str(self.stop_loss_pct)) / Decimal("100"))
        target = entry * (Decimal("1") + Decimal(str(self.take_profit_pct)) / Decimal("100"))
        if not stop < market < target:
            raise RuntimeError(
                "Configured entry-based exit prices are not both on the valid sides of "
                "the current market. Adjust the percentages or close the position."
            )
        list_client_id = f"ax_{uuid.uuid4().hex}"
        protection = {
            "status": "submitting",
            "client_order_id": list_client_id,
            "stop_price": float(stop),
            "take_profit_price": float(target),
            "reported_fills": {},
        }
        self.ledger.set_protection(self.symbol, protection)
        try:
            response = self.client.market_oco_sell(
                self.symbol,
                position["quantity"],
                rules["step_size"],
                rules["tick_size"],
                rules["min_qty"],
                rules["min_notional"],
                market,
                stop,
                target,
                list_client_id,
            )
        except Exception as error:
            protection["status"] = (
                "unprotected"
                if isinstance(error, BinanceTestnetError) and error.code is not None
                else "unknown"
            )
            self.ledger.set_protection(self.symbol, protection)
            raise RuntimeError(
                f"Buy filled, but Binance protective exits were not confirmed ({error}). "
                "Automation must remain stopped until the position is reviewed."
            ) from error
        is_active = response["listStatusType"] == "EXEC_STARTED"
        protection.update(
            {
                "status": "active" if is_active else "unknown",
                "order_list_id": response["orderListId"],
                "order_client_ids": [
                    report["clientOrderId"] for report in response["orderReports"]
                ],
                "stop_price": float(response["stopPrice"]),
                "take_profit_price": float(response["takeProfitPrice"]),
                "quantity": float(response["quantity"]),
            }
        )
        self.ledger.set_protection(self.symbol, protection)
        if not is_active:
            self._reconcile_protection()
        return response

    def protect_open_position(self):
        if self.ledger.pending_order:
            raise RuntimeError("Reconcile the pending exchange order before changing position protection.")
        current = self.ledger.position(self.symbol)
        if current["quantity"] <= 0:
            raise RuntimeError("There is no bot-managed position to protect.")
        status = current.get("protection", {}).get("status", "unprotected")
        if status != "unprotected":
            raise RuntimeError(
                f"Exit status is {status}; verify or reconcile it instead of placing another exit list."
            )
        if getattr(self.client, "IS_LIVE", False):
            validate_live_api_restrictions(
                self.client.api_key_restrictions(), require_trading=True
            )
            account = self.client.account()
            if account.get("canTrade") is not True:
                raise RuntimeError("Binance reports that this account cannot trade Spot.")
        return self._submit_protection()

    def verify_protection_status(self):
        position = self.ledger.position(self.symbol)
        if position["quantity"] <= 0:
            return "There is no open bot-managed position."
        state = position.get("protection", {"status": "unprotected"})
        if state.get("status") == "unprotected":
            return "No Binance protective exit list is recorded for this position."
        try:
            self._reconcile_protection()
        except RuntimeError as error:
            current = self.ledger.position(self.symbol)
            if current["quantity"] <= 1e-10:
                return f"Binance protective exit filled; tracked position is closed. {error}"
            if current.get("protection", {}).get("status") in ("partial", "unprotected"):
                return str(error)
            raise
        current = self.ledger.position(self.symbol)["protection"]
        return (
            f"Binance protective exits are active for {self.symbol}: stop "
            f"{current.get('stop_price', 0):.8g}, take-profit "
            f"{current.get('take_profit_price', 0):.8g}."
        )

    def close_open_position(self):
        if self.ledger.pending_order:
            raise RuntimeError("Reconcile the pending exchange order before closing the position.")
        position = self.ledger.position(self.symbol)
        if position["quantity"] <= 0:
            raise RuntimeError("There is no bot-managed position to close.")
        if getattr(self.client, "IS_LIVE", False):
            validate_live_api_restrictions(
                self.client.api_key_restrictions(), require_trading=True
            )
            account = self.client.account()
            if account.get("canTrade") is not True:
                raise RuntimeError("Binance reports that this account cannot trade Spot.")
        else:
            account = self.client.account()

        protection = position.get("protection", {"status": "unprotected"})
        if protection.get("status") not in ("unprotected",):
            list_client_id = protection.get("client_order_id")
            order_list_id = protection.get("order_list_id")
            if (
                not list_client_id
                or isinstance(order_list_id, bool)
                or not isinstance(order_list_id, int)
                or order_list_id < 0
            ):
                self.ledger.set_protection(
                    self.symbol, {**protection, "status": "unknown"}
                )
                raise RuntimeError(
                    "Protective exits cannot be safely canceled without their exchange order-list ID."
                )
            try:
                current_list = self.client.order_list_status(list_client_id)
                current_list = self._parse_order_list(
                    current_list,
                    self.symbol,
                    list_client_id,
                    protection.get("order_list_id"),
                )
                self._apply_order_list_fills(current_list)
                if current_list["listStatusType"] == "EXEC_STARTED":
                    self.ledger.set_protection(
                        self.symbol, {**protection, "status": "cancel_pending"}
                    )
                    self.client.cancel_order_list(order_list_id)
                    current_list = self.client.order_list_status(list_client_id)
                    current_list = self._parse_order_list(
                        current_list,
                        self.symbol,
                        list_client_id,
                        protection.get("order_list_id"),
                    )
                    self._apply_order_list_fills(current_list)
                if current_list["listStatusType"] != "ALL_DONE":
                    raise RuntimeError(
                        "Binance has not confirmed cancellation of both exit orders; no market sell was sent."
                    )
            except Exception as error:
                self.ledger.set_protection(
                    self.symbol,
                    {**self.ledger.position(self.symbol).get("protection", protection), "status": "unknown"},
                )
                raise RuntimeError(
                    f"Could not confirm protective-order cancellation ({error}); no market sell was sent."
                ) from error
            if self.ledger.position(self.symbol)["quantity"] <= 1e-10:
                self.ledger.clear_protection(self.symbol)
                return "Binance protective exit filled while closing; position is already closed."
            self.ledger.clear_protection(self.symbol)
            account = self.client.account()

        self._assert_no_open_symbol_orders(self.symbol)
        rules = self.client.symbol_rules(self.symbol)
        free_base = self._balance(account, rules["base_asset"])
        last_price = self.client.ticker_price(self.symbol)
        quantity = normalize_market_sell_quantity(
            min(self.ledger.position(self.symbol)["quantity"], free_base),
            rules["step_size"],
            rules["min_qty"],
            rules["min_notional"],
            last_price,
        )
        client_order_id = f"ast_{uuid.uuid4().hex}"
        self.ledger.reserve_order(self.symbol, "SELL", client_order_id)
        order = self.client.market_sell(
            self.symbol,
            quantity,
            rules["step_size"],
            rules["min_qty"],
            rules["min_notional"],
            last_price,
            client_order_id,
        )
        realized = self.ledger.record_sell(
            self.symbol, order, rules["base_asset"], rules["quote_asset"]
        )
        return {
            "order": order,
            "realized_pnl": realized,
            "message": f"Managed position sold at market on {self.client.ENVIRONMENT_NAME}.",
        }

    def run_cycle(self, execute_orders=False, progress_callback=None):
        if progress_callback:
            progress_callback(5, "Preparing market and account checks")
        if execute_orders and self.ledger.pending_order:
            raise RuntimeError(
                "A previous order has an unknown result. Reconcile it before sending another."
            )
        if execute_orders and self.ledger.position(self.symbol)["quantity"] > 0:
            self._reconcile_protection()

        live_account = None
        equity_risk = None
        if execute_orders and getattr(self.client, "IS_LIVE", False):
            restrictions = self.client.api_key_restrictions()
            validate_live_api_restrictions(restrictions, require_trading=True)
            live_account = self.client.account()
            if live_account.get("canTrade") is not True:
                raise RuntimeError("Binance reports that this account cannot trade Spot.")
            equity = float(self.client.account_equity_usdt(live_account))
            equity_risk = self.ledger.observe_account_equity(equity)
            if self.daily_loss_limit >= equity_risk["start_usdt"]:
                raise RuntimeError(
                    "Daily equity-loss limit must be less than the observed account equity."
                )

        candles = self.client.klines(self.symbol, interval=self.interval, limit=120)
        if progress_callback:
            progress_callback(42, "Calculating indicators")
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        closed_candles = [candle for candle in candles if candle["close_time"] < now_ms]
        indicators = calculate_indicators(closed_candles)
        if (
            execute_orders
            and closed_candles
            and closed_candles[-1]["close_time"] == self.last_analyzed_close_time
        ):
            return {
                "symbol": self.symbol,
                "indicators": indicators,
                "decision": {
                    "action": "HOLD",
                    "confidence": 0.0,
                    "reason": (
                        f"No new closed {self.interval} candle; waiting for the next signal."
                    ),
                },
                "order": None,
                "message": (
                    f"No new {self.interval} candle to evaluate; automation remains active."
                ),
            }
        if progress_callback:
            progress_callback(62, "Running local Ollama model")
        decision = self.analyzer.analyze(self.symbol, indicators)
        if not isinstance(decision, dict):
            raise ValueError("AI analysis did not return a decision object.")
        action = decision.get("action")
        confidence = decision.get("confidence")
        reason = decision.get("reason")
        if (
            action not in ("BUY", "SELL", "HOLD")
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 500
        ):
            raise ValueError("AI decision failed validation; no order was sent.")
        if execute_orders and closed_candles:
            self.last_analyzed_close_time = closed_candles[-1]["close_time"]
        position = self.ledger.position(self.symbol)
        if progress_callback:
            progress_callback(82, "Validating prediction and risk rules")
        result = {
            "symbol": self.symbol,
            "indicators": indicators,
            "decision": decision,
            "order": None,
            "message": "Analysis only. Automatic testnet execution is disabled.",
        }
        if equity_risk is not None:
            result["equity_risk"] = equity_risk
            if equity_risk["drawdown_usdt"] >= self.daily_loss_limit:
                result["message"] = (
                    "Daily account-equity loss limit reached. Automation is halted; "
                    "no further orders will be sent. Existing position is left untouched."
                )
                result["halt_automation"] = True
                return result

        if action == "SELL" and position["quantity"] <= 0:
            result["model_decision"] = decision
            result["decision"] = {
                "action": "HOLD",
                "confidence": confidence,
                "reason": (
                    "The model suggested SELL, but there is no bot-managed position "
                    "to close. No order is needed."
                ),
            }
            result["message"] = (
                "No open bot-managed position; ignored the SELL signal. "
                "Automation continues and will check again next cycle."
            )
            return result

        if action == "HOLD":
            result["message"] = "AI recommends HOLD; no order was sent."
            return result
        if confidence < MIN_CONFIDENCE:
            result["message"] = (
                f"Order blocked: AI confidence {confidence:.0%} is below "
                f"the {MIN_CONFIDENCE:.0%} minimum."
            )
            return result
        guard = self._technical_guard(
            action, indicators, position["quantity"]
        )
        if guard:
            result["message"] = guard
            return result
        if not execute_orders:
            result["message"] = "Signal passed analysis checks; preview only, no order sent."
            return result

        if progress_callback:
            progress_callback(92, "Checking account and preparing order")
        if getattr(self.client, "IS_LIVE", False):
            live_account = self.client.account()
            if live_account.get("canTrade") is not True:
                raise RuntimeError("Binance reports that this account cannot trade Spot.")
            current_equity = float(self.client.account_equity_usdt(live_account))
            equity_risk = self.ledger.observe_account_equity(current_equity)
            result["equity_risk"] = equity_risk
            if equity_risk["drawdown_usdt"] >= self.daily_loss_limit:
                result["message"] = (
                    "Daily account-equity loss limit reached. Automation is halted; "
                    "no further orders will be sent. Existing position is left untouched."
                )
                result["halt_automation"] = True
                return result

        rules = self.client.symbol_rules(self.symbol)
        if rules["quote_asset"] != "USDT":
            result["message"] = "This release supports USDT-quoted Spot pairs only."
            return result
        if action == "BUY" and self.max_trade_quote < float(
            rules["min_notional"]
        ):
            result["message"] = (
                "Configured buy cap is below the exchange's minimum order value for this pair."
            )
            return result
        account = live_account if live_account is not None else self.client.account()
        free_quote = self._balance(account, rules["quote_asset"])
        if action == "BUY":
            if self.max_trade_quote > free_quote:
                result["message"] = (
                    "Configured per-trade buy amount exceeds available free quote balance."
                )
                return result
            block = self.ledger.can_buy(
                self.symbol,
                self.max_trade_quote,
                free_quote,
                self.max_trade_quote,
                self.daily_loss_limit,
            )
            if block:
                result["message"] = block
                return result
            client_order_id = f"ast_{uuid.uuid4().hex}"
            self.ledger.reserve_order(self.symbol, "BUY", client_order_id)
            order = self.client.market_buy(
                self.symbol, self.max_trade_quote, client_order_id
            )
            self.ledger.record_buy(
                self.symbol,
                order,
                rules["base_asset"],
                rules["quote_asset"],
            )
            try:
                exit_order_list = self._submit_protection()
                result["exit_order_list"] = exit_order_list
                result["protection_status"] = "active"
            except Exception as error:
                result["protection_status"] = self.ledger.position(self.symbol)[
                    "protection"
                ]["status"]
                result["halt_automation"] = True
                result["message"] = (
                    f"Buy filled but protective exits are {result['protection_status']}. "
                    f"Automation halted: {error}"
                )
                result["order"] = order
                return result
        else:
            protection = position.get("protection", {"status": "unprotected"})
            if protection.get("status") != "unprotected":
                order_list_id = protection.get("order_list_id")
                if (
                    isinstance(order_list_id, bool)
                    or not isinstance(order_list_id, int)
                    or order_list_id < 0
                ):
                    self.ledger.set_protection(
                        self.symbol, {**protection, "status": "unknown"}
                    )
                    raise RuntimeError(
                        "Protective exits cannot be safely canceled without their exchange order-list ID."
                    )
                current_list = self.client.order_list_status(
                    protection["client_order_id"]
                )
                current_list = self._parse_order_list(
                    current_list,
                    self.symbol,
                    protection["client_order_id"],
                    protection.get("order_list_id"),
                )
                self._apply_order_list_fills(current_list)
                if current_list["listStatusType"] == "EXEC_STARTED":
                    self.ledger.set_protection(
                        self.symbol, {**protection, "status": "cancel_pending"}
                    )
                    self.client.cancel_order_list(order_list_id)
                    current_list = self.client.order_list_status(
                        protection["client_order_id"]
                    )
                    current_list = self._parse_order_list(
                        current_list,
                        self.symbol,
                        protection["client_order_id"],
                        protection.get("order_list_id"),
                    )
                    self._apply_order_list_fills(current_list)
                if current_list["listStatusType"] != "ALL_DONE":
                    raise RuntimeError(
                        "Binance has not confirmed protective-exit cancellation; no market sell was sent."
                    )
                if self.ledger.position(self.symbol)["quantity"] <= 1e-10:
                    self.ledger.clear_protection(self.symbol)
                    result["message"] = (
                        "A Binance protective exit filled while the strategy was evaluating; "
                        "no additional sell was sent."
                    )
                    result["halt_automation"] = True
                    return result
                self.ledger.clear_protection(self.symbol)
                account = self.client.account()
            self._assert_no_open_symbol_orders(self.symbol)
            free_base = self._balance(account, rules["base_asset"])
            quantity = normalize_market_sell_quantity(
                min(self.ledger.position(self.symbol)["quantity"], free_base),
                rules["step_size"],
                rules["min_qty"],
                rules["min_notional"],
                indicators["last_price"],
            )
            client_order_id = f"ast_{uuid.uuid4().hex}"
            self.ledger.reserve_order(self.symbol, "SELL", client_order_id)
            order = self.client.market_sell(
                self.symbol,
                quantity,
                rules["step_size"],
                rules["min_qty"],
                rules["min_notional"],
                indicators["last_price"],
                client_order_id,
            )
            realized = self.ledger.record_sell(
                self.symbol,
                order,
                rules["base_asset"],
                rules["quote_asset"],
            )
            result["realized_pnl"] = realized

        result["order"] = order
        result["message"] = (
            f"{self.client.ENVIRONMENT_NAME} {action} order accepted."
        )
        return result

    def reconcile_pending_order(self):
        pending = self.ledger.pending_order
        if not pending:
            return "There is no pending order to reconcile."

        symbol = pending["symbol"]
        client_order_id = pending["client_order_id"]
        try:
            order = self.client.order_status(symbol, client_order_id)
        except BinanceTestnetError as error:
            if error.code != -2013:
                raise
            raise RuntimeError(
                f"{self.client.ENVIRONMENT_NAME} did not return this order. The pending "
                "intent remains locked. Verify the order/trade history manually before "
                "using the separate explicit 'Confirm no order exists' action."
            )

        if order.get("status") in ("NEW", "PARTIALLY_FILLED"):
            order = self.client.cancel_order(symbol, client_order_id)
        status = order.get("status")
        if status not in ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"):
            raise RuntimeError(
                f"Order remains in state {status!r}; it was not changed in the ledger."
            )

        rules = self.client.symbol_rules(symbol)
        if float(order.get("executedQty", 0)) > 0:
            if pending["side"] == "BUY":
                self.ledger.record_buy(
                    symbol, order, rules["base_asset"], rules["quote_asset"]
                )
            else:
                realized = self.ledger.record_sell(
                    symbol, order, rules["base_asset"], rules["quote_asset"]
                )
                return f"Reconciled {status} sell order; estimated realized PnL: {realized:.4f} {rules['quote_asset']}."
            return f"Reconciled {status} buy order from {self.client.ENVIRONMENT_NAME}."
        self.ledger.clear_pending_order(client_order_id)
        return f"Order ended in {status} without a fill; pending intent cleared."
