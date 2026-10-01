import json
import math
import os
import tempfile
import unittest
import urllib.parse
from decimal import Decimal
from unittest.mock import Mock, patch

from account_display import (
    calculate_unrealized_pnl,
    format_account_balances,
    format_account_profile,
    parse_account_balances,
)
from backtest import run_backtest, validate_backtest_settings
from credential_store import CredentialStore
from desktop_alerts import message_alert, result_alert_messages
from binance_permissions import validate_live_api_restrictions
from binance_testnet import (
    LIVE_BASE_URL,
    BinanceSpotLive,
    BinanceSpotTestnet,
    BinanceTestnetError,
    TESTNET_BASE_URL,
)
from local_ai import LocalAIError, OllamaAnalyzer, calculate_indicators
from instance_lock import InstanceLock, InstanceLockError
from ledger_storage import resolve_ledger_path
from market_scanner import (
    MarketScannerError,
    SpotMarketScanner,
    score_spot_candidate,
    select_top_usdt_spot_symbols,
)
from trade_ledger import LedgerError, TradeLedger
from trade_history import TradeHistoryError, parse_trade_history
from trading_engine import TradingEngine


def make_indicators():
    return {
        "last_price": 100.0,
        "ema20": 105.0,
        "ema50": 100.0,
        "rsi14": 55.0,
        "candle_count": 60,
    }


class TestAccountDisplay(unittest.TestCase):
    def test_calculates_estimated_unrealized_trade_pnl(self):
        self.assertAlmostEqual(calculate_unrealized_pnl(0.25, 100, 420), 5)

    def test_rejects_invalid_trade_pnl_values(self):
        with self.assertRaises(ValueError):
            calculate_unrealized_pnl(0.25, 100, float("nan"))

    def test_formats_available_binance_account_profile_fields(self):
        profile = format_account_profile(
            {
                "accountType": "SPOT",
                "canTrade": True,
                "permissions": ["SPOT", "MARGIN"],
            }
        )
        self.assertEqual(
            profile,
            "Account type: SPOT · Spot trading: enabled · "
            "Account permissions: MARGIN, SPOT",
        )

    def test_rejects_account_profile_without_trading_status(self):
        with self.assertRaises(ValueError):
            format_account_profile({"balances": []})

    def test_shows_read_only_api_key_trading_permission(self):
        profile = format_account_profile(
            {"canTrade": False},
            {
                "enableSpotAndMarginTrading": False,
            },
        )
        self.assertEqual(
            profile,
            "Spot trading: disabled · API-key Spot/Margin trading: disabled",
        )

    def test_formats_nonzero_free_and_locked_asset_balances(self):
        account = {
            "balances": [
                {"asset": "BTC", "free": "0.25", "locked": "0.1"},
                {"asset": "USDT", "free": "0", "locked": "0"},
            ]
        }
        balances = format_account_balances(account)
        self.assertEqual(balances, "BTC: free 0.25, locked 0.1")
        self.assertEqual(parse_account_balances(account), [("BTC", 0.25, 0.1)])

    def test_rejects_invalid_account_balance_response(self):
        with self.assertRaises(ValueError):
            format_account_balances({"balances": [{"asset": "BTC", "free": "NaN"}]})


class TestBacktest(unittest.TestCase):
    @staticmethod
    def candles(count=90, spike_index=None, stop_index=None):
        rows = []
        previous = 100.0
        for index in range(count):
            open_price = previous
            close = open_price + (0.12 if index % 2 == 0 else -0.08)
            high = max(open_price, close) + 0.1
            low = min(open_price, close) - 0.1
            if index == spike_index:
                high = max(open_price, close) + 7
            if index == stop_index:
                low = min(open_price, close) * 0.97
            rows.append(
                {
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "close_time": 1_700_000_000_000 + index * 3_600_000,
                }
            )
            previous = close
        return rows

    def test_proxy_backtest_trades_and_compares_buy_and_hold(self):
        result = run_backtest(
            self.candles(spike_index=65),
            starting_cash=1000,
            quote_per_trade=10,
            daily_drawdown_stop=5,
            fee_bps=10,
            slippage_bps=5,
        )
        self.assertGreaterEqual(result["closed_trades"], 1)
        self.assertGreaterEqual(result["take_profit_exits"], 1)
        self.assertEqual(result["candles"], 90)
        self.assertTrue(math.isfinite(result["return_pct"]))
        self.assertTrue(math.isfinite(result["buy_hold_return_pct"]))
        self.assertGreater(result["fees_paid_usdt"], 0)
        self.assertIn("not historical Ollama", result["strategy"])

    def test_drawdown_stop_halts_new_entries_while_exchange_exits_run(self):
        result = run_backtest(
            self.candles(count=80, stop_index=52),
            starting_cash=100,
            quote_per_trade=10,
            daily_drawdown_stop=0.1,
            stop_loss_pct=2,
            take_profit_pct=4,
            fee_bps=0,
            slippage_bps=0,
        )
        self.assertTrue(result["daily_drawdown_halted"])
        self.assertGreaterEqual(result["stop_exits"], 1)
        self.assertEqual(result["closed_trades"], 1)

    def test_backtest_rejects_invalid_settings_and_unordered_data(self):
        with self.assertRaises(ValueError):
            validate_backtest_settings(100, 10, 100, 2, 4, 10, 5)
        rows = self.candles()
        rows[5]["close_time"] = rows[4]["close_time"]
        with self.assertRaisesRegex(ValueError, "unordered OHLC"):
            run_backtest(rows)


class TestBinancePermissions(unittest.TestCase):
    def test_account_inspection_allows_read_only_key_when_restrictions_are_safe(self):
        validate_live_api_restrictions(
            {
                "enableReading": True,
                "enableWithdrawals": False,
                "ipRestrict": True,
                "enableSpotAndMarginTrading": False,
            }
        )

    def test_order_automation_requires_spot_trading_permission(self):
        restrictions = {
            "enableReading": True,
            "enableWithdrawals": False,
            "ipRestrict": True,
            "enableSpotAndMarginTrading": False,
        }
        with self.assertRaisesRegex(
            RuntimeError, "enableSpotAndMarginTrading expected True, received False"
        ):
            validate_live_api_restrictions(restrictions, require_trading=True)

    def test_account_connection_reports_missing_restriction_fields(self):
        with self.assertRaisesRegex(
            RuntimeError, "enableReading expected True, received 'missing'"
        ):
            validate_live_api_restrictions({})


class TestCredentialStore(unittest.TestCase):
    def test_saved_credentials_are_encrypted_and_round_trip(self):
        credentials = {
            "version": 1,
            "mode": "LIVE",
            "credentials": {
                "TESTNET": {"api_key": "", "api_secret": ""},
                "LIVE": {"api_key": "live-key-123", "api_secret": "live-secret-456"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(os.path.join(directory, "credentials.dpapi"))
            with patch("credential_store._crypt", side_effect=lambda data, protect: data[::-1]):
                store.save(credentials)
                with open(store.path, "rb") as file:
                    encrypted = file.read()
                self.assertNotIn(b"live-key-123", encrypted)
                self.assertNotIn(b"live-secret-456", encrypted)
                self.assertEqual(store.load(), credentials)
            store.clear()
            self.assertIsNone(store.load())


class TestAppDataLedgerMigration(unittest.TestCase):
    def test_first_user_data_ledger_preserves_legacy_live_risk_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source_dir = os.path.join(directory, "source")
            app_data_dir = os.path.join(directory, "user-data")
            os.makedirs(source_dir)
            legacy_path = os.path.join(source_dir, "trade_ledger_live.json")
            legacy_ledger = TradeLedger(legacy_path)
            legacy_ledger.observe_account_equity(250)
            original_data = legacy_ledger.data.copy()

            migrated_path = resolve_ledger_path(
                "LIVE", app_data_dir, source_dir
            )
            migrated = TradeLedger(migrated_path)
            self.assertEqual(migrated.data, original_data)
            self.assertTrue(os.path.isfile(migrated_path))


class TestTradeHistory(unittest.TestCase):
    @staticmethod
    def fill(trade_id, is_buyer, price, quantity, quote, fee="0", fee_asset="BNB"):
        return {
            "id": trade_id,
            "orderId": trade_id + 100,
            "time": 1_700_000_000_000 + trade_id,
            "isBuyer": is_buyer,
            "price": str(price),
            "qty": str(quantity),
            "quoteQty": str(quote),
            "commission": str(fee),
            "commissionAsset": fee_asset,
        }

    def test_calculates_fifo_realized_pnl_with_base_and_quote_fees(self):
        fills = [
            self.fill(1, True, 100, 1, 100, "0.1", "USDT"),
            self.fill(2, False, 120, "0.5", 60, "0.06", "USDT"),
            self.fill(3, False, 80, "0.5", 40),
        ]
        history = parse_trade_history(fills, "BTC", "USDT")
        self.assertIsNone(history[0]["realized_pnl"])
        self.assertEqual(history[1]["realized_pnl"], Decimal("9.89"))
        self.assertEqual(history[2]["realized_pnl"], Decimal("-10.05"))

    def test_marks_missing_cost_basis_or_third_asset_fee_as_unknown(self):
        missing_basis = parse_trade_history(
            [self.fill(1, False, 120, "0.5", 60)], "BTC", "USDT"
        )
        self.assertIsNone(missing_basis[0]["realized_pnl"])

        third_asset_fee = parse_trade_history(
            [
                self.fill(1, True, 100, 1, 100, "0.01", "BNB"),
                self.fill(2, False, 120, 1, 120),
            ],
            "BTC",
            "USDT",
        )
        self.assertIsNone(third_asset_fee[1]["realized_pnl"])

    def test_rejects_malformed_exchange_fill(self):
        with self.assertRaises(TradeHistoryError):
            parse_trade_history([{"id": 1}], "BTC", "USDT")


class TestDesktopAlerts(unittest.TestCase):
    def test_reports_non_hold_signals_fills_and_risk_stops(self):
        messages = result_alert_messages(
            {
                "symbol": "BTCUSDT",
                "decision": {"action": "BUY", "confidence": 0.8},
                "order": {"executedQty": "0.02", "status": "FILLED"},
                "halt_automation": True,
                "message": "Daily loss limit reached.",
            }
        )
        self.assertEqual(len(messages), 3)
        self.assertIn("BUY signal", messages[0])
        self.assertIn("Fill received", messages[1])
        self.assertIn("RISK STOP", messages[2])

    def test_respects_global_alert_toggle_and_reconciled_fill_messages(self):
        self.assertEqual(
            result_alert_messages(
                {"decision": {"action": "BUY", "confidence": 0.9}}, enabled=False
            ),
            [],
        )
        self.assertIn(
            "Order fill reconciled",
            message_alert("Reconciled FILLED buy order from Binance Spot LIVE."),
        )
        self.assertIn(
            "RISK STOP",
            message_alert("Automation halted for safety; no retry will be attempted."),
        )


class TestIndicatorsAndLocalAI(unittest.TestCase):
    def test_indicators_require_sufficient_valid_candles(self):
        with self.assertRaises(ValueError):
            calculate_indicators([{"close": 1.0}] * 49)
        with self.assertRaises(ValueError):
            calculate_indicators([{"close": 0.0}] * 60)

    def test_indicators_calculate_trending_prices(self):
        candles = [{"close": 100 + i * 0.1} for i in range(60)]
        indicators = calculate_indicators(candles)
        self.assertEqual(indicators["candle_count"], 60)
        self.assertGreater(indicators["ema20"], indicators["ema50"])
        self.assertGreater(indicators["rsi14"], 50)

    def test_analyzer_refuses_remote_hosts(self):
        with self.assertRaises(ValueError):
            OllamaAnalyzer(base_url="http://localhost.attacker.example:11434")
        with self.assertRaises(ValueError):
            OllamaAnalyzer(base_url="https://localhost:11434")

    def test_analyzer_parses_bounded_json_decision(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(
            {
                "message": {
                    "content": json.dumps(
                        {"action": "BUY", "confidence": 0.91, "reason": "Trend is positive."}
                    )
                }
            }
        ).encode("utf-8")
        with patch("local_ai.urllib.request.urlopen", return_value=response) as urlopen:
            decision = OllamaAnalyzer().analyze("BTCUSDT", make_indicators())
        self.assertEqual(decision["action"], "BUY")
        self.assertEqual(decision["confidence"], 0.91)
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.startswith("http://127.0.0.1:11434/"))
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 600)

    def test_analyzer_explains_timeout_while_local_model_loads(self):
        with patch("local_ai.urllib.request.urlopen", side_effect=TimeoutError):
            with self.assertRaisesRegex(
                LocalAIError, "model may still be loading.*try Analyze again"
            ):
                OllamaAnalyzer().analyze("BTCUSDT", make_indicators())

    def test_invalid_ai_action_fails_closed(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(
            {
                "message": {
                    "content": json.dumps(
                        {"action": "YOLO", "confidence": 1, "reason": "Trade everything."}
                    )
                }
            }
        ).encode("utf-8")
        with patch("local_ai.urllib.request.urlopen", return_value=response):
            with self.assertRaises(LocalAIError):
                OllamaAnalyzer().analyze("BTCUSDT", make_indicators())


class TestBinanceSpotTestnetClient(unittest.TestCase):
    def test_configured_endpoint_is_testnet(self):
        self.assertEqual(TESTNET_BASE_URL, "https://testnet.binance.vision")
        self.assertNotIn("api.binance.com", TESTNET_BASE_URL)
        self.assertEqual(BinanceSpotTestnet.BASE_URL, TESTNET_BASE_URL)
        self.assertFalse(BinanceSpotTestnet.IS_LIVE)

    def test_live_client_has_separate_production_endpoint(self):
        self.assertEqual(LIVE_BASE_URL, "https://api.binance.com")
        self.assertEqual(BinanceSpotLive.BASE_URL, LIVE_BASE_URL)
        self.assertTrue(BinanceSpotLive.IS_LIVE)
        self.assertNotEqual(BinanceSpotLive.BASE_URL, BinanceSpotTestnet.BASE_URL)

    def test_rejects_malformed_symbol(self):
        with self.assertRaises(BinanceTestnetError):
            BinanceSpotTestnet.validate_symbol("https://attacker.invalid")

    def test_signed_account_request_has_api_key_and_signature(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"balances":[]}'
        client = BinanceSpotTestnet("sandbox-key", "sandbox-secret")
        with patch("binance_testnet.urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(client.account(), {"balances": []})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url.split("/api/")[0], TESTNET_BASE_URL)
        self.assertEqual(request.get_header("X-mbx-apikey"), "sandbox-key")
        self.assertIn("signature=", request.full_url)

    def test_my_trades_uses_signed_read_only_endpoint_and_caps_limit(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"[]"
        client = BinanceSpotLive("sandbox-key", "sandbox-secret")
        with patch("binance_testnet.urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(client.my_trades("BTCUSDT", limit=2000), [])
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.startswith(LIVE_BASE_URL + "/api/v3/myTrades?"))
        self.assertIn("symbol=BTCUSDT", request.full_url)
        self.assertIn("limit=1000", request.full_url)
        self.assertIn("signature=", request.full_url)

    def test_ticker_prices_batches_requested_symbols_in_one_public_request(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = (
            b'[{"symbol":"BTCUSDT","price":"101.25"},'
            b'{"symbol":"ETHUSDT","price":"202.5"}]'
        )
        client = BinanceSpotLive()
        with patch(
            "binance_testnet.urllib.request.urlopen", return_value=response
        ) as urlopen:
            prices = client.ticker_prices(["BTCUSDT", "ETHUSDT", "BTCUSDT"])
        request = urlopen.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        self.assertTrue(request.full_url.startswith(LIVE_BASE_URL + "/api/v3/ticker/price?"))
        self.assertEqual(
            json.loads(query["symbols"][0]), ["BTCUSDT", "ETHUSDT"]
        )
        self.assertEqual(prices, {"BTCUSDT": Decimal("101.25"), "ETHUSDT": Decimal("202.5")})

    def test_ticker_prices_rejects_unrequested_or_invalid_quotes(self):
        client = BinanceSpotLive()
        with patch.object(
            client,
            "_request",
            return_value=[{"symbol": "BNBUSDT", "price": "10"}],
        ):
            with self.assertRaisesRegex(BinanceTestnetError, "unexpected live ticker"):
                client.ticker_prices(["BTCUSDT", "ETHUSDT"])
        with patch.object(
            client,
            "_request",
            return_value={"symbol": "BTCUSDT", "price": "NaN"},
        ):
            with self.assertRaisesRegex(BinanceTestnetError, "invalid ticker price"):
                client.ticker_prices(["BTCUSDT"])

    def test_live_order_is_sent_only_to_production_client_host(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"orderId":1}'
        client = BinanceSpotLive("live-key", "live-secret")
        with patch("binance_testnet.urllib.request.urlopen", return_value=response) as urlopen:
            client.market_buy("BTCUSDT", 3, "ast_unit_test")
        self.assertTrue(urlopen.call_args.args[0].full_url.startswith(LIVE_BASE_URL))

    def test_market_oco_sell_rounds_prices_to_symbol_tick_and_uses_current_endpoint(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = (
            b'{"symbol":"BTCUSDT","orderListId":5,"listClientOrderId":"ax_unit",'
            b'"listStatusType":"EXEC_STARTED","listOrderStatus":"EXECUTING",'
            b'"orders":[{"symbol":"BTCUSDT","orderId":1,"clientOrderId":"tp"},'
            b'{"symbol":"BTCUSDT","orderId":2,"clientOrderId":"sl"}],'
            b'"orderReports":[{"symbol":"BTCUSDT","side":"SELL","clientOrderId":"tp"},'
            b'{"symbol":"BTCUSDT","side":"SELL","clientOrderId":"sl"}]}'
        )
        client = BinanceSpotLive("live-key", "live-secret")
        with patch("binance_testnet.urllib.request.urlopen", return_value=response) as urlopen:
            result = client.market_oco_sell(
                "BTCUSDT", "0.11239", "0.0001", "0.1", "0.0001", "5",
                "100", "98.01", "104.01", "ax_unit"
            )
        request = urlopen.call_args.args[0]
        self.assertTrue(
            request.full_url == LIVE_BASE_URL + "/api/v3/orderList/oco"
        )
        body = request.data.decode("ascii")
        self.assertIn("quantity=0.1123", body)
        self.assertIn("aboveStopPrice=104.1", body)
        self.assertIn("belowStopPrice=98.0", body)
        self.assertEqual(result["takeProfitPrice"], "104.1")

    def test_market_oco_sell_accepts_an_immediately_completed_exit_list(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = (
            b'{"symbol":"BTCUSDT","orderListId":5,"listClientOrderId":"ax_unit",'
            b'"listStatusType":"ALL_DONE","listOrderStatus":"ALL_DONE",'
            b'"orders":[{"symbol":"BTCUSDT","orderId":1,"clientOrderId":"tp"},'
            b'{"symbol":"BTCUSDT","orderId":2,"clientOrderId":"sl"}],'
            b'"orderReports":[{"symbol":"BTCUSDT","side":"SELL","clientOrderId":"tp"},'
            b'{"symbol":"BTCUSDT","side":"SELL","clientOrderId":"sl"}]}'
        )
        client = BinanceSpotLive("live-key", "live-secret")
        with patch("binance_testnet.urllib.request.urlopen", return_value=response):
            result = client.market_oco_sell(
                "BTCUSDT", "0.11239", "0.0001", "0.1", "0.0001", "5",
                "100", "98.01", "104.01", "ax_unit"
            )
        self.assertEqual(result["listStatusType"], "ALL_DONE")

    def test_order_list_query_and_cancel_use_current_signed_endpoints(self):
        client = BinanceSpotLive("live-key", "live-secret")
        order_list = {
            "symbol": "BTCUSDT",
            "orderListId": 5,
            "listClientOrderId": "ax_exit",
            "listStatusType": "EXEC_STARTED",
            "listOrderStatus": "EXECUTING",
            "orders": [
                {"symbol": "BTCUSDT", "orderId": 11, "clientOrderId": "ax_tp"},
                {"symbol": "BTCUSDT", "orderId": 12, "clientOrderId": "ax_sl"},
            ],
        }
        order_reports = [
            {
                "symbol": "BTCUSDT",
                "orderId": 11,
                "clientOrderId": "ax_tp",
                "side": "SELL",
                "status": "NEW",
                "executedQty": "0",
                "cummulativeQuoteQty": "0",
            },
            {
                "symbol": "BTCUSDT",
                "orderId": 12,
                "clientOrderId": "ax_sl",
                "side": "SELL",
                "status": "NEW",
                "executedQty": "0",
                "cummulativeQuoteQty": "0",
            },
        ]
        with patch.object(
            client,
            "_request",
            side_effect=[order_list, *order_reports, {}],
        ) as request:
            status = client.order_list_status("ax_exit")
            self.assertEqual(
                request.call_args_list[0].args[:2], ("GET", "/api/v3/orderList")
            )
            self.assertEqual(
                request.call_args_list[0].args[2],
                {"origClientOrderId": "ax_exit"},
            )
            self.assertEqual(status["orderReports"], order_reports)
            self.assertEqual(
                request.call_args_list[1].args[:3],
                (
                    "GET",
                    "/api/v3/order",
                    {"symbol": "BTCUSDT", "origClientOrderId": "ax_tp"},
                ),
            )
            self.assertEqual(
                request.call_args_list[2].args[:3],
                (
                    "GET",
                    "/api/v3/order",
                    {"symbol": "BTCUSDT", "origClientOrderId": "ax_sl"},
                ),
            )
            client.cancel_order_list(5)
            self.assertEqual(
                request.call_args_list[3].args[:2], ("DELETE", "/api/v3/orderList")
            )
            self.assertEqual(
                request.call_args_list[3].args[2],
                {"orderListId": 5},
            )

    def test_order_list_query_rejects_mismatched_child_order_details(self):
        client = BinanceSpotLive("live-key", "live-secret")
        order_list = {
            "symbol": "BTCUSDT",
            "orderListId": 5,
            "listClientOrderId": "ax_exit",
            "orders": [
                {"symbol": "BTCUSDT", "orderId": 11, "clientOrderId": "ax_tp"},
                {"symbol": "BTCUSDT", "orderId": 12, "clientOrderId": "ax_sl"},
            ],
        }
        mismatched_report = {
            "symbol": "BTCUSDT",
            "orderId": 99,
            "clientOrderId": "ax_tp",
        }
        valid_report = {
            "symbol": "BTCUSDT",
            "orderId": 12,
            "clientOrderId": "ax_sl",
        }
        with patch.object(
            client,
            "_request",
            side_effect=[order_list, mismatched_report, valid_report],
        ):
            with self.assertRaisesRegex(BinanceTestnetError, "do not match"):
                client.order_list_status("ax_exit")

    def test_order_list_cancel_rejects_invalid_exchange_id(self):
        client = BinanceSpotLive("live-key", "live-secret")
        for invalid_id in (True, -1, "5", 5.0):
            with self.subTest(invalid_id=invalid_id):
                with self.assertRaisesRegex(BinanceTestnetError, "order-list ID"):
                    client.cancel_order_list(invalid_id)

    def test_historical_candle_request_is_capped_at_exchange_limit(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = (
            b'[[0,"100","101","99","100.5","20",3600000]]'
        )
        client = BinanceSpotLive()
        with patch("binance_testnet.urllib.request.urlopen", return_value=response) as urlopen:
            candles = client.klines("BTCUSDT", interval="1h", limit=5000)
        self.assertEqual(len(candles), 1)
        self.assertIn("limit=1000", urlopen.call_args.args[0].full_url)

    def test_market_oco_sell_rejects_unconfirmed_exchange_response(self):
        client = BinanceSpotLive("live-key", "live-secret")
        client._request = Mock(return_value={"symbol": "BTCUSDT"})
        with self.assertRaisesRegex(BinanceTestnetError, "did not confirm"):
            client.market_oco_sell(
                "BTCUSDT", "0.1", "0.0001", "0.01", "0.0001", "5",
                "100", "98", "104", "ax_unit"
            )

    def test_market_oco_sell_rejects_mismatched_order_identifiers(self):
        client = BinanceSpotLive("live-key", "live-secret")
        client._request = Mock(
            return_value={
                "symbol": "BTCUSDT",
                "orderListId": 5,
                "listClientOrderId": "ax_unit",
                "listStatusType": "EXEC_STARTED",
                "listOrderStatus": "EXECUTING",
                "orders": [
                    {"symbol": "BTCUSDT", "orderId": 1, "clientOrderId": "tp"},
                    {"symbol": "BTCUSDT", "orderId": 2, "clientOrderId": "sl"},
                ],
                "orderReports": [
                    {"symbol": "BTCUSDT", "side": "SELL", "clientOrderId": "tp"},
                    {"symbol": "BTCUSDT", "side": "SELL", "clientOrderId": "other"},
                ],
            }
        )
        with self.assertRaisesRegex(BinanceTestnetError, "mismatched"):
            client.market_oco_sell(
                "BTCUSDT", "0.1", "0.0001", "0.01", "0.0001", "5",
                "100", "98", "104", "ax_unit"
            )

    def test_total_equity_values_free_locked_and_crypto_assets(self):
        client = BinanceSpotLive()
        account = {
            "balances": [
                {"asset": "USDT", "free": "100", "locked": "10"},
                {"asset": "BTC", "free": "0.001", "locked": "0.001"},
            ]
        }
        with patch.object(client, "ticker_price", return_value=50_000):
            self.assertEqual(client.account_equity_usdt(account), 210)

    def test_total_equity_fails_closed_for_unpriced_asset(self):
        client = BinanceSpotLive()
        account = {
            "balances": [
                {"asset": "XYZ", "free": "1", "locked": "0"},
            ]
        }
        with patch.object(
            client,
            "ticker_price",
            side_effect=BinanceTestnetError("ticker unavailable"),
        ):
            with self.assertRaises(BinanceTestnetError):
                client.account_equity_usdt(account)


class TestSpotMarketScanner(unittest.TestCase):
    @staticmethod
    def _symbol(symbol, base_asset, quote_asset="USDT", enabled=True):
        return {
            "symbol": symbol,
            "baseAsset": base_asset,
            "quoteAsset": quote_asset,
            "status": "TRADING",
            "isSpotTradingAllowed": enabled,
        }

    @staticmethod
    def _candles(base_price=100):
        return [
            {
                "close": base_price + index * 0.1,
                "volume": 100 if index < 57 else 200,
                "close_time": 999,
            }
            for index in range(60)
        ]

    def test_universe_selects_top_liquid_regular_usdt_spot_pairs(self):
        exchange_info = {
            "symbols": [
                self._symbol("BTCUSDT", "BTC"),
                self._symbol("ETHUSDT", "ETH"),
                self._symbol("USDCUSDT", "USDC"),
                self._symbol("BTCUPUSDT", "BTCUP"),
                self._symbol("BTCBUSD", "BTC", "BUSD"),
                self._symbol("HALTEDUSDT", "HALTED", enabled=False),
            ]
        }
        tickers = [
            {"symbol": "BTCUSDT", "quoteVolume": "1000"},
            {"symbol": "ETHUSDT", "quoteVolume": "2500"},
            {"symbol": "USDCUSDT", "quoteVolume": "9000"},
            {"symbol": "BTCUPUSDT", "quoteVolume": "8000"},
            {"symbol": "BTCBUSD", "quoteVolume": "7000"},
            {"symbol": "HALTEDUSDT", "quoteVolume": "6000"},
        ]
        self.assertEqual(
            select_top_usdt_spot_symbols(exchange_info, tickers, limit=1),
            [("ETHUSDT", 2500.0)],
        )

    def test_candidate_requires_uptrend_non_overbought_rsi_and_positive_momentum(self):
        candles = self._candles()
        indicators = {
            "last_price": 106,
            "ema20": 105,
            "ema50": 104,
            "rsi14": 54,
            "candle_count": 60,
        }
        with patch("market_scanner.calculate_indicators", return_value=indicators):
            candidate = score_spot_candidate("BTCUSDT", candles, 100_000)
        self.assertTrue(candidate["eligible"])
        self.assertGreater(candidate["score"], 0)
        self.assertEqual(candidate["status"], "CANDIDATE")

        with patch(
            "market_scanner.calculate_indicators",
            return_value={**indicators, "rsi14": 75},
        ):
            filtered = score_spot_candidate("BTCUSDT", candles, 100_000)
        self.assertFalse(filtered["eligible"])
        self.assertIn("RSI filter", filtered["status"])

    def test_scan_selects_highest_scoring_candidate_and_refreshes_universe_once(self):
        class Client:
            def __init__(self):
                self.exchange_info_calls = 0
                self.ticker_calls = 0
                self.candle_symbols = []

            def spot_exchange_info(self):
                self.exchange_info_calls += 1
                return {
                    "symbols": [
                        TestSpotMarketScanner._symbol("BTCUSDT", "BTC"),
                        TestSpotMarketScanner._symbol("ETHUSDT", "ETH"),
                    ]
                }

            def ticker_24hr(self):
                self.ticker_calls += 1
                return [
                    {"symbol": "BTCUSDT", "quoteVolume": "1000"},
                    {"symbol": "ETHUSDT", "quoteVolume": "2000"},
                ]

            def klines(self, symbol, interval, limit):
                self.candle_symbols.append((symbol, interval, limit))
                return TestSpotMarketScanner._candles(
                    100 if symbol == "BTCUSDT" else 200
                )

        client = Client()
        btc_indicators = {
            "last_price": 106,
            "ema20": 105,
            "ema50": 104.9,
            "rsi14": 54,
            "candle_count": 60,
        }
        eth_indicators = {
            "last_price": 206,
            "ema20": 205,
            "ema50": 203,
            "rsi14": 54,
            "candle_count": 60,
        }
        with patch(
            "market_scanner.calculate_indicators",
            side_effect=[eth_indicators, btc_indicators, eth_indicators, btc_indicators],
        ):
            scanner = SpotMarketScanner(client)
            result = scanner.scan("1m", now_ms=1000)
            scanner.scan("1m", now_ms=1000)

        self.assertEqual(result["candidate"]["symbol"], "ETHUSDT")
        self.assertEqual(client.exchange_info_calls, 1)
        self.assertEqual(client.ticker_calls, 1)
        self.assertEqual(len(client.candle_symbols), 4)

    def test_invalid_exchange_market_data_fails_explicitly(self):
        with self.assertRaises(MarketScannerError):
            select_top_usdt_spot_symbols({}, [], limit=20)


class TestSingleInstanceLock(unittest.TestCase):
    def test_second_process_cannot_acquire_same_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "app.lock")
            first = InstanceLock(path)
            second = InstanceLock(path)
            first.acquire()
            try:
                with self.assertRaises(InstanceLockError):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()


class TestTradeLedger(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp_dir.name, "trade_ledger.json")
        self.ledger = TradeLedger(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_round_trip_and_daily_loss_stop(self):
        client_id = "taskflow_test_buy"
        self.ledger.reserve_order("BTCUSDT", "BUY", client_id)
        self.ledger.record_buy(
            "BTCUSDT",
            {
                "clientOrderId": client_id,
                "executedQty": "1",
                "cummulativeQuoteQty": "10",
                "fills": [],
            },
            "BTC",
            "USDT",
        )
        self.assertIsNone(self.ledger.pending_order)
        self.assertEqual(self.ledger.position("BTCUSDT")["quantity"], 1)

        sell_id = "taskflow_test_sell"
        self.ledger.reserve_order("BTCUSDT", "SELL", sell_id)
        realized = self.ledger.record_sell(
            "BTCUSDT",
            {
                "clientOrderId": sell_id,
                "executedQty": "1",
                "cummulativeQuoteQty": "4",
                "fills": [],
            },
            "BTC",
            "USDT",
        )
        self.assertEqual(realized, -6)
        self.assertIn("Daily realized-loss limit", self.ledger.can_buy("BTCUSDT", 10, 100, 10, 5))

        loaded = TradeLedger(self.path)
        self.assertEqual(loaded.realized_pnl_today(), -6)
        self.assertEqual(loaded.position("BTCUSDT")["quantity"], 0)

    def test_unresolved_order_blocks_new_order(self):
        self.ledger.reserve_order("BTCUSDT", "BUY", "unknown-order")
        with self.assertRaises(LedgerError):
            self.ledger.reserve_order("BTCUSDT", "BUY", "duplicate-order")
        with self.assertRaises(LedgerError):
            TradeLedger(self.path).reserve_order("BTCUSDT", "BUY", "duplicate-order")

    def test_only_one_open_managed_position_across_symbols(self):
        client_id = "single-position"
        self.ledger.reserve_order("BTCUSDT", "BUY", client_id)
        self.ledger.record_buy(
            "BTCUSDT",
            {
                "clientOrderId": client_id,
                "executedQty": "0.1",
                "cummulativeQuoteQty": "10",
                "fills": [],
            },
            "BTC",
            "USDT",
        )
        message = self.ledger.can_buy("ETHUSDT", 10, 100, 10, 5)
        self.assertIn("only one position", message)

    def test_failed_utc_day_reset_preserves_loss_state(self):
        self.ledger.data["utc_day"] = "2000-01-01"
        self.ledger.data["realized_pnl"] = -4.0
        with patch.object(self.ledger, "_save", side_effect=LedgerError("disk full")):
            with self.assertRaises(LedgerError):
                self.ledger.realized_pnl_today()
        self.assertEqual(self.ledger.data["utc_day"], "2000-01-01")
        self.assertEqual(self.ledger.data["realized_pnl"], -4.0)

    def test_live_ledger_refuses_api_key_switch(self):
        first = "a" * 64
        second = "b" * 64
        self.ledger.bind_account(first)
        with self.assertRaises(LedgerError):
            self.ledger.bind_account(second)
        self.assertEqual(self.ledger.data["account_fingerprint"], first)

    def test_failed_persist_does_not_leave_false_pending_intent(self):
        with patch.object(self.ledger, "_save", side_effect=LedgerError("disk full")):
            with self.assertRaises(LedgerError):
                self.ledger.reserve_order("BTCUSDT", "BUY", "not-saved")
        self.assertIsNone(self.ledger.pending_order)

    def test_corrupt_ledger_fails_closed(self):
        with open(self.path, "w", encoding="utf-8") as file:
            file.write("{not json")
        with self.assertRaises(LedgerError):
            TradeLedger(self.path)

    def test_buy_fee_in_base_is_removed_from_managed_quantity(self):
        client_id = "buy-with-base-fee"
        self.ledger.reserve_order("BTCUSDT", "BUY", client_id)
        position = self.ledger.record_buy(
            "BTCUSDT",
            {
                "clientOrderId": client_id,
                "executedQty": "0.1",
                "cummulativeQuoteQty": "10",
                "fills": [{"commission": "0.001", "commissionAsset": "BTC"}],
            },
            "BTC",
            "USDT",
        )
        self.assertAlmostEqual(position["quantity"], 0.099)

    def test_v1_ledger_migrates_open_position_as_unprotected(self):
        data = {
            "version": 1,
            "utc_day": TradeLedger._today(),
            "realized_pnl": 0,
            "positions": {"BTCUSDT": {"quantity": 0.1, "cost_quote": 10}},
            "trades": [],
            "pending_order": None,
            "equity_day": None,
            "equity_start_usdt": None,
            "equity_high_water_usdt": None,
            "account_fingerprint": None,
        }
        with open(self.path, "w", encoding="utf-8") as file:
            json.dump(data, file)
        migrated = TradeLedger(self.path)
        self.assertEqual(migrated.data["version"], 2)
        self.assertEqual(
            migrated.position("BTCUSDT")["protection"]["status"], "unprotected"
        )
        with open(self.path, "r", encoding="utf-8") as file:
            self.assertEqual(json.load(file)["version"], 2)

    def test_cumulative_exchange_exit_fill_is_applied_exactly_once(self):
        self.ledger.reserve_order("BTCUSDT", "BUY", "exit-ledger-buy")
        self.ledger.record_buy(
            "BTCUSDT",
            {
                "clientOrderId": "exit-ledger-buy",
                "executedQty": "1",
                "cummulativeQuoteQty": "100",
                "fills": [],
            },
            "BTC",
            "USDT",
        )
        self.ledger.set_protection(
            "BTCUSDT",
            {
                "status": "active",
                "client_order_id": "exit-list",
                "reported_fills": {},
            },
        )
        self.assertEqual(
            self.ledger.record_protection_report("BTCUSDT", "exit-leg", 0.4, 44),
            0.4,
        )
        self.assertEqual(
            self.ledger.record_protection_report("BTCUSDT", "exit-leg", 0.4, 44),
            0,
        )
        self.assertAlmostEqual(self.ledger.position("BTCUSDT")["quantity"], 0.6)
        self.assertAlmostEqual(self.ledger.realized_pnl_today(), 4)

    def test_sell_fee_in_base_is_removed_from_position(self):
        buy_id = "buy-before-sell-fee"
        self.ledger.reserve_order("BTCUSDT", "BUY", buy_id)
        self.ledger.record_buy(
            "BTCUSDT",
            {
                "clientOrderId": buy_id,
                "executedQty": "0.1",
                "cummulativeQuoteQty": "10",
                "fills": [],
            },
            "BTC",
            "USDT",
        )
        sell_id = "sell-with-base-fee"
        self.ledger.reserve_order("BTCUSDT", "SELL", sell_id)
        self.ledger.record_sell(
            "BTCUSDT",
            {
                "clientOrderId": sell_id,
                "executedQty": "0.09",
                "cummulativeQuoteQty": "9",
                "fills": [{"commission": "0.001", "commissionAsset": "BTC"}],
            },
            "BTC",
            "USDT",
        )
        self.assertAlmostEqual(self.ledger.position("BTCUSDT")["quantity"], 0.009)


class FakeClient:
    ENVIRONMENT_NAME = "Binance Spot Testnet"

    def __init__(self):
        self.buy_calls = []
        self.sell_calls = []
        self.last_symbol = "BTCUSDT"

    @staticmethod
    def validate_symbol(symbol):
        return symbol

    def klines(self, _symbol, interval, limit):
        self.requested_candles = (interval, limit)
        return [{"close_time": 0, "close": 100.0}] * 60

    def symbol_rules(self, _symbol):
        from decimal import Decimal

        return {
            "step_size": Decimal("0.0001"),
            "tick_size": Decimal("0.01"),
            "min_qty": Decimal("0.0001"),
            "min_notional": Decimal("5"),
            "base_asset": "BTC",
            "quote_asset": "USDT",
        }

    def account(self):
        return {"balances": [{"asset": "USDT", "free": "100"}, {"asset": "BTC", "free": "1"}]}

    def market_buy(self, symbol, quote_amount, client_order_id):
        self.buy_calls.append((symbol, quote_amount, client_order_id))
        return {
            "clientOrderId": client_order_id,
            "orderId": 45,
            "side": "BUY",
            "status": "FILLED",
            "executedQty": "0.1",
            "cummulativeQuoteQty": str(quote_amount),
            "fills": [],
        }

    def ticker_price(self, _symbol):
        return Decimal("100")

    def open_orders(self, _symbol):
        return []

    def open_order_lists(self):
        return []

    def market_oco_sell(
        self,
        symbol,
        quantity,
        _step_size,
        _tick_size,
        _min_qty,
        _min_notional,
        _last_price,
        stop_price,
        take_profit_price,
        list_client_order_id,
    ):
        self.last_symbol = symbol
        reports = [
            {
                "symbol": symbol,
                "clientOrderId": f"{list_client_order_id}_tp",
                "side": "SELL",
                "status": "NEW",
                "executedQty": "0",
                "cummulativeQuoteQty": "0",
            },
            {
                "symbol": symbol,
                "clientOrderId": f"{list_client_order_id}_sl",
                "side": "SELL",
                "status": "NEW",
                "executedQty": "0",
                "cummulativeQuoteQty": "0",
            },
        ]
        self.current_order_list = {
            "symbol": symbol,
            "listClientOrderId": list_client_order_id,
            "orderListId": 88,
            "listStatusType": "EXEC_STARTED",
            "listOrderStatus": "EXECUTING",
            "orders": [
                {
                    "symbol": symbol,
                    "orderId": 89,
                    "clientOrderId": reports[0]["clientOrderId"],
                },
                {
                    "symbol": symbol,
                    "orderId": 90,
                    "clientOrderId": reports[1]["clientOrderId"],
                },
            ],
            "orderReports": reports,
        }
        return {
            **self.current_order_list,
            "quantity": str(quantity),
            "stopPrice": str(stop_price),
            "takeProfitPrice": str(take_profit_price),
        }

    def order_list_status(self, list_client_order_id):
        if not hasattr(self, "current_order_list"):
            symbol = self.last_symbol
            self.current_order_list = {
                "symbol": symbol,
                "listClientOrderId": list_client_order_id,
                "orderListId": 88,
                "listStatusType": "EXEC_STARTED",
                "listOrderStatus": "EXECUTING",
                "orders": [
                    {
                        "symbol": symbol,
                        "orderId": 89,
                        "clientOrderId": f"{list_client_order_id}_tp",
                    },
                    {
                        "symbol": symbol,
                        "orderId": 90,
                        "clientOrderId": f"{list_client_order_id}_sl",
                    },
                ],
                "orderReports": [
                    {
                        "symbol": symbol,
                        "clientOrderId": f"{list_client_order_id}_tp",
                        "side": "SELL",
                        "status": "NEW",
                        "executedQty": "0",
                        "cummulativeQuoteQty": "0",
                    },
                    {
                        "symbol": symbol,
                        "clientOrderId": f"{list_client_order_id}_sl",
                        "side": "SELL",
                        "status": "NEW",
                        "executedQty": "0",
                        "cummulativeQuoteQty": "0",
                    },
                ],
            }
        return self.current_order_list

    def cancel_order_list(self, order_list_id):
        current = self.current_order_list
        if current.get("orderListId") != order_list_id:
            raise RuntimeError("Exchange order-list ID did not match.")
        current["listStatusType"] = "ALL_DONE"
        current["listOrderStatus"] = "ALL_DONE"
        for report in current["orderReports"]:
            if report["status"] == "NEW":
                report["status"] = "CANCELED"
        return current

    def market_sell(self, symbol, quantity, *args):
        client_order_id = args[-1]
        self.sell_calls.append((symbol, quantity, client_order_id))
        return {
            "clientOrderId": client_order_id,
            "orderId": 46,
            "side": "SELL",
            "status": "FILLED",
            "executedQty": str(quantity),
            "cummulativeQuoteQty": "11",
            "fills": [],
        }

    def order_status(self, symbol, client_order_id):
        return {
            "symbol": symbol,
            "clientOrderId": client_order_id,
            "status": "FILLED",
            "executedQty": "0.1",
            "cummulativeQuoteQty": "10",
        }

    def cancel_order(self, symbol, client_order_id):
        return {
            "symbol": symbol,
            "clientOrderId": client_order_id,
            "status": "CANCELED",
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
        }


class FakeLiveClient(FakeClient):
    IS_LIVE = True
    ENVIRONMENT_NAME = "Binance Spot LIVE"

    def __init__(self):
        super().__init__()
        self.equity = 1000.0

    def api_key_restrictions(self):
        return {
            "enableReading": True,
            "enableSpotAndMarginTrading": True,
            "enableWithdrawals": False,
            "ipRestrict": True,
        }

    def account(self):
        return {
            "canTrade": True,
            "balances": [
                {"asset": "USDT", "free": "100", "locked": "0"},
                {"asset": "BTC", "free": "1", "locked": "0"},
            ],
        }

    def account_equity_usdt(self, account=None):
        return self.equity


class FakeAnalyzer:
    def __init__(self, action, confidence=0.95):
        self.action = action
        self.confidence = confidence

    def analyze(self, _symbol, _indicators):
        return {
            "action": self.action,
            "confidence": self.confidence,
            "reason": "Unit test decision.",
        }


class TestTradingEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ledger = TradeLedger(os.path.join(self.temp_dir.name, "ledger.json"))
        self.client = FakeClient()
        self.engine = TradingEngine(
            self.client,
            FakeAnalyzer("BUY"),
            self.ledger,
            max_trade_quote=10,
            daily_loss_limit=5,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("trading_engine.calculate_indicators", return_value=make_indicators())
    def test_analysis_mode_never_sends_order(self, _indicators):
        result = self.engine.run_cycle(execute_orders=False)
        self.assertEqual(result["decision"]["action"], "BUY")
        self.assertIsNone(result["order"])
        self.assertEqual(self.client.buy_calls, [])

    @patch("trading_engine.calculate_indicators", return_value=make_indicators())
    def test_engine_uses_selected_trade_interval(self, _indicators):
        self.engine.interval = "5m"
        self.engine.run_cycle(execute_orders=False)
        self.assertEqual(self.client.requested_candles, ("5m", 120))

    @patch("trading_engine.calculate_indicators", return_value=make_indicators())
    def test_automation_skips_duplicate_closed_candle(self, _indicators):
        first = self.engine.run_cycle(execute_orders=True)
        self.assertEqual(first["order"]["side"], "BUY")
        second = self.engine.run_cycle(execute_orders=True)
        self.assertEqual(second["decision"]["action"], "HOLD")
        self.assertIn("No new 15m candle", second["message"])
        self.assertEqual(len(self.client.buy_calls), 1)
        self.assertEqual(self.client.requested_candles, ("15m", 120))

    @patch(
        "trading_engine.calculate_indicators",
        return_value={**make_indicators(), "ema20": 95.0, "ema50": 100.0},
    )
    def test_sell_without_bot_position_is_silently_converted_to_hold(self, _indicators):
        self.engine.analyzer = FakeAnalyzer("SELL")
        result = self.engine.run_cycle(execute_orders=True)
        self.assertEqual(result["decision"]["action"], "HOLD")
        self.assertEqual(result["model_decision"]["action"], "SELL")
        self.assertIn("Automation continues", result["message"])
        self.assertNotIn("halt_automation", result)
        self.assertEqual(self.client.sell_calls, [])
        self.assertEqual(self.client.buy_calls, [])

    @patch("trading_engine.calculate_indicators", return_value=make_indicators())
    def test_analysis_emits_pipeline_progress_stages(self, _indicators):
        progress = []
        self.engine.run_cycle(
            execute_orders=False,
            progress_callback=lambda percent, text: progress.append((percent, text)),
        )
        self.assertEqual([step[0] for step in progress], [5, 42, 62, 82])
        self.assertIn("Running local Ollama model", [step[1] for step in progress])

    @patch("trading_engine.calculate_indicators", return_value=make_indicators())
    def test_engine_rejects_non_finite_ai_confidence(self, _indicators):
        self.engine.analyzer = FakeAnalyzer("BUY", confidence=float("nan"))
        with self.assertRaises(ValueError):
            self.engine.run_cycle(execute_orders=True)
        self.assertEqual(self.client.buy_calls, [])

    def test_invalid_exchange_balance_fails_closed(self):
        with self.assertRaises(ValueError):
            TradingEngine._balance(
                {"balances": [{"asset": "USDT", "free": "NaN"}]}, "USDT"
            )

    @patch("trading_engine.calculate_indicators", return_value=make_indicators())
    def test_testnet_auto_buy_uses_configured_cap(self, _indicators):
        result = self.engine.run_cycle(execute_orders=True)
        self.assertEqual(result["order"]["status"], "FILLED")
        self.assertEqual(self.client.buy_calls[0][1], 10)
        self.assertIsNone(self.ledger.pending_order)
        self.assertAlmostEqual(self.ledger.position("BTCUSDT")["quantity"], 0.1)
        self.assertEqual(
            self.ledger.position("BTCUSDT")["protection"]["status"], "active"
        )

    @patch(
        "trading_engine.calculate_indicators",
        return_value={**make_indicators(), "ema20": 95.0, "ema50": 100.0},
    )
    def test_testnet_sell_closes_bot_managed_position(self, _indicators):
        buy_id = "seed-position"
        self.ledger.reserve_order("BTCUSDT", "BUY", buy_id)
        self.ledger.record_buy(
            "BTCUSDT",
            {
                "clientOrderId": buy_id,
                "executedQty": "0.1",
                "cummulativeQuoteQty": "10",
                "fills": [],
            },
            "BTC",
            "USDT",
        )
        self.ledger.set_protection(
            "BTCUSDT",
            {
                "status": "active",
                "client_order_id": "seed-exits",
                "order_list_id": 88,
                "stop_price": 98.0,
                "take_profit_price": 104.0,
                "reported_fills": {},
            },
        )
        self.engine.analyzer = FakeAnalyzer("SELL")
        result = self.engine.run_cycle(execute_orders=True)
        self.assertEqual(result["order"]["side"], "SELL")
        self.assertEqual(len(self.client.sell_calls), 1)
        self.assertEqual(self.ledger.position("BTCUSDT")["quantity"], 0)
        self.assertIsNone(self.ledger.pending_order)

    def test_user_close_cancels_and_verifies_oco_before_market_sell(self):
        self.ledger.reserve_order("BTCUSDT", "BUY", "seed-close")
        self.ledger.record_buy(
            "BTCUSDT",
            {
                "clientOrderId": "seed-close",
                "executedQty": "0.1",
                "cummulativeQuoteQty": "10",
                "fills": [],
            },
            "BTC",
            "USDT",
        )
        self.ledger.set_protection(
            "BTCUSDT",
            {
                "status": "active",
                "client_order_id": "seed-close-exits",
                "order_list_id": 88,
                "stop_price": 98,
                "take_profit_price": 104,
                "reported_fills": {},
            },
        )
        result = self.engine.close_open_position()
        self.assertEqual(result["order"]["side"], "SELL")
        self.assertEqual(len(self.client.sell_calls), 1)
        self.assertEqual(self.ledger.position("BTCUSDT")["quantity"], 0)

    def test_user_close_never_sells_when_oco_cancel_is_ambiguous(self):
        self.ledger.reserve_order("BTCUSDT", "BUY", "seed-uncertain-close")
        self.ledger.record_buy(
            "BTCUSDT",
            {
                "clientOrderId": "seed-uncertain-close",
                "executedQty": "0.1",
                "cummulativeQuoteQty": "10",
                "fills": [],
            },
            "BTC",
            "USDT",
        )
        self.ledger.set_protection(
            "BTCUSDT",
            {
                "status": "active",
                "client_order_id": "seed-uncertain-exits",
                "order_list_id": 88,
                "stop_price": 98,
                "take_profit_price": 104,
                "reported_fills": {},
            },
        )
        self.client.cancel_order_list = Mock(
            side_effect=BinanceTestnetError("cancel response timed out")
        )
        with self.assertRaisesRegex(RuntimeError, "no market sell was sent"):
            self.engine.close_open_position()
        self.assertEqual(self.client.sell_calls, [])
        self.assertEqual(
            self.ledger.position("BTCUSDT")["protection"]["status"], "unknown"
        )

    def test_pending_testnet_order_is_reconciled(self):
        self.ledger.reserve_order("BTCUSDT", "BUY", "uncertain-order")
        message = self.engine.reconcile_pending_order()
        self.assertIn("Reconciled FILLED buy", message)
        self.assertIsNone(self.ledger.pending_order)
        self.assertAlmostEqual(self.ledger.position("BTCUSDT")["quantity"], 0.1)

    def test_failed_exit_submission_keeps_buy_visible_and_halts_bot(self):
        self.client.market_oco_sell = Mock(
            side_effect=BinanceTestnetError("exchange rejected exits", code=-1013)
        )
        with patch("trading_engine.calculate_indicators", return_value=make_indicators()):
            result = self.engine.run_cycle(execute_orders=True)
        self.assertEqual(result["order"]["side"], "BUY")
        self.assertTrue(result["halt_automation"])
        self.assertEqual(result["protection_status"], "unprotected")
        self.assertEqual(
            self.ledger.position("BTCUSDT")["protection"]["status"], "unprotected"
        )
        self.assertEqual(self.client.buy_calls.__len__(), 1)

    def test_unknown_exit_submission_is_persisted_and_blocks_next_cycle(self):
        self.client.market_oco_sell = Mock(
            side_effect=BinanceTestnetError("network timed out")
        )
        with patch("trading_engine.calculate_indicators", return_value=make_indicators()):
            first = self.engine.run_cycle(execute_orders=True)
        self.assertEqual(first["protection_status"], "unknown")
        restarted = TradeLedger(self.ledger.path)
        self.assertEqual(
            restarted.position("BTCUSDT")["protection"]["status"], "unknown"
        )
        self.client.order_list_status = Mock(
            side_effect=BinanceTestnetError("network timed out")
        )
        with self.assertRaisesRegex(RuntimeError, "Could not verify Binance protective exits"):
            TradingEngine(
                self.client, FakeAnalyzer("BUY"), restarted
            ).run_cycle(execute_orders=True)

    def test_exchange_protective_fill_updates_ledger_once_and_halts(self):
        with patch("trading_engine.calculate_indicators", return_value=make_indicators()):
            self.engine.run_cycle(execute_orders=True)
        protection = self.ledger.position("BTCUSDT")["protection"]
        client_id = protection["client_order_id"]
        response = self.client.order_list_status(client_id)
        response["listStatusType"] = "ALL_DONE"
        response["listOrderStatus"] = "ALL_DONE"
        response["orderReports"][0].update(
            {"status": "FILLED", "executedQty": "0.1", "cummulativeQuoteQty": "10.2"}
        )
        with self.assertRaisesRegex(RuntimeError, "protective exit filled"):
            self.engine._reconcile_protection()
        self.assertEqual(self.ledger.position("BTCUSDT")["quantity"], 0)
        self.assertAlmostEqual(self.ledger.realized_pnl_today(), 0.2)

    @patch("trading_engine.calculate_indicators", return_value=make_indicators())
    def test_immediately_completed_exit_list_is_reconciled_not_marked_active(self, _indicators):
        immediate_list = {
            "symbol": "BTCUSDT",
            "listClientOrderId": "ax_immediate",
            "orderListId": 91,
            "listStatusType": "ALL_DONE",
            "listOrderStatus": "ALL_DONE",
            "orders": [
                {"symbol": "BTCUSDT", "orderId": 92, "clientOrderId": "ax_tp"},
                {"symbol": "BTCUSDT", "orderId": 93, "clientOrderId": "ax_sl"},
            ],
            "orderReports": [
                {
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "clientOrderId": "ax_tp",
                    "executedQty": "0.1",
                    "cummulativeQuoteQty": "11",
                },
                {
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "clientOrderId": "ax_sl",
                    "executedQty": "0",
                    "cummulativeQuoteQty": "0",
                },
            ],
            "quantity": "0.1",
            "stopPrice": "98",
            "takeProfitPrice": "104",
        }
        self.client.market_oco_sell = Mock(
            side_effect=lambda *args: {
                **immediate_list,
                "listClientOrderId": args[-1],
            }
        )
        self.client.order_list_status = Mock(
            side_effect=lambda client_id: {
                **immediate_list,
                "listClientOrderId": client_id,
            }
        )
        result = self.engine.run_cycle(execute_orders=True)
        self.assertEqual(result["protection_status"], "unprotected", result["message"])
        self.assertTrue(result["halt_automation"])
        self.assertEqual(self.ledger.position("BTCUSDT")["quantity"], 0)
        self.assertAlmostEqual(self.ledger.realized_pnl_today(), 1)

    def test_malformed_exchange_exit_state_is_persisted_as_unknown(self):
        self.ledger.reserve_order("BTCUSDT", "BUY", "malformed-state-buy")
        self.ledger.record_buy(
            "BTCUSDT",
            {
                "clientOrderId": "malformed-state-buy",
                "executedQty": "0.1",
                "cummulativeQuoteQty": "10",
                "fills": [],
            },
            "BTC",
            "USDT",
        )
        self.ledger.set_protection(
            "BTCUSDT",
            {
                "status": "active",
                "client_order_id": "malformed-list",
                "order_list_id": 88,
                "stop_price": 98,
                "take_profit_price": 104,
                "reported_fills": {},
            },
        )
        self.client.order_list_status = Mock(return_value={"symbol": "BTCUSDT"})
        with self.assertRaisesRegex(RuntimeError, "could not be safely reconciled"):
            self.engine._reconcile_protection()
        self.assertEqual(
            self.ledger.position("BTCUSDT")["protection"]["status"], "unknown"
        )

    def test_unfound_order_stays_locked_until_manual_confirmation(self):
        self.ledger.reserve_order("BTCUSDT", "BUY", "missing-order")
        self.client.order_status = Mock(
            side_effect=BinanceTestnetError("order not found", code=-2013)
        )
        with self.assertRaisesRegex(RuntimeError, "remains locked"):
            self.engine.reconcile_pending_order()
        self.assertEqual(self.ledger.pending_order["client_order_id"], "missing-order")

    @patch("trading_engine.calculate_indicators", return_value=make_indicators())
    def test_daily_loss_stop_blocks_another_testnet_buy(self, _indicators):
        buy_id = "loss-buy"
        self.ledger.reserve_order("BTCUSDT", "BUY", buy_id)
        self.ledger.record_buy(
            "BTCUSDT",
            {
                "clientOrderId": buy_id,
                "executedQty": "1",
                "cummulativeQuoteQty": "10",
                "fills": [],
            },
            "BTC",
            "USDT",
        )
        sell_id = "loss-sell"
        self.ledger.reserve_order("BTCUSDT", "SELL", sell_id)
        self.ledger.record_sell(
            "BTCUSDT",
            {
                "clientOrderId": sell_id,
                "executedQty": "1",
                "cummulativeQuoteQty": "4",
                "fills": [],
            },
            "BTC",
            "USDT",
        )
        result = self.engine.run_cycle(execute_orders=True)
        self.assertIn("Daily realized-loss limit", result["message"])
        self.assertEqual(self.client.buy_calls, [])

    def test_live_risk_uses_full_account_equity_and_halts(self):
        live_client = FakeLiveClient()
        ledger = TradeLedger(os.path.join(self.temp_dir.name, "live.json"))
        ledger.observe_account_equity(1000)
        live_client.equity = 990
        live_engine = TradingEngine(
            live_client,
            FakeAnalyzer("BUY"),
            ledger,
            max_trade_quote=10,
            daily_loss_limit=5,
        )
        with patch("trading_engine.calculate_indicators", return_value=make_indicators()):
            result = live_engine.run_cycle(execute_orders=True)
        self.assertTrue(result["halt_automation"])
        self.assertIn("position is left untouched", result["message"])
        self.assertEqual(live_client.buy_calls, [])

    def test_live_trading_fails_closed_if_withdrawals_or_ip_lock_not_safe(self):
        live_client = FakeLiveClient()
        live_client.api_key_restrictions = lambda: {
            "enableReading": True,
            "enableSpotAndMarginTrading": True,
            "enableWithdrawals": True,
            "ipRestrict": False,
        }
        live_engine = TradingEngine(
            live_client,
            FakeAnalyzer("BUY"),
            TradeLedger(os.path.join(self.temp_dir.name, "unsafe-live.json")),
            max_trade_quote=10,
            daily_loss_limit=5,
        )
        with self.assertRaises(RuntimeError):
            live_engine.run_cycle(execute_orders=True)
        self.assertEqual(live_client.buy_calls, [])

    def test_live_daily_equity_stop_persists_baseline_across_restarts(self):
        path = os.path.join(self.temp_dir.name, "equity-ledger.json")
        ledger = TradeLedger(path)
        first = ledger.observe_account_equity(1000)
        self.assertEqual(first["start_usdt"], 1000)
        restarted = TradeLedger(path)
        next_observation = restarted.observe_account_equity(997)
        self.assertEqual(next_observation["start_usdt"], 1000)
        self.assertEqual(next_observation["drawdown_usdt"], 3)

    @patch("trading_engine.calculate_indicators", return_value=make_indicators())
    def test_low_confidence_signal_cannot_place_order(self, _indicators):
        self.engine.analyzer = FakeAnalyzer("BUY", confidence=0.79)
        result = self.engine.run_cycle(execute_orders=True)
        self.assertIn("below", result["message"])
        self.assertEqual(self.client.buy_calls, [])

    @patch(
        "trading_engine.calculate_indicators",
        return_value={**make_indicators(), "ema20": 95.0, "ema50": 100.0},
    )
    def test_buy_requires_bullish_technical_guard(self, _indicators):
        result = self.engine.run_cycle(execute_orders=True)
        self.assertIn("EMA20", result["message"])
        self.assertEqual(self.client.buy_calls, [])


if __name__ == "__main__":
    unittest.main()
