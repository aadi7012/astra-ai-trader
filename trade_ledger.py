import json
import math
import os
import tempfile
from datetime import datetime, timezone


class LedgerError(RuntimeError):
    pass


class TradeLedger:
    """Persists bot-managed positions, exchange exits, and estimated realized P/L."""

    def __init__(self, path):
        self.path = path
        self.data = {
            "version": 2,
            "utc_day": self._today(),
            "realized_pnl": 0.0,
            "positions": {},
            "trades": [],
            "pending_order": None,
            "equity_day": None,
            "equity_start_usdt": None,
            "equity_high_water_usdt": None,
            "account_fingerprint": None,
        }
        self._load()

    @staticmethod
    def _today():
        return datetime.now(timezone.utc).date().isoformat()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
            if (
                not isinstance(data, dict)
                or data.get("version") not in (1, 2)
                or not isinstance(data.get("positions"), dict)
                or not isinstance(data.get("trades"), list)
                or not isinstance(data.get("realized_pnl"), (int, float))
                or not math.isfinite(data.get("realized_pnl", 0))
                or any(
                    data.get(field) is not None
                    and (
                        not isinstance(data[field], (int, float))
                        or not math.isfinite(data[field])
                        or data[field] < 0
                    )
                    or (
                        data.get("account_fingerprint") is not None
                        and not isinstance(data.get("account_fingerprint"), str)
                    )
                    for field in ("equity_start_usdt", "equity_high_water_usdt")
                )
                or (
                    data.get("pending_order") is not None
                    and not isinstance(data.get("pending_order"), dict)
                )
            ):
                raise LedgerError("Trade ledger format is invalid. Trading is disabled.")
            pending = data.get("pending_order")
            if pending is not None and (
                pending.get("side") not in ("BUY", "SELL")
                or not isinstance(pending.get("symbol"), str)
                or not isinstance(pending.get("client_order_id"), str)
                or not pending["client_order_id"]
            ):
                raise LedgerError("Pending order record is invalid. Trading is disabled.")
            for position in data["positions"].values():
                if (
                    not isinstance(position, dict)
                    or not isinstance(position.get("quantity"), (int, float))
                    or not isinstance(position.get("cost_quote"), (int, float))
                    or not math.isfinite(position.get("quantity", 0))
                    or not math.isfinite(position.get("cost_quote", 0))
                    or position["quantity"] < 0
                    or position["cost_quote"] < 0
                ):
                    raise LedgerError("Trade ledger position is invalid. Trading is disabled.")
                protection = position.get("protection", {"status": "unprotected"})
                if (
                    not isinstance(protection, dict)
                    or protection.get("status")
                    not in (
                        "unprotected",
                        "submitting",
                        "active",
                        "unknown",
                        "cancel_pending",
                        "partial",
                    )
                    or (
                        protection.get("client_order_id") is not None
                        and (
                            not isinstance(protection["client_order_id"], str)
                            or not protection["client_order_id"]
                        )
                    )
                    or (
                        protection.get("order_list_id") is not None
                        and (
                            isinstance(protection["order_list_id"], bool)
                            or not isinstance(protection["order_list_id"], int)
                        )
                    )
                ):
                    raise LedgerError("Trade ledger exit protection is invalid. Trading is disabled.")
                for price_key in ("stop_price", "take_profit_price"):
                    value = protection.get(price_key)
                    if value is not None and (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        or value <= 0
                    ):
                        raise LedgerError("Trade ledger exit price is invalid. Trading is disabled.")
                reports = protection.get("reported_fills", {})
                if not isinstance(reports, dict) or any(
                    not isinstance(client_id, str)
                    or not isinstance(report, dict)
                    or any(
                        isinstance(report.get(key), bool)
                        or not isinstance(report.get(key), (int, float))
                        or not math.isfinite(report.get(key))
                        or report.get(key) < 0
                        for key in ("executed_qty", "quote_qty")
                    )
                    for client_id, report in reports.items()
                ):
                    raise LedgerError("Trade ledger exit fills are invalid. Trading is disabled.")
                position["protection"] = protection
            if data.get("utc_day") != self._today():
                data["utc_day"] = self._today()
                data["realized_pnl"] = 0.0
            migrated = data.get("version") != 2
            data["version"] = 2
            self.data = data
            if migrated:
                self._save()
        except LedgerError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LedgerError(f"Could not read trade ledger; refusing to trade: {error}") from error

    def _save(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        descriptor, temp_path = tempfile.mkstemp(
            dir=directory, prefix=".trade-ledger-", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(self.data, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, self.path)
        except OSError as error:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise LedgerError(f"Could not persist trade ledger; trading halted: {error}") from error

    def position(self, symbol):
        position = dict(
            self.data["positions"].get(
                symbol,
                {
                    "quantity": 0.0,
                    "cost_quote": 0.0,
                    "protection": {"status": "unprotected"},
                },
            )
        )
        position.setdefault("protection", {"status": "unprotected"})
        return position

    def positions(self):
        return {symbol: self.position(symbol) for symbol in self.data["positions"]}

    def set_protection(self, symbol, protection):
        if symbol not in self.data["positions"]:
            raise LedgerError("Cannot update exits for a position that is not tracked.")
        if not isinstance(protection, dict) or protection.get("status") not in (
            "unprotected",
            "submitting",
            "active",
            "unknown",
            "cancel_pending",
            "partial",
        ):
            raise LedgerError("Exit protection state is invalid.")
        old_position = dict(self.data["positions"][symbol])
        self.data["positions"][symbol]["protection"] = dict(protection)
        try:
            self._save()
        except LedgerError:
            self.data["positions"][symbol] = old_position
            raise

    def clear_protection(self, symbol):
        position = self.data["positions"].get(symbol)
        if not position:
            return
        if position["quantity"] <= 1e-10:
            self.data["positions"].pop(symbol)
        else:
            self.set_protection(symbol, {"status": "unprotected"})
            return
        try:
            self._save()
        except LedgerError:
            self.data["positions"][symbol] = position
            raise

    def record_protection_report(self, symbol, client_order_id, executed_qty, quote_qty):
        values = (executed_qty, quote_qty)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in values
        ):
            raise LedgerError("Exchange exit fill totals are invalid.")
        position = self.data["positions"].get(symbol)
        if not position or not isinstance(client_order_id, str) or not client_order_id:
            raise LedgerError("Exchange exit fill does not match a tracked position.")
        protection = position.setdefault("protection", {"status": "unknown"})
        reports = protection.setdefault("reported_fills", {})
        previous = reports.get(client_order_id, {"executed_qty": 0.0, "quote_qty": 0.0})
        if executed_qty + 1e-10 < previous["executed_qty"] or quote_qty + 1e-8 < previous["quote_qty"]:
            raise LedgerError("Exchange exit fill totals moved backwards; refusing reconciliation.")
        delta_qty = max(0.0, float(executed_qty) - previous["executed_qty"])
        delta_quote = max(0.0, float(quote_qty) - previous["quote_qty"])
        old_data = json.loads(json.dumps(self.data))
        if delta_qty > 0:
            current_qty = float(position["quantity"])
            current_cost = float(position["cost_quote"])
            if delta_qty > current_qty * (1 + 1e-8) or delta_quote <= 0:
                raise LedgerError("Exchange exit fills exceed the locally tracked position.")
            allocated_cost = current_cost * min(1.0, delta_qty / current_qty)
            realized = delta_quote - allocated_cost
            remaining_qty = max(0.0, current_qty - delta_qty)
            remaining_cost = max(0.0, current_cost - allocated_cost)
            position["quantity"] = remaining_qty
            position["cost_quote"] = remaining_cost
            self.data["utc_day"] = self._today()
            self.data["realized_pnl"] = float(self.data["realized_pnl"]) + realized
            self._append_trade(
                {
                    "symbol": symbol,
                    "side": "SELL",
                    "quantity": delta_qty,
                    "quote": delta_quote,
                    "realized_pnl": realized,
                    "source": "exchange-managed exit",
                }
            )
        reports[client_order_id] = {
            "executed_qty": float(executed_qty),
            "quote_qty": float(quote_qty),
        }
        try:
            self._save()
        except LedgerError:
            self.data = old_data
            raise
        return delta_qty

    def bind_account(self, account_fingerprint):
        if not isinstance(account_fingerprint, str) or not account_fingerprint:
            raise LedgerError("Invalid account fingerprint.")
        stored = self.data.get("account_fingerprint")
        if stored is not None and stored != account_fingerprint:
            raise LedgerError(
                "This live ledger is already associated with another API key. "
                "Verify the existing account position and reconcile it before changing keys."
            )
        if stored is None:
            self.data["account_fingerprint"] = account_fingerprint
            try:
                self._save()
            except LedgerError:
                self.data["account_fingerprint"] = None
                raise

    def observe_account_equity(self, equity_usdt):
        if (
            isinstance(equity_usdt, bool)
            or not isinstance(equity_usdt, (int, float))
            or not math.isfinite(equity_usdt)
            or equity_usdt <= 0
        ):
            raise LedgerError("Invalid total account equity; refusing to trade.")
        today = self._today()
        old_data = json.loads(json.dumps(self.data))
        if self.data.get("equity_day") != today:
            self.data["equity_day"] = today
            self.data["equity_start_usdt"] = float(equity_usdt)
            self.data["equity_high_water_usdt"] = float(equity_usdt)
        elif self.data.get("equity_start_usdt") is None:
            self.data["equity_start_usdt"] = float(equity_usdt)
            self.data["equity_high_water_usdt"] = float(equity_usdt)
        else:
            self.data["equity_high_water_usdt"] = max(
                float(self.data.get("equity_high_water_usdt") or 0),
                float(equity_usdt),
            )
        try:
            self._save()
        except LedgerError:
            self.data = old_data
            raise

        start = float(self.data["equity_start_usdt"])
        high_water = float(self.data["equity_high_water_usdt"])
        return {
            "equity_usdt": float(equity_usdt),
            "start_usdt": start,
            "high_water_usdt": high_water,
            "loss_from_start_usdt": max(0.0, start - float(equity_usdt)),
            "drawdown_usdt": max(0.0, high_water - float(equity_usdt)),
        }

    @property
    def pending_order(self):
        return self.data.get("pending_order")

    def reserve_order(self, symbol, side, client_order_id):
        if self.pending_order:
            raise LedgerError(
                "A previous order needs reconciliation; refusing to send another order."
            )
        if side not in ("BUY", "SELL"):
            raise LedgerError("Invalid order side.")
        self.data["pending_order"] = {
            "symbol": symbol,
            "side": side,
            "client_order_id": client_order_id,
        }
        try:
            self._save()
        except LedgerError:
            self.data["pending_order"] = None
            raise

    def clear_pending_order(self, client_order_id):
        if not self.pending_order:
            return
        if self.pending_order.get("client_order_id") != client_order_id:
            raise LedgerError("Pending order identifier does not match.")
        previous = self.data["pending_order"]
        self.data["pending_order"] = None
        try:
            self._save()
        except LedgerError:
            self.data["pending_order"] = previous
            raise

    def realized_pnl_today(self):
        if self.data["utc_day"] != self._today():
            old_day = self.data["utc_day"]
            old_pnl = self.data["realized_pnl"]
            self.data["utc_day"] = self._today()
            self.data["realized_pnl"] = 0.0
            try:
                self._save()
            except LedgerError:
                self.data["utc_day"] = old_day
                self.data["realized_pnl"] = old_pnl
                raise
        return float(self.data["realized_pnl"])

    def can_buy(self, symbol, quote_amount, free_quote, max_trade_quote, daily_loss_limit):
        limits = (quote_amount, free_quote, max_trade_quote, daily_loss_limit)
        if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in limits):
            return "Risk inputs are invalid; refusing to place an order."
        if free_quote < 0 or max_trade_quote <= 0 or daily_loss_limit <= 0:
            return "Risk inputs are outside their valid range; refusing to place an order."
        if quote_amount <= 0:
            return "Configured order amount must be greater than zero."
        if quote_amount > max_trade_quote:
            return "Order amount exceeds the configured per-trade cap."
        if quote_amount > free_quote:
            return "Available testnet quote balance is below the configured order amount."
        if any(position["quantity"] > 0 for position in self.data["positions"].values()):
            return "A bot-managed position is already open; only one position is allowed at a time."
        if self.realized_pnl_today() <= -daily_loss_limit:
            return "Daily realized-loss limit reached; no further automated buys are allowed."
        return None

    def record_buy(self, symbol, order, base_asset, quote_asset):
        executed_qty = float(order.get("executedQty", 0))
        quote_spent = float(order.get("cummulativeQuoteQty", 0))
        if executed_qty <= 0 or quote_spent <= 0:
            raise LedgerError("Buy response contains no executed quantity; refusing to track it.")

        net_qty = executed_qty
        total_cost = quote_spent
        for fill in order.get("fills", []):
            commission = float(fill.get("commission", 0))
            commission_asset = fill.get("commissionAsset")
            if commission_asset == base_asset:
                net_qty -= commission
            elif commission_asset == quote_asset:
                total_cost += commission
        if net_qty <= 0:
            raise LedgerError("Buy fills leave no position after commission.")

        old_data = json.loads(json.dumps(self.data))
        self._check_pending(symbol, "BUY", order)
        current = self.position(symbol)
        current["quantity"] += net_qty
        current["cost_quote"] += total_cost
        current.setdefault("protection", {"status": "unprotected"})
        self.data["positions"][symbol] = current
        self.data["pending_order"] = None
        self._append_trade(
            {"symbol": symbol, "side": "BUY", "quantity": net_qty, "quote": total_cost}
        )
        try:
            self._save()
        except LedgerError:
            self.data = old_data
            raise
        return current

    def record_sell(self, symbol, order, base_asset, quote_asset):
        executed_qty = float(order.get("executedQty", 0))
        quote_received = float(order.get("cummulativeQuoteQty", 0))
        current = self.position(symbol)
        self._check_pending(symbol, "SELL", order)
        if executed_qty <= 0 or quote_received <= 0:
            raise LedgerError("Sell response contains no executed quantity; refusing to track it.")
        if executed_qty > current["quantity"] * (1 + 1e-8):
            raise LedgerError("Sell execution exceeds the locally managed position.")

        quote_commission = sum(
            float(fill.get("commission", 0))
            for fill in order.get("fills", [])
            if fill.get("commissionAsset") == quote_asset
        )
        base_commission = sum(
            float(fill.get("commission", 0))
            for fill in order.get("fills", [])
            if fill.get("commissionAsset") == base_asset
        )
        net_proceeds = quote_received - quote_commission
        removed_qty = executed_qty + base_commission
        if removed_qty > current["quantity"] * (1 + 1e-8):
            raise LedgerError("Sell commissions exceed the bot-managed position.")
        allocated_cost = current["cost_quote"] * removed_qty / current["quantity"]
        realized = net_proceeds - allocated_cost
        remaining_qty = max(0.0, current["quantity"] - removed_qty)
        remaining_cost = max(0.0, current["cost_quote"] - allocated_cost)

        old_data = json.loads(json.dumps(self.data))
        self.data["pending_order"] = None
        self.data["utc_day"] = self._today()
        self.data["realized_pnl"] = float(self.data["realized_pnl"]) + realized
        if remaining_qty <= 1e-10:
            self.data["positions"].pop(symbol, None)
        else:
            self.data["positions"][symbol] = {
                "quantity": remaining_qty,
                "cost_quote": remaining_cost,
                "protection": current.get(
                    "protection", {"status": "unprotected"}
                ),
            }
        self._append_trade(
            {
                "symbol": symbol,
                "side": "SELL",
                "quantity": executed_qty,
                "quote": net_proceeds,
                "realized_pnl": realized,
            }
        )
        try:
            self._save()
        except LedgerError:
            self.data = old_data
            raise
        return realized

    def _check_pending(self, symbol, side, order):
        pending = self.pending_order
        if pending and (
            pending.get("symbol") != symbol
            or pending.get("side") != side
            or pending.get("client_order_id") != order.get("clientOrderId")
        ):
            raise LedgerError("Exchange order does not match the pending ledger entry.")

    def _append_trade(self, record):
        self.data["trades"].append(
            {**record, "timestamp": datetime.now(timezone.utc).isoformat()}
        )
        self.data["trades"] = self.data["trades"][-500:]
