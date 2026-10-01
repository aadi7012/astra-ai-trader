from decimal import Decimal, InvalidOperation


def result_alert_messages(result, enabled=True):
    if not enabled or not isinstance(result, dict):
        return []

    messages = []
    decision = result.get("decision")
    if isinstance(decision, dict) and decision.get("action") in ("BUY", "SELL"):
        messages.append(
            f"{result.get('symbol', 'Binance Spot')}: {decision['action']} signal "
            f"({decision.get('confidence', 0):.0%} model-reported confidence)."
        )

    order = result.get("order")
    if isinstance(order, dict):
        try:
            executed_quantity = Decimal(str(order.get("executedQty", "0")))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("Order response has invalid executed quantity.") from error
        if not executed_quantity.is_finite() or executed_quantity < 0:
            raise ValueError("Order response has invalid executed quantity.")
        if executed_quantity > 0:
            messages.append(
                f"Fill received: {result.get('symbol', 'Binance Spot')} "
                f"{order.get('status', 'UNKNOWN')} · {executed_quantity} base units."
            )

    if result.get("halt_automation") is True:
        messages.append(f"RISK STOP: {result.get('message', 'Automation halted.')}")
    return messages


def message_alert(message, enabled=True):
    if not enabled or not isinstance(message, str):
        return None
    if message.startswith("Reconciled "):
        return f"Order fill reconciled: {message}"
    if message.startswith("Automation halted for safety;"):
        return f"RISK STOP: {message}"
    return None
