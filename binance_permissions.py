def validate_live_api_restrictions(restrictions, require_trading=False):
    if not isinstance(restrictions, dict):
        raise RuntimeError("Binance returned invalid API-key restrictions.")

    required = {
        "enableReading": True,
        "enableWithdrawals": False,
        "ipRestrict": True,
    }
    if require_trading:
        required["enableSpotAndMarginTrading"] = True

    mismatches = [
        f"{field} expected {expected!r}, received {restrictions.get(field, 'missing')!r}"
        for field, expected in required.items()
        if restrictions.get(field) is not expected
    ]
    if mismatches:
        purpose = "LIVE order automation" if require_trading else "account inspection"
        raise RuntimeError(
            f"Binance API-key settings do not meet requirements for {purpose}: "
            + "; ".join(mismatches)
            + ". Update the key restrictions in Binance API Management, verify the "
            "allowed IP matches this computer's current public IP, wait for changes "
            "to take effect, then reconnect."
        )
