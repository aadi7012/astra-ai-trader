import math


def calculate_unrealized_pnl(quantity, cost_quote, market_price):
    values = (quantity, cost_quote, market_price)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in values
    ):
        raise ValueError("Trade P/L inputs must be finite numbers.")
    if quantity < 0 or cost_quote < 0 or market_price <= 0:
        raise ValueError("Trade P/L inputs are outside valid ranges.")
    return quantity * market_price - cost_quote


def format_account_profile(account, key_restrictions=None):
    if not isinstance(account, dict):
        raise ValueError("Binance account response is invalid.")
    details = []
    account_type = account.get("accountType")
    if account_type is not None:
        if not isinstance(account_type, str) or not account_type:
            raise ValueError("Binance account response contains an invalid account type.")
        details.append(f"Account type: {account_type}")

    can_trade = account.get("canTrade")
    if not isinstance(can_trade, bool):
        raise ValueError("Binance account response has no valid trading status.")
    details.append(f"Spot trading: {'enabled' if can_trade else 'disabled'}")

    permissions = account.get("permissions")
    if permissions is not None:
        if not isinstance(permissions, list) or any(
            not isinstance(permission, str) for permission in permissions
        ):
            raise ValueError("Binance account response contains invalid permissions.")
        details.append(
            "Account permissions: "
            + (", ".join(sorted(permissions)) if permissions else "none listed")
        )
    if key_restrictions is not None:
        if not isinstance(key_restrictions, dict):
            raise ValueError("Binance returned invalid API-key restrictions.")
        trading_permission = key_restrictions.get("enableSpotAndMarginTrading")
        if not isinstance(trading_permission, bool):
            raise ValueError("Binance returned invalid API-key trading permissions.")
        details.append(
            "API-key Spot/Margin trading: "
            + ("enabled" if trading_permission else "disabled")
        )
    return " · ".join(details)


def format_account_balances(account):
    rows = parse_account_balances(account)
    return (
        "No nonzero assets."
        if not rows
        else "; ".join(
            f"{asset}: free {free:.10g}, locked {locked:.10g}"
            for asset, free, locked in rows
        )
    )


def parse_account_balances(account):
    if not isinstance(account, dict):
        raise ValueError("Binance account response is invalid.")
    balances = account.get("balances")
    if not isinstance(balances, list):
        raise ValueError("Binance account response has no valid balances list.")
    rows = []
    for balance in balances:
        if not isinstance(balance, dict):
            raise ValueError("Binance account response contains an invalid balance.")
        try:
            asset = balance["asset"]
            free = float(balance.get("free", "0"))
            locked = float(balance.get("locked", "0"))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Binance account response contains an invalid balance.") from error
        if (
            not isinstance(asset, str)
            or not asset
            or not math.isfinite(free)
            or not math.isfinite(locked)
            or free < 0
            or locked < 0
        ):
            raise ValueError("Binance account response contains an invalid balance.")
        if free or locked:
            rows.append((asset, free, locked))
    return sorted(rows)
