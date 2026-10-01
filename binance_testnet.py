import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN

TESTNET_BASE_URL = "https://testnet.binance.vision"
LIVE_BASE_URL = "https://api.binance.com"
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")


class BinanceTestnetError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def normalize_market_sell_quantity(quantity, step_size, min_qty, min_notional, last_price):
    step = Decimal(str(step_size))
    qty = Decimal(str(quantity))
    price = Decimal(str(last_price))
    if (
        not step.is_finite()
        or step <= 0
        or not qty.is_finite()
        or qty <= 0
        or not price.is_finite()
        or price <= 0
    ):
        raise BinanceTestnetError("Sell quantity, price, or symbol step size is invalid.")
    rounded_quantity = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
    if rounded_quantity < Decimal(str(min_qty)):
        raise BinanceTestnetError("Managed position is below the symbol's minimum quantity.")
    if rounded_quantity * price < Decimal(str(min_notional)):
        raise BinanceTestnetError("Managed position is below the symbol's minimum order value.")
    return rounded_quantity


class BinanceSpotTestnet:
    """Binance Spot REST client base, pinned by concrete environment subclasses."""

    BASE_URL = TESTNET_BASE_URL
    ENVIRONMENT_NAME = "Binance Spot Testnet"
    IS_LIVE = False

    def __init__(self, api_key="", api_secret="", timeout=15):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.timeout = timeout

    def _request(self, method, path, params=None, signed=False):
        params = dict(params or {})
        headers = {"Accept": "application/json", "User-Agent": "AstraTrader/0.1"}
        if signed:
            if not self.api_key or not self.api_secret:
                raise BinanceTestnetError(
                    f"Enter {self.ENVIRONMENT_NAME} API credentials."
                )
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            query = urllib.parse.urlencode(params)
            signature = hmac.new(
                self.api_secret.encode("utf-8"),
                query.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            query = f"{query}&signature={signature}"
            headers["X-MBX-APIKEY"] = self.api_key
        else:
            query = urllib.parse.urlencode(params)

        url = f"{self.BASE_URL}{path}"
        body = None
        if method in ("GET", "DELETE") and query:
            url = f"{url}?{query}"
        elif method == "POST":
            body = query.encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            try:
                details = json.loads(error.read().decode("utf-8"))
                if not isinstance(details, dict):
                    raise ValueError("Unexpected error response.")
                message = details.get("msg", str(error))
                code = details.get("code")
                raise BinanceTestnetError(
                    f"{self.ENVIRONMENT_NAME} error {code}: {message}", code=code
                ) from error
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                raise BinanceTestnetError(
                    f"{self.ENVIRONMENT_NAME} returned HTTP {error.code}."
                ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise BinanceTestnetError(
                f"Could not reach {self.ENVIRONMENT_NAME}: {error}"
            ) from error

        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BinanceTestnetError(
                f"{self.ENVIRONMENT_NAME} returned an invalid response."
            ) from error

    @staticmethod
    def validate_symbol(symbol):
        symbol = symbol.strip().upper()
        if not SYMBOL_PATTERN.fullmatch(symbol):
            raise BinanceTestnetError("Enter a valid Binance symbol, such as BTCUSDT.")
        return symbol

    def ping(self):
        self._request("GET", "/api/v3/ping")
        return True

    def klines(self, symbol, interval="1h", limit=100):
        symbol = self.validate_symbol(symbol)
        if interval not in ("1m", "5m", "15m", "1h", "4h", "1d"):
            raise BinanceTestnetError("Unsupported candle interval.")
        rows = self._request(
            "GET",
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": min(max(int(limit), 50), 1000)},
        )
        try:
            return [
                {
                    "open_time": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "close_time": int(row[6]),
                }
                for row in rows
            ]
        except (TypeError, ValueError, IndexError) as error:
            raise BinanceTestnetError(
                f"{self.ENVIRONMENT_NAME} returned malformed candle data."
            ) from error

    def account(self):
        return self._request("GET", "/api/v3/account", signed=True)

    def my_trades(self, symbol, limit=1000):
        symbol = self.validate_symbol(symbol)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise BinanceTestnetError("Trade-history limit must be a positive integer.")
        return self._request(
            "GET",
            "/api/v3/myTrades",
            {"symbol": symbol, "limit": min(limit, 1000)},
            signed=True,
        )

    def api_key_restrictions(self):
        return self._request(
            "GET", "/sapi/v1/account/apiRestrictions", signed=True
        )

    def spot_exchange_info(self):
        return self._request("GET", "/api/v3/exchangeInfo")

    def ticker_24hr(self):
        return self._request("GET", "/api/v3/ticker/24hr")

    def ticker_price(self, symbol):
        symbol = self.validate_symbol(symbol)
        response = self._request(
            "GET", "/api/v3/ticker/price", {"symbol": symbol}
        )
        try:
            price = Decimal(response["price"])
        except (KeyError, TypeError, ValueError) as error:
            raise BinanceTestnetError(
                f"Binance returned no valid ticker price for {symbol}."
            ) from error
        if not price.is_finite() or price <= 0:
            raise BinanceTestnetError(f"Binance returned an invalid ticker price for {symbol}.")
        return price

    def ticker_prices(self, symbols):
        if not isinstance(symbols, (list, tuple)) or not symbols:
            raise BinanceTestnetError("At least one symbol is required for live price updates.")
        if any(not isinstance(symbol, str) for symbol in symbols):
            raise BinanceTestnetError("Live price symbols must be strings.")
        normalized = list(dict.fromkeys(self.validate_symbol(symbol) for symbol in symbols))
        if len(normalized) > 100:
            raise BinanceTestnetError("Live price updates support at most 100 symbols.")
        if len(normalized) == 1:
            response = self._request(
                "GET",
                "/api/v3/ticker/price",
                {"symbol": normalized[0]},
            )
            rows = [response]
        else:
            rows = self._request(
                "GET",
                "/api/v3/ticker/price",
                {"symbols": json.dumps(normalized, separators=(",", ":"))},
            )
        if not isinstance(rows, list):
            raise BinanceTestnetError(
                f"{self.ENVIRONMENT_NAME} returned invalid live ticker data."
            )
        prices = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("symbol") not in normalized:
                raise BinanceTestnetError(
                    f"{self.ENVIRONMENT_NAME} returned an unexpected live ticker."
                )
            symbol = row["symbol"]
            try:
                price = Decimal(row["price"])
            except (KeyError, TypeError, ValueError) as error:
                raise BinanceTestnetError(
                    f"{self.ENVIRONMENT_NAME} returned no valid ticker price for {symbol}."
                ) from error
            if not price.is_finite() or price <= 0:
                raise BinanceTestnetError(
                    f"{self.ENVIRONMENT_NAME} returned an invalid ticker price for {symbol}."
                )
            prices[symbol] = price
        return prices

    def account_equity_usdt(self, account=None):
        account = self.account() if account is None else account
        balances = account.get("balances")
        if not isinstance(balances, list):
            raise BinanceTestnetError("Account response has no valid balance list.")
        total = Decimal("0")
        for balance in balances:
            try:
                asset = balance["asset"]
                free = Decimal(balance.get("free", "0"))
                locked = Decimal(balance.get("locked", "0"))
            except (KeyError, TypeError, ValueError) as error:
                raise BinanceTestnetError("Account contains an invalid asset balance.") from error
            amount = free + locked
            if not amount.is_finite() or amount < 0:
                raise BinanceTestnetError(f"Account contains an invalid {asset} balance.")
            if amount == 0:
                continue
            if asset == "USDT":
                total += amount
                continue
            pair = f"{asset}USDT"
            if not SYMBOL_PATTERN.fullmatch(pair):
                raise BinanceTestnetError(
                    f"Cannot value {asset} in USDT; equity stop must not assume it is worthless."
                )
            total += amount * self.ticker_price(pair)
        if not total.is_finite() or total <= 0:
            raise BinanceTestnetError("Could not calculate a positive total account equity.")
        return total

    def order_status(self, symbol, client_order_id):
        return self._request(
            "GET",
            "/api/v3/order",
            {"symbol": self.validate_symbol(symbol), "origClientOrderId": client_order_id},
            signed=True,
        )

    def cancel_order(self, symbol, client_order_id):
        return self._request(
            "DELETE",
            "/api/v3/order",
            {"symbol": self.validate_symbol(symbol), "origClientOrderId": client_order_id},
            signed=True,
        )

    def open_orders(self, symbol):
        return self._request(
            "GET",
            "/api/v3/openOrders",
            {"symbol": self.validate_symbol(symbol)},
            signed=True,
        )

    def open_order_lists(self):
        return self._request("GET", "/api/v3/openOrderList", signed=True)

    def order_list_status(self, list_client_order_id):
        if not isinstance(list_client_order_id, str) or not list_client_order_id:
            raise BinanceTestnetError("Exit-list client identifier is invalid.")
        response = self._request(
            "GET",
            "/api/v3/orderList",
            {"origClientOrderId": list_client_order_id},
            signed=True,
        )
        if (
            not isinstance(response, dict)
            or response.get("listClientOrderId") != list_client_order_id
            or not isinstance(response.get("orders"), list)
            or len(response["orders"]) != 2
        ):
            raise BinanceTestnetError("Binance returned an invalid exit-list response.")
        order_reports = []
        for order in response["orders"]:
            if (
                not isinstance(order, dict)
                or not isinstance(order.get("symbol"), str)
                or not order["symbol"]
                or isinstance(order.get("orderId"), bool)
                or not isinstance(order.get("orderId"), int)
                or order["orderId"] < 0
                or not isinstance(order.get("clientOrderId"), str)
                or not order["clientOrderId"]
            ):
                raise BinanceTestnetError(
                    "Binance returned malformed child-order identifiers for the exit list."
                )
            order_report = self.order_status(
                order["symbol"], order["clientOrderId"]
            )
            if (
                not isinstance(order_report, dict)
                or order_report.get("symbol") != order["symbol"]
                or order_report.get("orderId") != order["orderId"]
                or order_report.get("clientOrderId") != order["clientOrderId"]
            ):
                raise BinanceTestnetError(
                    "Binance returned child-order details that do not match the exit list."
                )
            order_reports.append(order_report)
        response["orderReports"] = order_reports
        return response

    def cancel_order_list(self, order_list_id):
        if (
            isinstance(order_list_id, bool)
            or not isinstance(order_list_id, int)
            or order_list_id < 0
        ):
            raise BinanceTestnetError("Exchange order-list ID is invalid.")
        return self._request(
            "DELETE",
            "/api/v3/orderList",
            {"orderListId": order_list_id},
            signed=True,
        )

    def market_oco_sell(
        self,
        symbol,
        quantity,
        step_size,
        tick_size,
        min_qty,
        min_notional,
        last_price,
        stop_price,
        take_profit_price,
        list_client_order_id,
    ):
        symbol = self.validate_symbol(symbol)
        step = Decimal(str(step_size))
        tick = Decimal(str(tick_size))
        qty = Decimal(str(quantity))
        market = Decimal(str(last_price))
        stop = Decimal(str(stop_price))
        take_profit = Decimal(str(take_profit_price))
        if (
            not step.is_finite()
            or step <= 0
            or not tick.is_finite()
            or tick <= 0
            or not qty.is_finite()
            or qty <= 0
            or not market.is_finite()
            or market <= 0
            or not stop.is_finite()
            or not take_profit.is_finite()
            or not isinstance(list_client_order_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,36}", list_client_order_id)
        ):
            raise BinanceTestnetError("Exit quantity, prices, or list identifier are invalid.")
        rounded_quantity = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
        rounded_stop = (stop / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        rounded_take_profit = (
            (take_profit / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        )
        if rounded_quantity < Decimal(str(min_qty)):
            raise BinanceTestnetError("Managed position is below the symbol's minimum quantity.")
        if rounded_quantity * market < Decimal(str(min_notional)):
            raise BinanceTestnetError("Managed position is below the symbol's minimum order value.")
        if not rounded_stop < market < rounded_take_profit:
            raise BinanceTestnetError(
                "Exit prices are no longer valid relative to the current market price."
            )
        response = self._request(
            "POST",
            "/api/v3/orderList/oco",
            {
                "symbol": symbol,
                "side": "SELL",
                "quantity": format(rounded_quantity, "f"),
                "listClientOrderId": list_client_order_id,
                "aboveType": "TAKE_PROFIT",
                "aboveStopPrice": format(rounded_take_profit, "f"),
                "belowType": "STOP_LOSS",
                "belowStopPrice": format(rounded_stop, "f"),
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )
        if (
            not isinstance(response, dict)
            or response.get("listClientOrderId") != list_client_order_id
            or (
                (response.get("listStatusType"), response.get("listOrderStatus"))
                not in (("EXEC_STARTED", "EXECUTING"), ("ALL_DONE", "ALL_DONE"))
            )
            or isinstance(response.get("orderListId"), bool)
            or not isinstance(response.get("orderListId"), int)
            or not isinstance(response.get("orders"), list)
            or len(response["orders"]) != 2
            or not isinstance(response.get("orderReports"), list)
            or len(response["orderReports"]) != 2
            or any(
                not isinstance(report, dict)
                or report.get("symbol") != symbol
                or report.get("side") != "SELL"
                or not isinstance(report.get("clientOrderId"), str)
                or not report["clientOrderId"]
                for report in response["orderReports"]
            )
            or any(
                not isinstance(order, dict)
                or order.get("symbol") != symbol
                or not isinstance(order.get("orderId"), int)
                or not isinstance(order.get("clientOrderId"), str)
                for order in response["orders"]
            )
        ):
            raise BinanceTestnetError(
                "Binance did not confirm both active exchange-managed exit orders."
            )
        order_client_ids = {order["clientOrderId"] for order in response["orders"]}
        report_client_ids = {
            report["clientOrderId"] for report in response["orderReports"]
        }
        if len(order_client_ids) != 2 or report_client_ids != order_client_ids:
            raise BinanceTestnetError(
                "Binance returned mismatched protective-exit order identifiers."
            )
        response["quantity"] = format(rounded_quantity, "f")
        response["stopPrice"] = format(rounded_stop, "f")
        response["takeProfitPrice"] = format(rounded_take_profit, "f")
        return response

    def symbol_rules(self, symbol):
        symbol = self.validate_symbol(symbol)
        result = self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
        symbols = result.get("symbols", [])
        if not symbols:
            raise BinanceTestnetError(
                f"{symbol} is not available on {self.ENVIRONMENT_NAME}."
            )

        symbol_data = symbols[0]
        if symbol_data.get("status") != "TRADING":
            raise BinanceTestnetError(
                f"{symbol} is not currently trading on {self.ENVIRONMENT_NAME}."
            )
        filters = {item["filterType"]: item for item in symbol_data.get("filters", [])}
        lot_size = filters.get("LOT_SIZE", {})
        price_filter = filters.get("PRICE_FILTER", {})
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        try:
            step_size = Decimal(lot_size["stepSize"])
            min_qty = Decimal(lot_size["minQty"])
            tick_size = Decimal(price_filter["tickSize"])
            min_notional = Decimal(notional.get("minNotional", "0"))
        except (KeyError, ValueError) as error:
            raise BinanceTestnetError(f"Trading rules for {symbol} are incomplete.") from error
        if (
            not step_size.is_finite()
            or not min_qty.is_finite()
            or not min_notional.is_finite()
            or not tick_size.is_finite()
            or step_size <= 0
            or tick_size <= 0
            or min_qty < 0
            or min_notional < 0
        ):
            raise BinanceTestnetError(f"Trading rules for {symbol} are invalid.")
        return {
            "step_size": step_size,
            "tick_size": tick_size,
            "min_qty": min_qty,
            "min_notional": min_notional,
            "base_asset": symbols[0]["baseAsset"],
            "quote_asset": symbols[0]["quoteAsset"],
        }

    def market_buy(self, symbol, quote_amount, client_order_id):
        symbol = self.validate_symbol(symbol)
        amount = Decimal(str(quote_amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if not amount.is_finite() or amount <= 0:
            raise BinanceTestnetError("Buy amount must be greater than zero.")
        return self._request(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": format(amount, "f"),
                "newOrderRespType": "FULL",
                "newClientOrderId": client_order_id,
            },
            signed=True,
        )

    def market_sell(
        self,
        symbol,
        quantity,
        step_size,
        min_qty,
        min_notional,
        last_price,
        client_order_id,
    ):
        symbol = self.validate_symbol(symbol)
        rounded_quantity = normalize_market_sell_quantity(
            quantity, step_size, min_qty, min_notional, last_price
        )
        return self._request(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": format(rounded_quantity, "f"),
                "newOrderRespType": "FULL",
                "newClientOrderId": client_order_id,
            },
            signed=True,
        )


class BinanceSpotLive(BinanceSpotTestnet):
    """Production Spot client, intentionally separate from the Testnet client."""

    BASE_URL = LIVE_BASE_URL
    ENVIRONMENT_NAME = "Binance Spot LIVE"
    IS_LIVE = True
