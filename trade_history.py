from collections import deque
from decimal import Decimal, InvalidOperation


class TradeHistoryError(ValueError):
    pass


def parse_trade_history(trades, base_asset, quote_asset):
    if not isinstance(trades, list) or len(trades) > 1000:
        raise TradeHistoryError("Binance returned an invalid or oversized trade history.")
    if (
        not isinstance(base_asset, str)
        or not base_asset
        or not isinstance(quote_asset, str)
        or not quote_asset
        or base_asset == quote_asset
    ):
        raise TradeHistoryError("Trading-pair assets are invalid.")

    fills = []
    for trade in trades:
        if not isinstance(trade, dict):
            raise TradeHistoryError("Binance returned a malformed trade record.")
        try:
            trade_id = trade["id"]
            order_id = trade["orderId"]
            timestamp = trade["time"]
            is_buyer = trade["isBuyer"]
            price = Decimal(str(trade["price"]))
            quantity = Decimal(str(trade["qty"]))
            quote_quantity = Decimal(str(trade["quoteQty"]))
            commission = Decimal(str(trade["commission"]))
            commission_asset = trade["commissionAsset"]
        except (KeyError, TypeError, InvalidOperation, ValueError) as error:
            raise TradeHistoryError("Binance returned incomplete trade data.") from error

        if (
            isinstance(trade_id, bool)
            or not isinstance(trade_id, int)
            or trade_id < 0
            or isinstance(order_id, bool)
            or not isinstance(order_id, int)
            or order_id < 0
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 0
            or timestamp > 253402300799999
            or not isinstance(is_buyer, bool)
            or not isinstance(commission_asset, str)
            or not commission_asset
            or not all(value.is_finite() for value in (price, quantity, quote_quantity, commission))
            or price <= 0
            or quantity <= 0
            or quote_quantity <= 0
            or commission < 0
        ):
            raise TradeHistoryError("Binance returned invalid trade values.")

        fills.append(
            {
                "id": trade_id,
                "order_id": order_id,
                "time": timestamp,
                "side": "BUY" if is_buyer else "SELL",
                "price": price,
                "quantity": quantity,
                "quote_quantity": quote_quantity,
                "commission": commission,
                "commission_asset": commission_asset,
            }
        )

    fills.sort(key=lambda fill: (fill["time"], fill["id"]))
    lots = deque()
    for fill in fills:
        fee = fill["commission"]
        fee_asset = fill["commission_asset"]
        known_fee = fee == 0 or fee_asset in (base_asset, quote_asset)
        if fill["side"] == "BUY":
            quantity = fill["quantity"]
            cost = fill["quote_quantity"]
            if fee_asset == base_asset:
                quantity -= fee
            elif fee_asset == quote_asset:
                cost += fee
            if quantity <= 0:
                raise TradeHistoryError("A buy fill's base-asset fee exceeds its quantity.")
            lots.append(
                {
                    "quantity": quantity,
                    "unit_cost": cost / quantity,
                    "cost_known": known_fee,
                }
            )
            fill["realized_pnl"] = None
            continue

        sold_quantity = fill["quantity"] + (fee if fee_asset == base_asset else Decimal("0"))
        proceeds = fill["quote_quantity"]
        if fee_asset == quote_asset:
            proceeds -= fee
        if proceeds < 0:
            raise TradeHistoryError("A sell fill's quote-asset fee exceeds its proceeds.")

        remaining = sold_quantity
        matched_cost = Decimal("0")
        basis_known = True
        while remaining > 0 and lots:
            lot = lots[0]
            matched = min(remaining, lot["quantity"])
            matched_cost += matched * lot["unit_cost"]
            basis_known = basis_known and lot["cost_known"]
            lot["quantity"] -= matched
            remaining -= matched
            if lot["quantity"] == 0:
                lots.popleft()

        fill["realized_pnl"] = (
            proceeds - matched_cost
            if remaining == 0 and basis_known and known_fee
            else None
        )

    return fills
