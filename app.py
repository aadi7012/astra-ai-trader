import csv
from datetime import datetime, timezone
import hashlib
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import webbrowser

from account_display import (
    calculate_unrealized_pnl,
    format_account_balances,
    format_account_profile,
    parse_account_balances,
)
from backtest import run_backtest, validate_backtest_settings
from binance_permissions import validate_live_api_restrictions
from binance_testnet import BinanceSpotLive, BinanceSpotTestnet, BinanceTestnetError
from credential_store import CredentialStore
from desktop_alerts import message_alert, result_alert_messages
from instance_lock import InstanceLock, InstanceLockError
from ledger_storage import resolve_ledger_path
from local_ai import OllamaAnalyzer, calculate_indicators
from market_scanner import SpotMarketScanner
from market_chart import CandlestickChart
from trade_ledger import LedgerError, TradeLedger
from trade_history import parse_trade_history
from trading_engine import TRADE_INTERVAL_SECONDS, TradingEngine

SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    executable_dir = os.path.dirname(os.path.abspath(sys.executable))
    parent_dir = os.path.dirname(executable_dir)
    SOURCE_DIR = (
        parent_dir
        if os.path.isfile(os.path.join(parent_dir, "app.py"))
        else executable_dir
    )
APP_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "AstraAITrader",
)
APP_DIR = APP_DATA_DIR
MARKET_REFRESH_MS = 60_000
LIVE_PRICE_REFRESH_MS = 1_000
BINANCE_API_MANAGEMENT_URL = "https://www.binance.com/en/my/settings/api-management"


class TraderApp:
    def __init__(self, root, instance_lock=None):
        self.root = root
        self.root.title("Astra AI Trader — Binance Spot")
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        window_width = min(1240, max(900, screen_width - 40))
        window_height = min(900, max(620, screen_height - 100))
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(min(900, window_width), min(620, window_height))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.alerts_enabled_var = tk.BooleanVar(master=self.root, value=True)
        self.live_quotes_var = tk.BooleanVar(master=self.root, value=True)
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.notification_window = None
        self.notification_after = None
        self.market_refresh_after = None
        self.live_price_after = None
        self.live_price_worker = None
        self.live_prices = {}
        self.last_live_price_error = None
        self.live_price_error_logged_at = 0
        self.market_candles = []
        self.balance_records = []
        self.trade_history_records = []
        self.trade_history_symbol = ""
        self.trade_history_quote_asset = ""
        self.trade_history_mode = ""
        self.animation_step = 0
        self.operation_after = None
        self.last_analysis = None
        self.active_trade_records = []
        self.auto_trading_active = False
        self.instance_lock = instance_lock
        self.active_mode = "TESTNET"
        self.ledger_cache = {}
        credential_dir = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "AstraAITrader",
        )
        self.credential_store = CredentialStore(
            os.path.join(credential_dir, "credentials.dpapi")
        )
        self.saved_credentials_error = None
        self.mode_var = tk.StringVar(value="TESTNET")
        self.ledger = self._get_ledger("TESTNET", "")
        self._build_ui()
        self.root.after(150, self._process_events)

        if self.ledger.pending_order:
            self.status_var.set(
                "A previous order needs reconciliation before more orders can be sent."
            )

    def _build_ui(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.colors = {
            "background": "#0a1020",
            "surface": "#111a2c",
            "surface_alt": "#1a2740",
            "text": "#edf3ff",
            "muted": "#9cacc4",
            "accent": "#57b8ff",
            "green": "#35d0a0",
            "red": "#ff7184",
            "amber": "#f1b95f",
        }
        style.configure(".", background=self.colors["background"], foreground=self.colors["text"])
        style.configure("TFrame", background=self.colors["background"])
        style.configure(
            "Card.TFrame",
            background=self.colors["surface"],
            borderwidth=1,
            relief="solid",
        )
        style.configure("TLabel", background=self.colors["background"], foreground=self.colors["text"])
        style.configure(
            "TLabelframe",
            background=self.colors["background"],
            foreground=self.colors["muted"],
            bordercolor="#2a3b58",
        )
        style.configure(
            "TLabelframe.Label",
            background=self.colors["background"],
            foreground=self.colors["muted"],
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "TEntry",
            fieldbackground=self.colors["surface_alt"],
            foreground=self.colors["text"],
            insertcolor=self.colors["text"],
            bordercolor="#2a3b58",
        )
        style.configure(
            "TCombobox",
            fieldbackground=self.colors["surface_alt"],
            foreground=self.colors["text"],
            arrowcolor=self.colors["text"],
        )
        style.configure(
            "TCheckbutton",
            background=self.colors["background"],
            foreground=self.colors["muted"],
        )
        style.map(
            "TCheckbutton",
            background=[("active", self.colors["background"])],
            foreground=[("active", self.colors["text"])],
        )
        style.configure(
            "Muted.TLabel",
            background=self.colors["background"],
            foreground=self.colors["muted"],
        )
        style.configure(
            "Title.TLabel",
            background=self.colors["background"],
            foreground=self.colors["text"],
            font=("Segoe UI", 22, "bold"),
        )
        style.configure(
            "CardTitle.TLabel",
            background=self.colors["surface"],
            foreground=self.colors["muted"],
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "Metric.TLabel",
            background=self.colors["surface"],
            foreground=self.colors["text"],
            font=("Segoe UI", 18, "bold"),
        )
        style.configure("TNotebook", background=self.colors["background"], borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=self.colors["surface_alt"],
            foreground=self.colors["muted"],
            padding=(18, 9),
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", self.colors["surface"])],
            foreground=[("selected", self.colors["text"])],
        )
        style.configure(
            "Treeview",
            background=self.colors["surface"],
            fieldbackground=self.colors["surface"],
            foreground=self.colors["text"],
            rowheight=26,
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=self.colors["surface_alt"],
            foreground=self.colors["muted"],
            font=("Segoe UI", 9, "bold"),
            relief="flat",
        )
        style.map("Treeview", background=[("selected", "#254a72")])
        style.configure(
            "TButton",
            background=self.colors["surface_alt"],
            foreground=self.colors["text"],
            padding=(11, 7),
            borderwidth=0,
        )
        style.map(
            "TButton",
            background=[("active", "#293f60"), ("disabled", self.colors["surface"])],
            foreground=[("disabled", self.colors["muted"])],
        )
        style.configure(
            "Accent.TButton",
            background="#176da8",
            foreground="#ffffff",
            font=("Segoe UI", 9, "bold"),
        )
        style.map("Accent.TButton", background=[("active", "#2289cb")])
        style.configure(
            "Danger.TButton",
            background="#873449",
            foreground="#ffffff",
            font=("Segoe UI", 9, "bold"),
        )
        self.root.configure(background=self.colors["background"])

        self.mode_banner_var = tk.StringVar()
        self.mode_banner = tk.Label(
            self.root,
            textvariable=self.mode_banner_var,
            bg="#123d5b",
            fg="#d9f1ff",
            font=("Segoe UI", 9, "bold"),
            padx=16,
            pady=7,
            anchor=tk.W,
        )
        self.mode_banner.pack(fill=tk.X)

        header = ttk.Frame(self.root, padding=(22, 14, 22, 12))
        header.pack(fill=tk.X)
        ttk.Label(header, text="Astra AI Trader", style="Title.TLabel").pack(
            side=tk.LEFT
        )
        ttk.Label(
            header,
            text="  BINANCE SPOT  ·  LOCAL AI",
            style="Muted.TLabel",
            padding=(12, 8),
        ).pack(side=tk.LEFT)
        self.connection_dot = tk.Canvas(
            header,
            width=14,
            height=14,
            background=self.colors["background"],
            highlightthickness=0,
        )
        self.connection_dot.pack(side=tk.RIGHT, padx=(8, 0))
        self.connection_label_var = tk.StringVar(value="MARKET DATA")
        ttk.Label(
            header, textvariable=self.connection_label_var, style="Muted.TLabel"
        ).pack(side=tk.RIGHT)
        self.ai_health_var = tk.StringVar(value="LOCAL AI · NOT CHECKED")
        ttk.Label(
            header,
            textvariable=self.ai_health_var,
            style="Muted.TLabel",
            padding=(14, 0),
        ).pack(side=tk.RIGHT)

        self.action_bar = ttk.Frame(self.root, padding=(18, 0, 18, 10))
        self.action_bar.pack(fill=tk.X)
        self.connect_button = ttk.Button(
            self.action_bar,
            text="Connect Binance account",
            command=self.connect_account,
        )
        self.connect_button.pack(side=tk.LEFT, padx=(0, 7))
        self.analyze_button = ttk.Button(
            self.action_bar, text="Analyze only", command=self.analyze_only
        )
        self.analyze_button.pack(side=tk.LEFT, padx=(0, 7))
        self.start_button = ttk.Button(
            self.action_bar,
            text="Start automatic trading",
            command=self.start_automation,
            style="Accent.TButton",
        )
        self.start_button.pack(side=tk.LEFT, padx=(0, 7))
        self.stop_button = ttk.Button(
            self.action_bar,
            text="Pause bot",
            command=self.stop_automation,
            state=tk.DISABLED,
            style="Danger.TButton",
        )
        self.stop_button.pack(side=tk.LEFT, padx=(0, 7))
        self.secondary_action_bar = ttk.Frame(self.root, padding=(18, 0, 18, 8))
        self.secondary_action_bar.pack(fill=tk.X)
        self.reconcile_button = ttk.Button(
            self.secondary_action_bar,
            text="Reconcile pending order",
            command=self.reconcile_pending,
        )
        self.reconcile_button.pack(side=tk.LEFT, padx=(0, 7))
        self.clear_pending_button = ttk.Button(
            self.secondary_action_bar,
            text="Confirm no order exists",
            command=self.confirm_no_pending_order,
        )
        self.clear_pending_button.pack(side=tk.LEFT)
        ttk.Checkbutton(
            self.secondary_action_bar,
            text="Desktop alerts",
            variable=self.alerts_enabled_var,
        ).pack(side=tk.RIGHT, padx=(0, 12))

        self.testnet_api_key_var = tk.StringVar()
        self.testnet_api_secret_var = tk.StringVar()
        self.live_api_key_var = tk.StringVar()
        self.live_api_secret_var = tk.StringVar()
        self.credential_entries = []
        self.model_var = tk.StringVar(value="qwen2.5:3b")
        self.symbol_var = tk.StringVar(value="BTCUSDT")
        self.interval_var = tk.StringVar(value="1h")
        self.trade_interval_var = tk.StringVar(value="15m")
        self.auto_refresh_var = tk.BooleanVar(value=True)
        self.max_order_var = tk.StringVar(value="10")
        self.daily_loss_var = tk.StringVar(value="5")
        self.stop_loss_pct_var = tk.StringVar(value="2")
        self.take_profit_pct_var = tk.StringVar(value="4")
        self.account_var = tk.StringVar(value="No exchange account connected.")
        self.status_var = tk.StringVar(value="Automation is off.")
        self.position_var = tk.StringVar(value=self._position_summary())
        self.price_var = tk.StringVar(value="Waiting for market data")
        self.market_time_var = tk.StringVar(value="Chart refreshes every 60 seconds")
        self.balance_total_var = tk.StringVar(value="Connect an account to view balances")
        self.account_equity_var = tk.StringVar(value="Not valued")
        self.daily_loss_from_start_var = tk.StringVar(value="LIVE only")
        self.drawdown_var = tk.StringVar(value="LIVE only")
        self.market_delta_var = tk.StringVar(value="—")
        self.prediction_var = tk.StringVar(value="Run Analyze only to request an AI assessment.")
        self.prediction_action_var = tk.StringVar(value="NO PREDICTION")
        self.prediction_confidence_var = tk.StringVar(value="Model confidence is uncalibrated")
        self.prediction_reason_var = tk.StringVar(value="")
        self.scanner_status_var = tk.StringVar(
            value="Automatic mode scans the top 20 liquid USDT Spot pairs."
        )
        self.operation_var = tk.StringVar(value="Ready")
        self.remember_credentials_var = tk.BooleanVar(value=True)
        self.show_credentials_var = tk.BooleanVar(value=False)
        self._load_saved_credentials()

        operation_line = ttk.Frame(self.root, padding=(20, 0, 20, 8))
        operation_line.pack(fill=tk.X)
        ttk.Label(
            operation_line, textvariable=self.operation_var, style="Muted.TLabel"
        ).pack(side=tk.LEFT)
        self.operation_progress = ttk.Progressbar(
            operation_line, mode="indeterminate", length=240
        )
        self.operation_progress.pack(side=tk.RIGHT)

        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 14))
        self.dashboard_tab = ttk.Frame(self.tabs)
        self.dashboard_canvas = tk.Canvas(
            self.dashboard_tab,
            background=self.colors["background"],
            highlightthickness=0,
        )
        self.dashboard_scrollbar = ttk.Scrollbar(
            self.dashboard_tab,
            orient=tk.VERTICAL,
            command=self.dashboard_canvas.yview,
        )
        self.dashboard_canvas.configure(yscrollcommand=self.dashboard_scrollbar.set)
        self.dashboard_content = ttk.Frame(self.dashboard_canvas, padding=14)
        self.dashboard_canvas_window = self.dashboard_canvas.create_window(
            (0, 0), window=self.dashboard_content, anchor=tk.NW
        )
        self.dashboard_content.bind(
            "<Configure>", self._update_dashboard_scrollregion
        )
        self.dashboard_canvas.bind("<Configure>", self._resize_dashboard_content)
        self.dashboard_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.dashboard_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.root.bind_all("<MouseWheel>", self._scroll_dashboard)
        self.settings_tab = ttk.Frame(self.tabs, padding=18)
        self.activity_tab = self.settings_tab
        self.tabs.add(self.dashboard_tab, text="  Dashboard  ")
        self.tabs.add(self.settings_tab, text="  Connection & Activity  ")

        market_toolbar = ttk.Frame(self.dashboard_content)
        market_toolbar.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(market_toolbar, text="Market", font=("Segoe UI", 14, "bold")).pack(
            side=tk.LEFT, padx=(0, 12)
        )
        ttk.Label(market_toolbar, text="Symbol", style="Muted.TLabel").pack(side=tk.LEFT)
        symbol_entry = ttk.Entry(market_toolbar, textvariable=self.symbol_var, width=13)
        symbol_entry.pack(side=tk.LEFT, padx=(6, 12))
        symbol_entry.bind("<Return>", lambda _event: self.refresh_market())
        ttk.Label(market_toolbar, text="Interval", style="Muted.TLabel").pack(side=tk.LEFT)
        interval_box = ttk.Combobox(
            market_toolbar,
            textvariable=self.interval_var,
            values=("1m", "5m", "15m", "1h", "4h", "1d"),
            state="readonly",
            width=7,
        )
        interval_box.pack(side=tk.LEFT, padx=(6, 10))
        interval_box.bind("<<ComboboxSelected>>", lambda _event: self.refresh_market())
        self.refresh_market_button = ttk.Button(
            market_toolbar,
            text="Refresh chart",
            command=self.refresh_market,
            style="Accent.TButton",
        )
        self.refresh_market_button.pack(side=tk.LEFT)
        self.backtest_button = ttk.Button(
            market_toolbar,
            text="Backtest",
            command=self.open_backtest_dialog,
        )
        self.backtest_button.pack(side=tk.LEFT, padx=(7, 0))
        ttk.Checkbutton(
            market_toolbar,
            text="Auto refresh · 60 sec",
            variable=self.auto_refresh_var,
            command=self._update_refresh_label,
        ).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Checkbutton(
            market_toolbar,
            text="Live prices · ~1 sec",
            variable=self.live_quotes_var,
            command=self._toggle_live_quotes,
        ).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Label(
            market_toolbar, textvariable=self.market_time_var, style="Muted.TLabel"
        ).pack(side=tk.RIGHT)

        metrics = ttk.Frame(self.dashboard_content)
        metrics.pack(fill=tk.X, pady=(0, 10))
        for column in range(3):
            metrics.columnconfigure(column, weight=1, uniform="metrics")
        self._metric_card(metrics, 0, "LATEST PRICE", self.price_var)
        self._metric_card(metrics, 1, "PERIOD CHANGE", self.market_delta_var)
        self._metric_card(metrics, 2, "FREE + LOCKED USDT", self.balance_total_var)

        risk_metrics = ttk.Frame(self.dashboard_content)
        risk_metrics.pack(fill=tk.X, pady=(0, 10))
        for column in range(3):
            risk_metrics.columnconfigure(column, weight=1, uniform="risk-metrics")
        self._metric_card(
            risk_metrics, 0, "EST. TOTAL ACCOUNT EQUITY", self.account_equity_var
        )
        self._metric_card(
            risk_metrics, 1, "LOSS FROM UTC DAY START", self.daily_loss_from_start_var
        )
        self._metric_card(
            risk_metrics, 2, "DRAWDOWN FROM HIGH-WATER", self.drawdown_var
        )
        ttk.Label(
            self.dashboard_content,
            text=(
                "Account equity is an estimate from valued free + locked Spot balances. "
                "Refresh the account to update it; the LIVE loss stop is checked during "
                "each trading cycle, not continuously. The stop compares current equity "
                "with that day's persisted high-water mark; loss from day start is shown separately."
            ),
            style="Muted.TLabel",
            wraplength=1050,
        ).pack(fill=tk.X, pady=(0, 10))

        prediction_card = ttk.Frame(self.dashboard_content, style="Card.TFrame", padding=10)
        prediction_card.pack(fill=tk.X, pady=(0, 10))
        prediction_header = ttk.Frame(prediction_card, style="Card.TFrame")
        prediction_header.pack(fill=tk.X)
        ttk.Label(
            prediction_header, text="AI PREDICTION & PROCESS", style="CardTitle.TLabel"
        ).pack(side=tk.LEFT)
        ttk.Label(
            prediction_header,
            textvariable=self.prediction_confidence_var,
            style="CardTitle.TLabel",
        ).pack(side=tk.RIGHT)
        prediction_main = ttk.Frame(prediction_card, style="Card.TFrame")
        prediction_main.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(
            prediction_main,
            textvariable=self.prediction_action_var,
            style="Metric.TLabel",
        ).pack(side=tk.LEFT, padx=(0, 12))
        prediction_details = ttk.Frame(prediction_main, style="Card.TFrame")
        prediction_details.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(
            prediction_details,
            textvariable=self.prediction_var,
            style="Muted.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            prediction_details,
            textvariable=self.prediction_reason_var,
            style="Muted.TLabel",
            wraplength=820,
        ).pack(anchor=tk.W, pady=(3, 0))
        self.prediction_progress = ttk.Progressbar(
            prediction_card, maximum=100, mode="determinate", length=300
        )
        self.prediction_progress.pack(fill=tk.X, pady=(8, 0))

        scanner_card = ttk.Frame(
            self.dashboard_content, style="Card.TFrame", padding=10
        )
        scanner_card.pack(fill=tk.X, pady=(0, 10))
        scanner_heading = ttk.Frame(scanner_card, style="Card.TFrame")
        scanner_heading.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            scanner_heading,
            text="SPOT OPPORTUNITY SCANNER · TOP 20 LIQUID USDT PAIRS",
            style="CardTitle.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Label(
            scanner_heading,
            textvariable=self.scanner_status_var,
            style="CardTitle.TLabel",
        ).pack(side=tk.RIGHT)
        scanner_table = ttk.Frame(scanner_card, style="Card.TFrame")
        scanner_table.pack(fill=tk.X)
        self.market_scan_tree = ttk.Treeview(
            scanner_table,
            columns=("symbol", "price", "volume", "score", "rsi", "momentum", "status"),
            show="headings",
            height=6,
        )
        for column, title, width, anchor in (
            ("symbol", "SYMBOL", 90, tk.W),
            ("price", "LIVE PRICE", 110, tk.E),
            ("volume", "24H QUOTE VOLUME", 150, tk.E),
            ("score", "TECH SCORE · NOT PROBABILITY", 190, tk.E),
            ("rsi", "RSI14", 75, tk.E),
            ("momentum", "3-CANDLE MOMENTUM", 155, tk.E),
            ("status", "FILTER STATUS", 270, tk.W),
        ):
            self.market_scan_tree.heading(column, text=title)
            self.market_scan_tree.column(column, width=width, anchor=anchor)
        self.market_scan_tree.tag_configure(
            "candidate", foreground=self.colors["green"]
        )
        self.market_scan_tree.tag_configure(
            "selected", background="#254a72", foreground=self.colors["text"]
        )
        scan_scrollbar = ttk.Scrollbar(
            scanner_table, orient=tk.VERTICAL, command=self.market_scan_tree.yview
        )
        self.market_scan_tree.configure(yscrollcommand=scan_scrollbar.set)
        self.market_scan_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        scan_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        ttk.Label(
            scanner_card,
            text=(
                "Scanner score ranks technical conditions; it is not an AI probability "
                "or a promise of profit. Only the top eligible candidate is sent to local "
                "AI for review. Automatic Spot mode manages one position at a time."
            ),
            style="Muted.TLabel",
            wraplength=1050,
        ).pack(fill=tk.X, pady=(6, 0))

        chart_card = ttk.Frame(self.dashboard_content, style="Card.TFrame", padding=10)
        chart_card.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        chart_heading = ttk.Frame(chart_card, style="Card.TFrame")
        chart_heading.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            chart_heading, text="PRICE ACTION", style="CardTitle.TLabel"
        ).pack(side=tk.LEFT)
        self.chart_status_var = tk.StringVar(value="Waiting for Binance market data")
        ttk.Label(
            chart_heading, textvariable=self.chart_status_var, style="CardTitle.TLabel"
        ).pack(side=tk.RIGHT)
        self.market_chart = CandlestickChart(chart_card, height=340)
        self.market_chart.pack(fill=tk.BOTH, expand=True)

        dashboard_bottom = ttk.Frame(self.dashboard_content)
        dashboard_bottom.pack(fill=tk.X)
        balance_card = ttk.Frame(dashboard_bottom, style="Card.TFrame", padding=10)
        balance_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))
        balance_heading = ttk.Frame(balance_card, style="Card.TFrame")
        balance_heading.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            balance_heading, text="ACCOUNT BALANCES", style="CardTitle.TLabel"
        ).pack(side=tk.LEFT)
        self.refresh_account_button = ttk.Button(
            balance_heading, text="Refresh account", command=self.connect_account
        )
        self.refresh_account_button.pack(side=tk.RIGHT)
        self.export_balance_button = ttk.Button(
            balance_heading, text="Export CSV", command=self.export_balances
        )
        self.export_balance_button.pack(side=tk.RIGHT, padx=(0, 6))
        self.balance_tree = ttk.Treeview(
            balance_card,
            columns=("asset", "free", "locked", "total"),
            show="headings",
            height=5,
        )
        for column, title, width in (
            ("asset", "ASSET", 90),
            ("free", "FREE", 130),
            ("locked", "LOCKED", 130),
            ("total", "TOTAL", 130),
        ):
            self.balance_tree.heading(column, text=title)
            self.balance_tree.column(
                column, width=width, anchor=tk.W if column == "asset" else tk.E
            )
        self.balance_tree.pack(fill=tk.BOTH, expand=True)

        active_card = ttk.Frame(self.dashboard_content, style="Card.TFrame", padding=10)
        active_card.pack(fill=tk.X, pady=(8, 0))
        active_heading = ttk.Frame(active_card, style="Card.TFrame")
        active_heading.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            active_heading,
            text="BOT POSITION · EST. GROSS P/L (FEES EXCLUDED)",
            style="CardTitle.TLabel",
        ).pack(side=tk.LEFT)
        self.close_position_button = ttk.Button(
            active_heading,
            text="Close managed position",
            command=self.close_managed_position,
            style="Danger.TButton",
            state=tk.DISABLED,
        )
        self.close_position_button.pack(side=tk.RIGHT, padx=(6, 0))
        self.verify_position_button = ttk.Button(
            active_heading,
            text="Verify exits",
            command=self.verify_managed_position,
            state=tk.DISABLED,
        )
        self.verify_position_button.pack(side=tk.RIGHT, padx=(6, 0))
        self.protect_position_button = ttk.Button(
            active_heading,
            text="Protect open position",
            command=self.protect_managed_position,
            style="Accent.TButton",
            state=tk.DISABLED,
        )
        self.protect_position_button.pack(side=tk.RIGHT, padx=(6, 0))
        self.active_trade_tree = ttk.Treeview(
            active_card,
            columns=("symbol", "quantity", "cost", "price", "pnl", "protection"),
            show="headings",
            height=2,
        )
        for column, title, width in (
            ("symbol", "SYMBOL", 80),
            ("quantity", "QTY", 110),
            ("cost", "ENTRY COST", 120),
            ("price", "MARK PRICE", 120),
            ("pnl", "EST. P/L", 130),
            ("protection", "EXCHANGE EXITS", 230),
        ):
            self.active_trade_tree.heading(column, text=title)
            self.active_trade_tree.column(
                column, width=width, anchor=tk.W if column == "symbol" else tk.E
            )
        self.active_trade_tree.pack(fill=tk.X)

        history_card = ttk.Frame(self.dashboard_content, style="Card.TFrame", padding=10)
        history_card.pack(fill=tk.X, pady=(8, 0))
        history_heading = ttk.Frame(history_card, style="Card.TFrame")
        history_heading.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            history_heading, text="BINANCE RECENT FILLS · UP TO 1,000", style="CardTitle.TLabel"
        ).pack(side=tk.LEFT)
        self.export_history_button = ttk.Button(
            history_heading,
            text="Export CSV",
            command=self.export_trade_history,
            state=tk.DISABLED,
        )
        self.export_history_button.pack(side=tk.RIGHT, padx=(0, 6))
        self.load_history_button = ttk.Button(
            history_heading,
            text="Load selected symbol",
            command=self.load_trade_history,
        )
        self.load_history_button.pack(side=tk.RIGHT)
        self.history_status_var = tk.StringVar(
            value="Read-only account fills · select a symbol and load history"
        )
        ttk.Label(
            history_card,
            textvariable=self.history_status_var,
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(0, 6))
        ttk.Label(
            history_card,
            text=(
                "FIFO realized P/L is shown only when the fetched fills include the "
                "matched cost basis and fees are in the base/quote asset; otherwise it "
                "is unavailable (—)."
            ),
            style="Muted.TLabel",
            wraplength=1050,
        ).pack(anchor=tk.W, pady=(0, 7))
        history_table = ttk.Frame(history_card, style="Card.TFrame")
        history_table.pack(fill=tk.X)
        self.trade_history_tree = ttk.Treeview(
            history_table,
            columns=("time", "side", "price", "quantity", "quote", "fee", "pnl"),
            show="headings",
            height=6,
        )
        for column, title, width in (
            ("time", "TIME (LOCAL)", 160),
            ("side", "SIDE", 70),
            ("price", "PRICE", 120),
            ("quantity", "BASE QTY", 110),
            ("quote", "QUOTE TOTAL", 130),
            ("fee", "COMMISSION", 130),
            ("pnl", "FIFO REALIZED P/L", 160),
        ):
            self.trade_history_tree.heading(column, text=title)
            self.trade_history_tree.column(
                column, width=width, anchor=tk.W if column == "side" else tk.E
            )
        history_scrollbar = ttk.Scrollbar(
            history_table,
            orient=tk.VERTICAL,
            command=self.trade_history_tree.yview,
        )
        self.trade_history_tree.configure(yscrollcommand=history_scrollbar.set)
        self.trade_history_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        history_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        bot_card = ttk.Frame(dashboard_bottom, style="Card.TFrame", padding=10)
        bot_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))
        ttk.Label(bot_card, text="ACCOUNT & BOT", style="CardTitle.TLabel").pack(
            anchor=tk.W, pady=(0, 7)
        )
        ttk.Label(bot_card, textvariable=self.account_var, wraplength=430).pack(
            anchor=tk.W
        )
        ttk.Label(
            bot_card, textvariable=self.position_var, wraplength=430, style="Muted.TLabel"
        ).pack(anchor=tk.W, pady=(6, 0))
        self.status_dot = tk.Canvas(
            bot_card,
            width=12,
            height=12,
            background=self.colors["surface"],
            highlightthickness=0,
        )
        self.status_dot.pack(side=tk.LEFT, padx=(0, 6), pady=(8, 0))
        ttk.Label(
            bot_card, textvariable=self.status_var, wraplength=400, style="Muted.TLabel"
        ).pack(anchor=tk.W, pady=(7, 0))

        config = ttk.LabelFrame(
            self.settings_tab, text="Exchange credentials and risk limits", padding=16
        )
        config.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(config, text="Exchange mode").grid(row=0, column=0, sticky=tk.W)
        mode_box = ttk.Combobox(
            config,
            textvariable=self.mode_var,
            values=("TESTNET", "LIVE"),
            state="readonly",
            width=18,
        )
        mode_box.grid(row=1, column=0, sticky=tk.EW, padx=(0, 12), pady=(4, 0))
        mode_box.bind("<<ComboboxSelected>>", lambda _event: self._on_mode_changed())

        ttk.Label(config, text="Testnet API key").grid(row=0, column=1, sticky=tk.W)
        testnet_key_entry = ttk.Entry(
            config, textvariable=self.testnet_api_key_var, show="•", width=34
        )
        testnet_key_entry.grid(
            row=1, column=1, sticky=tk.EW, padx=(0, 12), pady=(4, 0)
        )
        self.credential_entries.append(testnet_key_entry)
        ttk.Label(config, text="Testnet API secret").grid(row=0, column=2, sticky=tk.W)
        testnet_secret_entry = ttk.Entry(
            config, textvariable=self.testnet_api_secret_var, show="•", width=34
        )
        testnet_secret_entry.grid(
            row=1, column=2, sticky=tk.EW, pady=(4, 0)
        )
        self.credential_entries.append(testnet_secret_entry)

        ttk.Label(config, text="LIVE API key (real account)").grid(
            row=2, column=0, sticky=tk.W, pady=(8, 0)
        )
        live_key_entry = ttk.Entry(
            config, textvariable=self.live_api_key_var, show="•", width=34
        )
        live_key_entry.grid(
            row=3, column=0, sticky=tk.EW, padx=(0, 12), pady=(4, 0)
        )
        self.credential_entries.append(live_key_entry)
        ttk.Label(config, text="LIVE API secret").grid(
            row=2, column=1, sticky=tk.W, pady=(8, 0)
        )
        live_secret_entry = ttk.Entry(
            config, textvariable=self.live_api_secret_var, show="•", width=34
        )
        live_secret_entry.grid(
            row=3, column=1, sticky=tk.EW, padx=(0, 12), pady=(4, 0)
        )
        self.credential_entries.append(live_secret_entry)
        ttk.Label(config, text="Local Ollama model").grid(
            row=2, column=2, sticky=tk.W, pady=(8, 0)
        )
        ttk.Entry(config, textvariable=self.model_var, width=22).grid(
            row=3, column=2, sticky=tk.EW, pady=(4, 0)
        )

        ttk.Label(config, text="Spot symbol").grid(row=4, column=0, sticky=tk.W, pady=(10, 0))
        ttk.Entry(config, textvariable=self.symbol_var, width=20).grid(
            row=5, column=0, sticky=tk.EW, padx=(0, 12), pady=(4, 0)
        )
        ttk.Label(config, text="Maximum buy per trade (USDT)").grid(
            row=4, column=1, sticky=tk.W, pady=(10, 0)
        )
        ttk.Entry(config, textvariable=self.max_order_var, width=20).grid(
            row=5, column=1, sticky=tk.EW, padx=(0, 12), pady=(4, 0)
        )
        ttk.Label(config, text="Maximum equity drawdown stop (USDT)").grid(
            row=4, column=2, sticky=tk.W, pady=(10, 0)
        )
        ttk.Entry(config, textvariable=self.daily_loss_var, width=20).grid(
            row=5, column=2, sticky=tk.EW, pady=(4, 0)
        )
        ttk.Label(config, text="Protective stop-loss from average entry (%)").grid(
            row=6, column=0, sticky=tk.W, pady=(10, 0)
        )
        ttk.Entry(config, textvariable=self.stop_loss_pct_var, width=20).grid(
            row=7, column=0, sticky=tk.EW, padx=(0, 12), pady=(4, 0)
        )
        ttk.Label(config, text="Protective take-profit from average entry (%)").grid(
            row=6, column=1, sticky=tk.W, pady=(10, 0)
        )
        ttk.Entry(config, textvariable=self.take_profit_pct_var, width=20).grid(
            row=7, column=1, sticky=tk.EW, padx=(0, 12), pady=(4, 0)
        )
        ttk.Label(config, text="Automatic trading timeframe").grid(
            row=6, column=2, sticky=tk.W, pady=(10, 0)
        )
        ttk.Combobox(
            config,
            textvariable=self.trade_interval_var,
            values=tuple(TRADE_INTERVAL_SECONDS),
            state="readonly",
            width=18,
        ).grid(row=7, column=2, sticky=tk.EW, pady=(4, 0))
        ttk.Label(
            config,
            text=(
                "Example starting values only—not financial advice. After a buy fills, "
                "the app requests a Binance OCO pair of market-triggered exits. A trigger "
                "does not guarantee a fill price; fees and slippage can change results. "
                "Automation evaluates each newly closed candle and waits for the next "
                "selected timeframe. No setting can guarantee profit or prevent all losses."
            ),
            style="Muted.TLabel",
            wraplength=920,
        ).grid(row=8, column=0, columnspan=3, sticky=tk.W, pady=(7, 0))
        config.columnconfigure(0, weight=1)
        config.columnconfigure(1, weight=1)
        config.columnconfigure(2, weight=1)
        self.credential_notice_var = tk.StringVar()
        ttk.Label(
            config,
            textvariable=self.credential_notice_var,
            foreground=self.colors["amber"],
            wraplength=920,
        ).grid(row=9, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))
        self._on_mode_changed(reset_limits=False)

        setup_note = ttk.Frame(self.settings_tab, style="Card.TFrame", padding=12)
        setup_note.pack(fill=tk.X)
        ttk.Label(
            setup_note,
            text="API KEY SAFETY",
            style="CardTitle.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            setup_note,
            text=(
                "Use a dedicated key. Keep withdrawals disabled and restrict it to your public IP. "
                "Remembered credentials are encrypted for this Windows user with DPAPI."
            ),
            wraplength=950,
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(6, 8))
        self.api_setup_button = ttk.Button(
            setup_note, text="Open Binance API management", command=self.open_api_setup
        )
        self.api_setup_button.pack(anchor=tk.W)
        credential_options = ttk.Frame(setup_note, style="Card.TFrame")
        credential_options.pack(fill=tk.X, pady=(8, 0))
        ttk.Checkbutton(
            credential_options,
            text="Remember last connected keys (encrypted for this Windows account)",
            variable=self.remember_credentials_var,
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            credential_options,
            text="Show API keys",
            variable=self.show_credentials_var,
            command=self._toggle_credential_visibility,
        ).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(
            credential_options, text="Forget saved keys", command=self.forget_credentials
        ).pack(side=tk.RIGHT)

        activity_header = ttk.Frame(self.activity_tab)
        activity_header.pack(fill=tk.X, pady=(18, 8))
        ttk.Label(
            activity_header, text="Activity & execution log", font=("Segoe UI", 13, "bold")
        ).pack(side=tk.LEFT)
        ttk.Button(
            activity_header, text="Copy log", command=self.copy_log
        ).pack(side=tk.RIGHT, padx=(7, 0))
        ttk.Button(
            activity_header, text="Clear logs", command=self.clear_logs
        ).pack(side=tk.RIGHT)
        details = ttk.Frame(self.activity_tab, style="Card.TFrame", padding=8)
        details.pack(fill=tk.BOTH, expand=True)
        self.output = tk.Text(
            details,
            wrap=tk.WORD,
            font=("Cascadia Mono", 10),
            height=20,
            state=tk.DISABLED,
            background="#0b1220",
            foreground="#cbd7e8",
            insertbackground="#ffffff",
            selectbackground="#29466f",
            relief=tk.FLAT,
            padx=10,
            pady=10,
        )
        self.output.pack(fill=tk.BOTH, expand=True)
        self._append_log(
            "Automation starts OFF. LIVE mode submits real Binance Spot orders and can lose money.\n"
            "Use a dedicated API key, disable withdrawals, and restrict its IP. The app only calls Spot endpoints; Binance may group Spot and Margin permissions together.\n"
            "Never enter your Binance website password here or share API secrets in chat. Saved keys are DPAPI-encrypted for this Windows user.\n"
            "Market chart refreshes from public Binance candles; account refresh is manual and never sends orders.\n"
            "Trade-history requests are read-only, limited to 1,000 recent fills, and estimate FIFO realized P/L only when the fetched cost basis and fees are sufficient.\n"
            "Desktop alerts can be disabled with the toolbar checkbox and do not submit or cancel orders.\n"
            "AI analysis uses the local Ollama service; unavailable/invalid AI output or failed risk checks send no order.\n"
        )
        if self.saved_credentials_error:
            self._append_log(
                "Saved credentials were not loaded: "
                f"{self.saved_credentials_error}. Enter credentials again or forget the saved file."
            )
            self.status_var.set("Saved credentials could not be decrypted.")
        else:
            self.operation_progress.stop()
            self.operation_progress.configure(mode="indeterminate")
            self.operation_progress.pack_forget()
        self._animate_status()
        self.root.after(1200, self.refresh_market)
        self._schedule_market_refresh()
        self._schedule_live_price_refresh()

    def _update_dashboard_scrollregion(self, _event=None):
        self.dashboard_canvas.configure(
            scrollregion=self.dashboard_canvas.bbox("all")
        )

    def _resize_dashboard_content(self, event):
        self.dashboard_canvas.itemconfigure(
            self.dashboard_canvas_window, width=event.width
        )

    def _scroll_dashboard(self, event):
        if self.tabs.select() == str(self.dashboard_tab):
            self.dashboard_canvas.yview_scroll(
                int(-event.delta / 120), "units"
            )
            return "break"
        return None

    def _metric_card(self, parent, column, title, variable):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(12, 10))
        card.grid(row=0, column=column, sticky=tk.EW, padx=4)
        ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor=tk.W)
        metric = ttk.Label(card, textvariable=variable, style="Metric.TLabel")
        metric.pack(
            anchor=tk.W, pady=(5, 0)
        )
        if variable is self.market_delta_var:
            self.market_delta_label = metric

    def _animate_status(self):
        self.animation_step = (self.animation_step + 1) % 4
        running = bool(self.worker and self.worker.is_alive())
        color = (
            self.colors["green"]
            if self.start_button.instate(["disabled"])
            else self.colors["accent"]
            if running and self.animation_step % 2 == 0
            else "#49607d"
            if running
            else self.colors["muted"]
        )
        self.connection_dot.delete("all")
        self.connection_dot.create_oval(3, 3, 11, 11, fill=color, outline="")
        self.status_dot.delete("all")
        self.status_dot.create_oval(2, 2, 10, 10, fill=color, outline="")
        self.root.after(550, self._animate_status)

    def _load_saved_credentials(self):
        try:
            saved = self.credential_store.load()
        except (OSError, ValueError) as error:
            self.saved_credentials_error = str(error)
            return
        if saved is None:
            return
        modes = saved["credentials"]
        self.testnet_api_key_var.set(modes["TESTNET"]["api_key"])
        self.testnet_api_secret_var.set(modes["TESTNET"]["api_secret"])
        self.live_api_key_var.set(modes["LIVE"]["api_key"])
        self.live_api_secret_var.set(modes["LIVE"]["api_secret"])
        self.mode_var.set(saved["mode"])
        self.remember_credentials_var.set(True)

    def _credential_snapshot(self):
        return {
            "version": 1,
            "mode": self.mode_var.get(),
            "credentials": {
                "TESTNET": {
                    "api_key": self.testnet_api_key_var.get().strip(),
                    "api_secret": self.testnet_api_secret_var.get().strip(),
                },
                "LIVE": {
                    "api_key": self.live_api_key_var.get().strip(),
                    "api_secret": self.live_api_secret_var.get().strip(),
                },
            },
        }

    def _toggle_credential_visibility(self):
        show = "" if self.show_credentials_var.get() else "•"
        for entry in self.credential_entries:
            entry.configure(show=show)

    def forget_credentials(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(
                "Operation in progress",
                "Wait for the current account operation to finish before forgetting keys.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Forget saved credentials",
            "Delete this app's encrypted saved keys and clear them from the form?",
            parent=self.root,
        ):
            return
        try:
            self.credential_store.clear()
        except OSError as error:
            self._report_error("Could not remove saved credentials", error)
            return
        self.testnet_api_key_var.set("")
        self.testnet_api_secret_var.set("")
        self.live_api_key_var.set("")
        self.live_api_secret_var.set("")
        self.remember_credentials_var.set(False)
        self.status_var.set("Saved credentials forgotten.")
        self._append_log("Saved credentials removed from this Windows account.")

    def _start_progress(self, label):
        self.operation_var.set(label)
        self.operation_progress.pack(side=tk.RIGHT)
        self.operation_progress.configure(mode="indeterminate")
        self.operation_progress.start(12)

    def _stop_progress(self, label="Ready"):
        self.operation_progress.stop()
        self.operation_progress.pack_forget()
        self.operation_var.set(label)

    def _show_desktop_notification(self, title, message, risk=False):
        if not self.alerts_enabled_var.get():
            return
        self._dismiss_desktop_notification()
        window = tk.Toplevel(self.root)
        self.notification_window = window
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        frame = ttk.Frame(window, style="Card.TFrame", padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frame,
            text=title,
            style="CardTitle.TLabel",
            foreground=self.colors["red"] if risk else self.colors["accent"],
        ).pack(anchor=tk.W)
        ttk.Label(
            frame,
            text=message,
            wraplength=390,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(7, 10))
        ttk.Button(
            frame,
            text="Dismiss",
            command=self._dismiss_desktop_notification,
        ).pack(anchor=tk.E)
        width, height = 430, 150
        x = max(0, self.root.winfo_screenwidth() - width - 24)
        y = max(0, self.root.winfo_screenheight() - height - 70)
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.deiconify()
        window.lift()
        self.root.bell()
        self.notification_after = self.root.after(
            8000, self._dismiss_desktop_notification
        )

    def _dismiss_desktop_notification(self):
        if self.notification_after is not None:
            self.root.after_cancel(self.notification_after)
            self.notification_after = None
        if self.notification_window is not None:
            if self.notification_window.winfo_exists():
                self.notification_window.destroy()
            self.notification_window = None

    def _report_analysis_progress(self, percent, text):
        self.events.put(("analysis-progress", (percent, text)))

    def _calculate_active_trade_records(self, client):
        records = []
        for symbol, position in self.ledger.positions().items():
            quantity = float(position["quantity"])
            cost = float(position["cost_quote"])
            if quantity <= 0:
                continue
            try:
                price = float(client.ticker_price(symbol))
                pnl = calculate_unrealized_pnl(quantity, cost, price)
            except BinanceTestnetError:
                price = None
                pnl = None
            protection = position.get("protection", {"status": "unprotected"})
            records.append((symbol, quantity, cost, price, pnl, protection))
        return records

    def _render_active_trade_records(self, records):
        self.active_trade_records = list(records)
        for row in self.active_trade_tree.get_children():
            self.active_trade_tree.delete(row)
        for symbol, quantity, cost, price, pnl, protection in self.active_trade_records:
            protection_text = protection.get("status", "unprotected").replace("_", " ").upper()
            if protection.get("status") == "active":
                protection_text = (
                    f"ACTIVE · SL {protection.get('stop_price', 0):.8g} · "
                    f"TP {protection.get('take_profit_price', 0):.8g}"
                )
            elif protection.get("status") == "unknown":
                protection_text = "VERIFY ON BINANCE"
            self.active_trade_tree.insert(
                "",
                tk.END,
                values=(
                    symbol,
                    f"{quantity:.10g}",
                    f"{cost:.8g} USDT",
                    "Unavailable" if price is None else f"{price:.8g}",
                    "Unavailable" if pnl is None else f"{pnl:+.8g} USDT",
                    protection_text,
                ),
                tags=(
                    "profit" if pnl is not None and pnl >= 0 else "loss",
                    (
                        "protected"
                        if protection.get("status") == "active"
                        else "unprotected"
                    ),
                ),
            )
        self.active_trade_tree.tag_configure("profit", foreground=self.colors["green"])
        self.active_trade_tree.tag_configure("loss", foreground=self.colors["red"])
        self.active_trade_tree.tag_configure("protected", background="#17352f")
        self.active_trade_tree.tag_configure("unprotected", background="#44242d")
        if hasattr(self, "protect_position_button"):
            open_positions = [
                position
                for position in self.ledger.positions().values()
                if position["quantity"] > 0
            ]
            has_position = bool(open_positions)
            protection_status = (
                open_positions[0].get("protection", {}).get("status", "unprotected")
                if len(open_positions) == 1
                else "unknown"
            )
            self.close_position_button.configure(
                state=tk.NORMAL if has_position else tk.DISABLED
            )
            self.verify_position_button.configure(
                state=tk.NORMAL if has_position else tk.DISABLED
            )
            self.protect_position_button.configure(
                state=(
                    tk.NORMAL
                    if has_position and protection_status == "unprotected"
                    else tk.DISABLED
                )
            )

    def _sync_active_trades_from_ledger(self):
        existing = {row[0]: row for row in self.active_trade_records}
        records = []
        for symbol, position in self.ledger.positions().items():
            quantity = float(position["quantity"])
            if quantity <= 0:
                continue
            cost = float(position["cost_quote"])
            previous = existing.get(symbol)
            market_price = previous[3] if previous else None
            pnl = (
                calculate_unrealized_pnl(quantity, cost, market_price)
                if market_price is not None
                else None
            )
            records.append(
                (
                    symbol,
                    quantity,
                    cost,
                    market_price,
                    pnl,
                    position.get("protection", {"status": "unprotected"}),
                )
            )
        self._render_active_trade_records(records)

    def _refresh_active_trade_market_price(self, symbol, price):
        updated = []
        for row in self.active_trade_records:
            row_symbol, quantity, cost, previous_price, previous_pnl, protection = row
            if row_symbol == symbol:
                previous_price = price
                previous_pnl = (
                    calculate_unrealized_pnl(quantity, cost, price)
                    if price is not None
                    else None
                )
            updated.append(
                (row_symbol, quantity, cost, previous_price, previous_pnl, protection)
            )
        if updated != self.active_trade_records:
            self._render_active_trade_records(updated)

    def _show_prediction_progress(self, percent, text):
        self.prediction_progress.configure(value=percent)
        self.prediction_var.set(text)

    def _show_analysis_result(self, result):
        decision = result["decision"]
        self.last_analysis = result
        self.prediction_action_var.set(decision["action"])
        self.prediction_confidence_var.set(
            f"Model-reported confidence {decision['confidence']:.0%} · not calibrated"
        )
        self.prediction_progress.configure(value=100)
        self.prediction_var.set(
            "Analysis complete · "
            + ("order response received" if result.get("order") else "no order sent")
        )
        self.prediction_reason_var.set(decision["reason"])
        if result["indicators"]["last_price"] > 0:
            self._refresh_active_trade_market_price(
                result["symbol"], result["indicators"]["last_price"]
            )

    def _schedule_market_refresh(self):
        self.market_refresh_after = self.root.after(
            MARKET_REFRESH_MS, self._auto_refresh_market
        )

    def _update_refresh_label(self):
        self.market_time_var.set(
            "Chart refreshes every 60 seconds"
            if self.auto_refresh_var.get()
            else "Auto refresh paused"
        )

    def _auto_refresh_market(self):
        if self.auto_refresh_var.get():
            self.refresh_market(silent=True)
        self._schedule_market_refresh()

    def _toggle_live_quotes(self):
        if self.live_quotes_var.get():
            self.market_time_var.set("Live price polling starting…")
            self._schedule_live_price_refresh(delay=0)
        else:
            if self.live_price_after is not None:
                self.root.after_cancel(self.live_price_after)
                self.live_price_after = None
            self.market_time_var.set("Live price updates paused")

    def _schedule_live_price_refresh(self, delay=LIVE_PRICE_REFRESH_MS):
        if self.live_quotes_var.get() and self.live_price_after is None:
            self.live_price_after = self.root.after(
                delay, self._refresh_live_prices
            )

    def _live_price_symbols(self):
        symbols = [self.symbol_var.get().strip().upper()]
        if hasattr(self, "market_scan_tree"):
            symbols.extend(
                self.market_scan_tree.item(item, "values")[0]
                for item in self.market_scan_tree.get_children()
            )
        symbols.extend(
            symbol
            for symbol, position in self.ledger.positions().items()
            if position["quantity"] > 0
        )
        validated = []
        for symbol in symbols:
            try:
                validated.append(BinanceSpotTestnet.validate_symbol(symbol))
            except (AttributeError, BinanceTestnetError):
                continue
        return list(dict.fromkeys(validated))[:100]

    def _refresh_live_prices(self):
        self.live_price_after = None
        if not self.live_quotes_var.get():
            return
        if self.live_price_worker and self.live_price_worker.is_alive():
            self._schedule_live_price_refresh()
            return
        symbols = self._live_price_symbols()
        if not symbols:
            self.market_time_var.set("Live prices unavailable · no valid symbols")
            self._schedule_live_price_refresh()
            return
        selected_mode = self.mode_var.get()

        def worker():
            try:
                client_type = (
                    BinanceSpotLive
                    if selected_mode == "LIVE"
                    else BinanceSpotTestnet
                )
                prices = client_type().ticker_prices(symbols)
                self.events.put(
                    ("live-prices", (selected_mode, prices, time.time()))
                )
            except Exception as error:
                self.events.put(
                    ("live-price-error", (selected_mode, str(error), time.time()))
                )

        self.live_price_worker = threading.Thread(
            target=worker, name="live-price-refresh", daemon=True
        )
        self.live_price_worker.start()
        self._schedule_live_price_refresh()

    def _render_live_prices(self, payload):
        mode, prices, received_at = payload
        if mode != self.mode_var.get():
            return
        self.live_prices = prices
        if prices:
            self.last_live_price_error = None
        selected_symbol = self.symbol_var.get().strip().upper()
        selected_price = prices.get(selected_symbol)
        if selected_price is not None:
            price = float(selected_price)
            self.price_var.set(f"{price:,.8g}")
            if self.market_candles and self.market_candles[0]["open"] > 0:
                change = (
                    price / self.market_candles[0]["open"] - 1
                ) * 100
                self.market_delta_var.set(f"{change:+.2f}%")
                self.market_delta_label.configure(
                    foreground=(
                        self.colors["green"]
                        if change >= 0
                        else self.colors["red"]
                    )
                )
            if self.market_chart.symbol == selected_symbol:
                try:
                    self.market_chart.update_live_price(price)
                except (TypeError, ValueError, tk.TclError) as error:
                    self._append_log(f"Live chart update failed: {error}")
        else:
            self.price_var.set("Live quote unavailable")
        for active_symbol, position in self.ledger.positions().items():
            if position["quantity"] > 0:
                self._refresh_active_trade_market_price(
                    active_symbol,
                    (
                        float(prices[active_symbol])
                        if active_symbol in prices
                        else None
                    ),
                )
        for item in self.market_scan_tree.get_children():
            values = list(self.market_scan_tree.item(item, "values"))
            symbol = values[0]
            values[1] = (
                f"{float(prices[symbol]):,.8g}" if symbol in prices else "—"
            )
            self.market_scan_tree.item(item, values=values)
        updated_at = datetime.fromtimestamp(received_at).strftime("%H:%M:%S")
        self.market_time_var.set(
            f"LIVE PRICE · {updated_at} · {mode}"
            if prices
            else f"LIVE PRICE UNAVAILABLE · {mode}"
        )
        if not prices:
            no_quote_message = (
                f"{mode} did not return live ticker prices for the watched symbols."
            )
            if (
                no_quote_message != self.last_live_price_error
                or received_at - self.live_price_error_logged_at >= 60
            ):
                self._append_log(no_quote_message)
                self.last_live_price_error = no_quote_message
                self.live_price_error_logged_at = received_at

    def _make_market_client(self, mode=None):
        selected_mode = mode or self.mode_var.get()
        client_type = BinanceSpotLive if selected_mode == "LIVE" else BinanceSpotTestnet
        api_key = ""
        api_secret = ""
        if selected_mode == "LIVE":
            api_key = self.live_api_key_var.get().strip()
            api_secret = self.live_api_secret_var.get().strip()
        else:
            api_key = self.testnet_api_key_var.get().strip()
            api_secret = self.testnet_api_secret_var.get().strip()
        return client_type(api_key, api_secret)

    @staticmethod
    def _backtest_default(variable, fallback):
        try:
            value = float(variable.get())
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 and value < float("inf") else fallback

    def open_backtest_dialog(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(
                "Busy", "Wait for the current operation to finish.", parent=self.root
            )
            return
        starting_cash = 1000.0
        quote_per_trade = min(
            self._backtest_default(self.max_order_var, 10.0),
            starting_cash * 0.5,
        )
        daily_drawdown = self._backtest_default(self.daily_loss_var, 5.0)
        if daily_drawdown >= starting_cash:
            daily_drawdown = 5.0
        defaults = (
            ("Starting capital (USDT)", starting_cash),
            ("Quote amount per entry (USDT)", quote_per_trade),
            ("Intraday drawdown stop (USDT)", daily_drawdown),
            ("Stop-loss from entry (%)", self._backtest_default(self.stop_loss_pct_var, 2.0)),
            ("Take-profit from entry (%)", self._backtest_default(self.take_profit_pct_var, 4.0)),
            ("Taker fee estimate (basis points)", 10.0),
            ("Slippage estimate per side (basis points)", 5.0),
        )
        dialog = tk.Toplevel(self.root)
        dialog.title("Historical backtest · simulation only")
        dialog.transient(self.root)
        dialog.configure(background=self.colors["background"])
        dialog.resizable(False, False)
        content = ttk.Frame(dialog, padding=18)
        content.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            content,
            text="Simulation settings",
            font=("Segoe UI", 16, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))
        fields = {}
        for index, (label, value) in enumerate(defaults):
            row = 1 + index
            ttk.Label(content, text=label, style="Muted.TLabel").grid(
                row=row, column=0, sticky=tk.W, padx=(0, 14), pady=4
            )
            variable = tk.StringVar(value=f"{value:g}")
            entry = ttk.Entry(content, textvariable=variable, width=17)
            entry.grid(row=row, column=1, sticky=tk.EW, pady=4)
            fields[label] = variable
        ttk.Label(
            content,
            text=(
                "Uses up to 1,000 public Binance candles for the selected interval. "
                "Signals use a deterministic EMA/RSI technical proxy—not historical "
                "Ollama decisions. Fees/slippage are estimates; this simulation cannot "
                "model order-book depth, exact fills, or exchange outages. If both "
                "exits touch in one candle, it assumes the stop-loss happens first."
            ),
            style="Muted.TLabel",
            wraplength=480,
            justify=tk.LEFT,
        ).grid(row=8, column=0, columnspan=2, sticky=tk.W, pady=(10, 12))
        buttons = ttk.Frame(content)
        buttons.grid(row=9, column=0, columnspan=2, sticky=tk.E)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(
            side=tk.RIGHT, padx=(7, 0)
        )

        def begin_backtest():
            labels = [label for label, _value in defaults]
            try:
                settings = validate_backtest_settings(
                    *(float(fields[label].get()) for label in labels)
                )
            except (ValueError, TypeError) as error:
                messagebox.showwarning(
                    "Invalid simulation settings", str(error), parent=dialog
                )
                return
            symbol = self.symbol_var.get().strip().upper()
            interval = self.interval_var.get()
            if self._launch(
                "backtest",
                lambda: self._run_backtest_worker(
                    symbol, interval, settings
                ),
            ):
                dialog.grab_release()
                dialog.destroy()

        ttk.Button(
            buttons,
            text="Run backtest",
            command=begin_backtest,
            style="Accent.TButton",
        ).pack(side=tk.RIGHT)
        dialog.bind("<Return>", lambda _event: begin_backtest())
        dialog.grab_set()
        dialog.wait_visibility()
        dialog.focus_set()

    def _run_backtest_worker(self, symbol, interval, settings):
        self.events.put(
            (
                "backtest-progress",
                f"Fetching up to {1000} public Binance {symbol} {interval} candles…",
            )
        )
        try:
            client = BinanceSpotLive()
            candles = client.klines(symbol, interval=interval, limit=1000)
            self.events.put(("backtest-progress", f"Simulating {len(candles)} candles…"))
            report = run_backtest(candles, **settings)
            self.events.put(
                (
                    "backtest-result",
                    {
                        **report,
                        "symbol": symbol,
                        "interval": interval,
                        "settings": settings,
                    },
                )
            )
        except Exception as error:
            self.events.put(("error", f"Backtest failed; no order was sent: {error}"))

    @staticmethod
    def _format_backtest_report(report):
        start = datetime.fromtimestamp(
            report["start_close_time"] / 1000, timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        end = datetime.fromtimestamp(
            report["end_close_time"] / 1000, timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        return (
            f"{report['symbol']} · {report['interval']} · {report['candles']} candles\n"
            f"Period: {start} — {end}\n\n"
            f"Technical proxy: {report['return_pct']:+.2f}% "
            f"(final {report['final_equity_usdt']:.2f} USDT)\n"
            f"Buy & hold: {report['buy_hold_return_pct']:+.2f}% "
            f"(final {report['buy_hold_equity_usdt']:.2f} USDT)\n"
            f"Max simulated drawdown: {report['max_drawdown_usdt']:.2f} USDT "
            f"({report['max_drawdown_pct']:.2f}%)\n"
            f"Closed trades: {report['closed_trades']} · "
            f"win rate: {report['win_rate_pct']:.1f}%\n"
            f"Stop exits: {report['stop_exits']} · take-profit exits: "
            f"{report['take_profit_exits']} · signal exits: {report['signal_exits']}\n"
            f"Strategy fees estimate: {report['fees_paid_usdt']:.4f} USDT\n"
            f"Drawdown stop: {report['settings']['daily_drawdown_stop']:.2f} USDT · "
            f"exit triggers: -{report['settings']['stop_loss_pct']:.2f}% / "
            f"+{report['settings']['take_profit_pct']:.2f}%\n"
            f"Entry size: {report['settings']['quote_per_trade']:.2f} USDT · "
            f"fee: {report['settings']['fee_bps']:.1f} bps · "
            f"slippage: {report['settings']['slippage_bps']:.1f} bps/side\n\n"
            "Research simulation only—not the Ollama strategy, a live result, or a "
            "profitability forecast. Candle-based fills and fees are approximate; "
            "a candle touching both exits is counted as a stop-loss first."
        )

    def refresh_market(self, silent=False):
        if self.worker and self.worker.is_alive():
            if not silent:
                self.status_var.set("Market refresh queued after the current operation.")
            return
        symbol = self.symbol_var.get().strip().upper()
        interval = self.interval_var.get()
        selected_mode = self.mode_var.get()
        client = self._make_market_client(selected_mode)
        self.refresh_market_button.configure(state=tk.DISABLED)

        def worker():
            try:
                candles = client.klines(symbol, interval=interval, limit=100)
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                indicators = calculate_indicators(
                    [
                        candle
                        for candle in candles
                        if candle["close_time"] < now_ms
                    ]
                )
                active_prices = []
                for active_symbol, position in self.ledger.positions().items():
                    if position["quantity"] <= 0:
                        continue
                    try:
                        active_prices.append(
                            (active_symbol, float(client.ticker_price(active_symbol)))
                        )
                    except BinanceTestnetError:
                        active_prices.append((active_symbol, None))
                self.events.put(
                    (
                        "market-data",
                        (
                            selected_mode,
                            symbol,
                            interval,
                            candles,
                            indicators,
                            active_prices,
                            silent,
                        ),
                    )
                )
            except Exception as error:
                self.events.put(("market-error", f"Market data refresh failed: {error}"))

        if self._launch("market-refresh", worker):
            self.chart_status_var.set(f"Loading {symbol} {interval} candles…")
        else:
            self.refresh_market_button.configure(state=tk.NORMAL)

    def _render_market(self, payload):
        mode, symbol, interval, candles, indicators, active_prices, silent = payload
        self.market_candles = candles
        self.market_chart.set_data(symbol, interval, candles)
        self.price_var.set(f"{indicators['last_price']:,.8g}")
        if len(candles) > 1 and candles[0]["open"] > 0:
            change = (candles[-1]["close"] / candles[0]["open"] - 1) * 100
            self.market_delta_var.set(f"{change:+.2f}%")
            delta_color = self.colors["green"] if change >= 0 else self.colors["red"]
        else:
            self.market_delta_var.set("—")
            delta_color = self.colors["muted"]
        self.market_delta_label.configure(foreground=delta_color)
        self.market_time_var.set(datetime.now().strftime("Updated %H:%M:%S"))
        self.chart_status_var.set(
            f"{mode} · {len(candles)} candles · EMA20 {indicators['ema20']:,.6g} · "
            f"EMA50 {indicators['ema50']:,.6g} · RSI {indicators['rsi14']:.1f}"
        )
        self.connection_label_var.set(f"{mode} MARKET DATA")
        for active_symbol, active_price in active_prices:
            self._refresh_active_trade_market_price(active_symbol, active_price)
        if not silent:
            self._append_log(
                f"Market data refreshed: {symbol} {interval}, latest closed price "
                f"{indicators['last_price']:.8g}."
            )

    def clear_logs(self):
        self.output.configure(state=tk.NORMAL)
        self.output.delete("1.0", tk.END)
        self.output.configure(state=tk.DISABLED)

    def copy_log(self):
        text = self.output.get("1.0", tk.END).strip()
        if not text:
            self.status_var.set("There are no logs to copy.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set("Activity log copied to clipboard.")

    def export_balances(self):
        if not self.balance_records:
            messagebox.showinfo(
                "No account records",
                "Connect to an account and refresh balances before exporting.",
                parent=self.root,
            )
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export account balance snapshot",
            defaultextension=".csv",
            filetypes=(("CSV file", "*.csv"),),
            initialfile=f"astra_balances_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as output_file:
                writer = csv.writer(output_file)
                writer.writerow(("captured_at_utc", "mode", "asset", "free", "locked", "total"))
                captured_at = datetime.now(timezone.utc).isoformat()
                for asset, free, locked in self.balance_records:
                    writer.writerow(
                        (captured_at, self.mode_var.get(), asset, free, locked, free + locked)
                    )
        except OSError as error:
            self._report_error("Could not export balance snapshot", error)
            return
        self.status_var.set("Balance snapshot exported.")

    def load_trade_history(self):
        try:
            self._credentials()
            mode = self.mode_var.get()
            client = self._make_market_client(mode)
            symbol = client.validate_symbol(self.symbol_var.get())
        except ValueError as error:
            messagebox.showwarning(
                "Binance credentials required", str(error), parent=self.root
            )
            return

        def worker():
            try:
                rules = client.symbol_rules(symbol)
                trades = client.my_trades(symbol, limit=1000)
                records = parse_trade_history(
                    trades, rules["base_asset"], rules["quote_asset"]
                )
                self.events.put(
                    (
                        "trade-history",
                        (mode, symbol, rules["quote_asset"], records),
                    )
                )
            except Exception as error:
                self.events.put(
                    ("trade-history-error", f"Could not load {symbol} fills: {error}")
                )

        if self._launch("trade-history", worker):
            self.load_history_button.configure(state=tk.DISABLED)
            self.history_status_var.set(f"Loading recent {symbol} fills from Binance…")

    def _render_trade_history(self, mode, symbol, quote_asset, records):
        self.trade_history_mode = mode
        self.trade_history_symbol = symbol
        self.trade_history_quote_asset = quote_asset
        self.trade_history_records = records
        for row in self.trade_history_tree.get_children():
            self.trade_history_tree.delete(row)
        for record in reversed(records):
            timestamp = datetime.fromtimestamp(
                record["time"] / 1000, tz=timezone.utc
            ).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            pnl = record["realized_pnl"]
            pnl_text = "—" if pnl is None else f"{pnl:+,.6f} {quote_asset}"
            self.trade_history_tree.insert(
                "",
                tk.END,
                values=(
                    timestamp,
                    record["side"],
                    f"{record['price']:g}",
                    f"{record['quantity']:g}",
                    f"{record['quote_quantity']:g}",
                    f"{record['commission']:g} {record['commission_asset']}",
                    pnl_text,
                ),
                tags=(
                    "profit" if pnl is not None and pnl > 0
                    else "loss" if pnl is not None and pnl < 0
                    else "neutral",
                ),
            )
        self.trade_history_tree.tag_configure(
            "profit", foreground=self.colors["green"]
        )
        self.trade_history_tree.tag_configure("loss", foreground=self.colors["red"])
        self.export_history_button.configure(
            state=tk.NORMAL if records else tk.DISABLED
        )
        self.history_status_var.set(
            f"{mode} · {len(records)} recent {symbol} fills loaded · FIFO realized P/L "
            "uses only the fetched fills"
        )

    def export_trade_history(self):
        if not self.trade_history_records:
            self.status_var.set("Load Binance fills before exporting trade history.")
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export Binance trade history",
            defaultextension=".csv",
            filetypes=(("CSV file", "*.csv"),),
            initialfile=(
                f"astra_{self.trade_history_symbol}_fills_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            ),
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as output_file:
                writer = csv.writer(output_file)
                writer.writerow(
                    (
                        "time_utc",
                        "mode",
                        "symbol",
                        "trade_id",
                        "order_id",
                        "side",
                        "price",
                        "base_quantity",
                        f"quote_quantity_{self.trade_history_quote_asset}",
                        "commission",
                        "commission_asset",
                        f"fifo_realized_pnl_{self.trade_history_quote_asset}",
                    )
                )
                for record in self.trade_history_records:
                    timestamp = datetime.fromtimestamp(
                        record["time"] / 1000, tz=timezone.utc
                    ).isoformat()
                    writer.writerow(
                        (
                            timestamp,
                            self.trade_history_mode,
                            self.trade_history_symbol,
                            record["id"],
                            record["order_id"],
                            record["side"],
                            str(record["price"]),
                            str(record["quantity"]),
                            str(record["quote_quantity"]),
                            str(record["commission"]),
                            record["commission_asset"],
                            (
                                ""
                                if record["realized_pnl"] is None
                                else str(record["realized_pnl"])
                            ),
                        )
                    )
        except OSError as error:
            self._report_error("Could not export trade history", error)
            return
        self.status_var.set("Binance trade history exported.")

    def open_api_setup(self):
        open_page = messagebox.askyesno(
            "Connect with a Binance API key",
            "This desktop app connects using an API key you create on Binance; it does not "
            "use Binance website-password login or OAuth.\n\n"
            "Create a dedicated key, enable reading and the required trading permission, "
            "disable withdrawals, and restrict the key to your trusted IP. Keep the secret "
            "private and enter it only in this app. If Remember last connected keys is "
            "selected, credentials are encrypted with Windows DPAPI for this user.\n\n"
            "Open Binance API management in your browser?",
            parent=self.root,
        )
        if open_page:
            try:
                if not webbrowser.open(BINANCE_API_MANAGEMENT_URL):
                    raise RuntimeError("Windows could not open the Binance API page.")
            except (OSError, RuntimeError) as error:
                self._report_error("Could not open Binance API management", error)

    def _position_summary(self):
        try:
            pnl = self.ledger.realized_pnl_today()
            positions = []
            for symbol, position in self.ledger.positions().items():
                if position["quantity"] <= 0:
                    continue
                protection = position.get("protection", {"status": "unprotected"})
                status = protection.get("status", "unprotected").replace("_", " ").upper()
                if protection.get("status") == "active":
                    status = (
                        f"ACTIVE (stop {protection.get('stop_price', 0):.8g}, "
                        f"target {protection.get('take_profit_price', 0):.8g})"
                    )
                positions.append(
                    f"{symbol}: {position['quantity']:.8f} "
                    f"(cost {position['cost_quote']:.4f} quote units; exits {status})"
                )
            position_text = "; ".join(positions) if positions else "none"
            return (
                f"Bot-managed {self.mode_var.get()} position(s): {position_text} · "
                f"Today's estimated realized P/L: {pnl:.4f} quote units "
                "(commissions may be incomplete)"
            )
        except (LedgerError, ValueError):
            return "Bot-managed position / realized PnL unavailable."

    def _render_equity_risk(self, risk, error=None):
        if not risk:
            self.account_equity_var.set("Unavailable" if error else "Not valued")
            self.daily_loss_from_start_var.set("Unavailable" if error else "LIVE only")
            self.drawdown_var.set("Unavailable" if error else "LIVE only")
            if error:
                self._append_log(f"Total account equity could not be valued: {error}")
            return
        self.account_equity_var.set(f"{risk['equity_usdt']:,.4f} USDT")
        if risk.get("is_live") or "loss_from_start_usdt" in risk:
            self.daily_loss_from_start_var.set(
                f"{risk['loss_from_start_usdt']:,.4f} USDT"
            )
            self.drawdown_var.set(f"{risk['drawdown_usdt']:,.4f} USDT")
        else:
            self.daily_loss_from_start_var.set("Testnet · no live risk stop")
            self.drawdown_var.set("Testnet · no live risk stop")

    def _get_ledger(self, mode, api_key):
        try:
            path = resolve_ledger_path(
                mode,
                APP_DIR,
                SOURCE_DIR,
                migrate_legacy=APP_DIR == APP_DATA_DIR,
            )
        except OSError as error:
            raise LedgerError(str(error)) from error
        if path not in self.ledger_cache:
            self.ledger_cache[path] = TradeLedger(path)
        ledger = self.ledger_cache[path]
        if mode == "LIVE" and api_key:
            fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            ledger.bind_account(fingerprint)
        return ledger

    def _selected_credentials(self):
        if self.mode_var.get() == "LIVE":
            return (
                self.live_api_key_var.get().strip(),
                self.live_api_secret_var.get().strip(),
            )
        return (
            self.testnet_api_key_var.get().strip(),
            self.testnet_api_secret_var.get().strip(),
        )

    def _on_mode_changed(self, reset_limits=True):
        mode = self.mode_var.get()
        if mode != self.active_mode and self.worker and self.worker.is_alive():
            self.mode_var.set(self.active_mode)
            messagebox.showinfo(
                "Operation in progress",
                "Wait for the current request to finish before changing exchange mode.",
                parent=self.root,
            )
            return
        self.active_mode = mode
        if reset_limits and hasattr(self, "max_order_var"):
            if mode == "LIVE":
                self.max_order_var.set("")
                self.daily_loss_var.set("")
            else:
                self.max_order_var.set("10")
                self.daily_loss_var.set("5")
        api_key, _api_secret = self._selected_credentials()
        try:
            self.ledger = self._get_ledger(mode, api_key)
        except LedgerError as error:
            if hasattr(self, "status_var"):
                self.status_var.set(str(error))
            return

        if mode == "LIVE":
            notice = (
                "LIVE can place real orders. Set your own trade and daily-loss limits. "
                "Disable withdrawals and restrict the dedicated key by IP. Binance may "
                "group Spot and Margin permissions; this app calls Spot endpoints only. "
                "Credentials stay in memory."
            )
        else:
            notice = (
                "TESTNET uses simulated funds. Use only Testnet keys here; credentials stay in memory."
            )
        self.credential_notice_var.set(notice)
        if hasattr(self, "mode_banner_var"):
            if mode == "LIVE":
                self.mode_banner_var.set(
                    "LIVE · REAL BINANCE FUNDS  |  Pausing the bot does not cancel exchange exit orders."
                )
                self.mode_banner.configure(bg="#713541", fg="#fff0f2")
            else:
                self.mode_banner_var.set(
                    "TESTNET · SIMULATED FUNDS  |  Testnet activity does not predict live results."
                )
                self.mode_banner.configure(bg="#123d5b", fg="#d9f1ff")
        if hasattr(self, "account_var"):
            self.account_var.set(f"{mode}: not connected.")
        if hasattr(self, "position_var"):
            self.position_var.set(self._position_summary())
        if hasattr(self, "status_var"):
            self.status_var.set(
                "LIVE real-money execution requires explicit confirmation."
                if mode == "LIVE"
                else "Testnet mode selected; automation is off."
            )
        if hasattr(self, "balance_tree"):
            self.balance_records = []
            self._render_active_trade_records([])
            for row in self.balance_tree.get_children():
                self.balance_tree.delete(row)
            self.balance_total_var.set("Connect an account to view balances")
            self.account_equity_var.set("Not valued")
            self.daily_loss_from_start_var.set("LIVE only")
            self.drawdown_var.set("LIVE only")
            self.refresh_market(silent=True)

    def _render_market_scan(self, payload):
        for item in self.market_scan_tree.get_children():
            self.market_scan_tree.delete(item)
        selected_symbol = payload.get("selected_symbol")
        for row in payload.get("markets", []):
            score = row.get("score")
            rsi = row.get("rsi")
            momentum = row.get("momentum")
            values = (
                row["symbol"],
                (
                    f"{float(self.live_prices[row['symbol']]):,.8g}"
                    if row["symbol"] in self.live_prices
                    else "—"
                ),
                f"{row['quote_volume']:.4g}",
                f"{score:.2f}" if score is not None else "—",
                f"{rsi:.2f}" if rsi is not None else "—",
                f"{momentum * 100:+.3f}%" if momentum is not None else "—",
                row.get("status", ""),
            )
            tags = []
            if row.get("eligible"):
                tags.append("candidate")
            if row["symbol"] == selected_symbol:
                tags.append("selected")
            self.market_scan_tree.insert("", tk.END, values=values, tags=tags)
        status = payload.get("status", "Market scan updated.")
        if payload.get("errors"):
            status += f" · {len(payload['errors'])} market-data issue(s)"
        self.scanner_status_var.set(status)
        self.status_var.set(status)

    def _append_log(self, text):
        self.output.configure(state=tk.NORMAL)
        self.output.insert(tk.END, text.rstrip() + "\n\n")
        self.output.see(tk.END)
        self.output.configure(state=tk.DISABLED)

    def _report_error(self, title, error):
        message = f"{title}: {error}"
        self.status_var.set(message)
        self._append_log(message)
        messagebox.showerror(title, str(error), parent=self.root)

    def _credentials(self):
        api_key, api_secret = self._selected_credentials()
        if not api_key or not api_secret:
            environment = "LIVE" if self.mode_var.get() == "LIVE" else "Spot Testnet"
            raise ValueError(f"Enter API credentials created for Binance {environment}.")
        return api_key, api_secret

    def _make_engine(self, require_credentials=True, require_limits=False, symbol=None):
        if require_credentials:
            api_key, api_secret = self._credentials()
        else:
            api_key, api_secret = self._selected_credentials()
        symbol = (symbol or self.symbol_var.get()).strip().upper()
        model = self.model_var.get().strip()
        try:
            max_order = float(self.max_order_var.get())
            daily_loss = float(self.daily_loss_var.get())
            stop_loss_pct = float(self.stop_loss_pct_var.get())
            take_profit_pct = float(self.take_profit_pct_var.get())
        except ValueError:
            if require_limits:
                raise ValueError(
                    "Set positive USDT trade/daily-loss limits and both protective-exit percentages."
                )
            max_order, daily_loss = 10.0, 5.0
            stop_loss_pct, take_profit_pct = 2.0, 4.0
        client_type = BinanceSpotLive if self.mode_var.get() == "LIVE" else BinanceSpotTestnet
        client = client_type(api_key, api_secret)
        analyzer = OllamaAnalyzer(model=model)
        ledger = self._get_ledger(self.mode_var.get(), api_key)
        self.ledger = ledger
        engine = TradingEngine(
            client,
            analyzer,
            ledger,
            symbol=symbol,
            max_trade_quote=max_order,
            daily_loss_limit=daily_loss,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            interval=self.trade_interval_var.get(),
        )
        if require_limits and (max_order <= 0 or daily_loss <= 0):
            raise ValueError("Set positive USDT risk limits before starting automation.")
        return engine

    def _launch(self, name, target):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Another operation is already running.", parent=self.root)
            return False
        self.worker = threading.Thread(target=target, name=name, daemon=True)
        self.worker.start()
        self._start_progress(f"{name.replace('-', ' ').title()} in progress…")
        return True

    def connect_account(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Wait for the current operation to finish.", parent=self.root)
            return
        try:
            api_key, api_secret = self._credentials()
            self.ledger = self._get_ledger(self.mode_var.get(), api_key)
            self.position_var.set(self._position_summary())
        except (ValueError, LedgerError) as error:
            messagebox.showwarning("Binance credentials required", str(error), parent=self.root)
            return
        selected_mode = self.mode_var.get()
        model_name = self.model_var.get().strip()
        remember_credentials = self.remember_credentials_var.get()
        credential_snapshot = self._credential_snapshot()

        def worker():
            try:
                self.events.put(("analysis-progress", (10, "Connecting to Binance account")))
                client_type = BinanceSpotLive if selected_mode == "LIVE" else BinanceSpotTestnet
                client = client_type(api_key, api_secret)
                client.ping()
                account = client.account()
                if client.IS_LIVE:
                    self.events.put(("analysis-progress", (48, "Checking LIVE API-key restrictions")))
                    restrictions = client.api_key_restrictions()
                    validate_live_api_restrictions(restrictions)
                    profile = format_account_profile(account, restrictions)
                    message = "Connected to Binance LIVE account (private Spot data)"
                else:
                    profile = format_account_profile(account)
                    usdt_free = TradingEngine._balance(account, "USDT")
                    message = f"Connected to Binance Spot Testnet. Free USDT: {usdt_free:.4f}"
                balances = format_account_balances(account)
                balance_records = parse_account_balances(account)
                equity_summary = None
                equity_error = None
                try:
                    equity = float(client.account_equity_usdt(account))
                    if client.IS_LIVE:
                        equity_summary = self.ledger.observe_account_equity(equity)
                        equity_summary["is_live"] = True
                    else:
                        equity_summary = {"equity_usdt": equity, "is_live": False}
                except BinanceTestnetError as error:
                    equity_error = str(error)
                protection_messages = []
                for symbol, position in self.ledger.positions().items():
                    if position["quantity"] <= 0:
                        continue
                    engine = TradingEngine(
                        client,
                        OllamaAnalyzer(model=model_name),
                        self.ledger,
                        symbol=symbol,
                    )
                    try:
                        protection_messages.append(
                            (symbol, engine.verify_protection_status())
                        )
                    except Exception as error:
                        protection_messages.append(
                            (symbol, f"Exit status could not be confirmed: {error}")
                        )
                active_trades = self._calculate_active_trade_records(client)
                save_error = None
                try:
                    if remember_credentials:
                        self.credential_store.save(credential_snapshot)
                    else:
                        self.credential_store.clear()
                except (OSError, ValueError) as error:
                    save_error = str(error)
                self.events.put(
                    (
                        "connected",
                        (
                            selected_mode,
                            message,
                            profile,
                            balances,
                            balance_records,
                            equity_summary,
                            equity_error,
                            protection_messages,
                            active_trades,
                            save_error,
                            remember_credentials,
                        ),
                    )
                )
            except Exception as error:
                self.events.put(("error", f"Binance {selected_mode} account check failed: {error}"))

        if self._launch("connect-account", worker):
            self.status_var.set(
                f"Checking the fixed Binance Spot {selected_mode} endpoint…"
            )

    def analyze_only(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Wait for the current operation to finish.", parent=self.root)
            return
        try:
            engine = self._make_engine(require_credentials=False)
        except (ValueError, LedgerError) as error:
            messagebox.showwarning("Check configuration", str(error), parent=self.root)
            return

        def worker():
            try:
                result = engine.run_cycle(
                    execute_orders=False,
                    progress_callback=self._report_analysis_progress,
                )
                self.events.put(("result", result))
            except Exception as error:
                self.events.put(("error", f"Analysis failed; no order was sent: {error}"))

        if self._launch("analyze", worker):
            self.status_var.set(
                f"Fetching Binance {self.mode_var.get()} market candles and asking local Ollama…"
            )

    def start_automation(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Wait for the current operation to finish.", parent=self.root)
            return
        try:
            engine = self._make_engine(require_limits=True)
        except (ValueError, LedgerError) as error:
            messagebox.showwarning("Check configuration", str(error), parent=self.root)
            return
        if self.ledger.pending_order:
            messagebox.showwarning(
                "Order reconciliation required",
                "A previous order has an unknown result. Reconcile it before starting automation.",
                parent=self.root,
            )
            return
        open_positions = {
            symbol: position
            for symbol, position in self.ledger.positions().items()
            if position["quantity"] > 0
        }
        if len(open_positions) > 1:
            messagebox.showwarning(
                "Multiple managed positions need attention",
                "Automatic mode supports one bot-managed position at a time. "
                "Review and resolve the recorded positions before restarting.",
                parent=self.root,
            )
            return
        if open_positions:
            managed_symbol, managed_position = next(iter(open_positions.items()))
            if managed_position.get("protection", {}).get("status") != "active":
                messagebox.showwarning(
                    "Position needs attention",
                    "The bot-managed position does not have confirmed active exchange exits. "
                    "Use Protect open position or Close managed position before restarting automation.",
                    parent=self.root,
                )
                return
            engine.symbol = managed_symbol
        if getattr(engine.client, "IS_LIVE", False):
            accepted = messagebox.askyesno(
                "REAL MONEY — confirm Binance LIVE trading",
                "The selected mode will send real Binance Spot MARKET orders.\n\n"
                f"Maximum buy per trade: {engine.max_trade_quote:g} USDT\n"
                f"Maximum intraday peak-to-current equity drawdown: "
                f"{engine.daily_loss_limit:g} USDT\n\n"
                f"Signal timeframe: {engine.interval}\n"
                "When flat, the scanner checks the top 20 liquid USDT Spot pairs and "
                "evaluates only the best technical candidate. It manages one position at a time.\n"
                f"Exchange exits: stop {engine.stop_loss_pct:g}% below and "
                f"take-profit {engine.take_profit_pct:g}% above average entry.\n"
                "Triggered market exits can slip; trigger prices are not guaranteed fill prices.\n\n"
                "A loss stop halts automation and blocks further orders; it does NOT sell "
                "your position. AI can be wrong and you can lose money. Start live trading?",
                parent=self.root,
                icon=messagebox.WARNING,
            )
            if not accepted:
                return
            confirmation = simpledialog.askstring(
                "Type LIVE to arm real orders",
                "Type LIVE exactly to enable automatic real-money orders for this run.",
                parent=self.root,
            )
            if confirmation != "LIVE":
                messagebox.showinfo("Live trading not armed", "No live orders were enabled.")
                return
        else:
            accepted = messagebox.askyesno(
                "Start Testnet automation",
                "This starts automatic MARKET orders using simulated Binance Spot Testnet funds only.\n\n"
                "When flat, the scanner checks the top 20 liquid USDT Spot pairs and "
                "evaluates only the best technical candidate. It manages one position at a time.\n"
                f"Signals use {engine.interval} closed candles; exits use the configured take-profit and stop-loss.\n\n"
                "Testnet results do not predict profitability on a real account.\n\nStart the bot?",
                parent=self.root,
            )
            if not accepted:
                return

        self.stop_event.clear()
        if self._launch("testnet-automation", lambda: self._automation_loop(engine)):
            self.start_button.configure(state=tk.DISABLED)
            self.stop_button.configure(state=tk.NORMAL)
            self.status_var.set(
                f"Automatic Binance Spot {engine.client.ENVIRONMENT_NAME} trading is running "
                f"on {engine.interval} candles."
            )

    @staticmethod
    def _engine_for_symbol(template, symbol):
        return TradingEngine(
            template.client,
            template.analyzer,
            template.ledger,
            symbol=symbol,
            max_trade_quote=template.max_trade_quote,
            daily_loss_limit=template.daily_loss_limit,
            stop_loss_pct=template.stop_loss_pct,
            take_profit_pct=template.take_profit_pct,
            interval=template.interval,
        )

    def _automation_loop(self, engine):
        self.events.put(("automation", True))
        try:
            scanner = SpotMarketScanner(engine.client)
            engines = {engine.symbol: engine}
            last_scan = {"markets": [], "status": "Waiting for first market scan."}
            while not self.stop_event.is_set():
                open_positions = {
                    symbol: position
                    for symbol, position in engine.ledger.positions().items()
                    if position["quantity"] > 0
                }
                if len(open_positions) > 1:
                    raise RuntimeError(
                        "Multiple bot-managed positions were found; automatic mode supports one."
                    )
                scan_candidate = None
                if open_positions:
                    symbol, _position = next(iter(open_positions.items()))
                    active_engine = engines.get(symbol)
                    if active_engine is None:
                        active_engine = self._engine_for_symbol(engine, symbol)
                        engines[symbol] = active_engine
                    scan_view = {
                        "markets": [
                            {
                                "symbol": symbol,
                                "quote_volume": 0.0,
                                "score": None,
                                "rsi": None,
                                "momentum": None,
                                "status": "Managing open bot position",
                                "eligible": False,
                            }
                        ],
                        "errors": [],
                        "candidate": None,
                        "selected_symbol": symbol,
                        "status": (
                            f"Managing open {symbol} position; new entries are paused "
                            "until it closes."
                        ),
                    }
                else:
                    last_scan = scanner.scan(engine.interval)
                    scan_candidate = last_scan["candidate"]
                    if scan_candidate is None:
                        self.events.put(("scanner-update", last_scan))
                        if self.stop_event.wait(engine.seconds_until_next_candle()):
                            break
                        continue
                    symbol = scan_candidate["symbol"]
                    active_engine = engines.get(symbol)
                    if active_engine is None:
                        active_engine = self._engine_for_symbol(engine, symbol)
                        engines[symbol] = active_engine
                    scan_view = {
                        **last_scan,
                        "selected_symbol": symbol,
                        "status": (
                            f"{last_scan['status']} · AI reviewing {symbol} "
                            f"(technical score {scan_candidate['score']:.2f}, not probability)."
                        ),
                    }
                self.events.put(("scanner-update", scan_view))
                if self.stop_event.is_set():
                    break
                result = active_engine.run_cycle(
                    execute_orders=True,
                    progress_callback=self._report_analysis_progress,
                )
                if scan_candidate:
                    result["scan_candidate"] = {
                        "symbol": scan_candidate["symbol"],
                        "score": scan_candidate["score"],
                    }
                self.events.put(("result", result))
                if result.get("halt_automation"):
                    self.stop_event.set()
                    break
                if self.stop_event.wait(active_engine.seconds_until_next_candle()):
                    break
        except Exception as error:
            self.stop_event.set()
            self.events.put(
                ("error", f"Automation halted for safety; no retry will be attempted: {error}")
            )
        finally:
            self.events.put(("automation", False))

    def stop_automation(self):
        self.stop_event.set()
        self.status_var.set(
            "Pause requested. Waiting for the in-flight operation; existing Binance exit orders remain active."
        )
        self.stop_button.configure(state=tk.DISABLED)

    def protect_managed_position(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Wait for the current request to finish.", parent=self.root)
            return
        open_symbols = [
            symbol
            for symbol, position in self.ledger.positions().items()
            if position["quantity"] > 0
        ]
        if len(open_symbols) != 1:
            messagebox.showinfo(
                "Position selection required",
                "Protect is available when exactly one bot-managed position is open.",
                parent=self.root,
            )
            return
        try:
            engine = self._make_engine(symbol=open_symbols[0])
        except (ValueError, LedgerError) as error:
            messagebox.showwarning("Check configuration", str(error), parent=self.root)
            return
        position = self.ledger.position(engine.symbol)
        if position["quantity"] <= 0:
            messagebox.showinfo("No open position", "There is no managed position for the selected symbol.")
            return
        prompt = (
            f"Submit a Binance OCO sell list for {engine.symbol}?\n\n"
            f"Stop trigger: {engine.stop_loss_pct:g}% below average entry\n"
            f"Take-profit trigger: {engine.take_profit_pct:g}% above average entry\n\n"
            "The stop and target trigger market orders; actual execution prices are not guaranteed."
        )
        if self.mode_var.get() == "LIVE":
            prompt = "LIVE uses real funds.\n\n" + prompt
        if not messagebox.askyesno("Place exchange-managed exit orders", prompt, parent=self.root):
            return

        def worker():
            try:
                order_list = engine.protect_open_position()
                self.events.put(
                    (
                        "message",
                        f"Exchange exits confirmed for {engine.symbol}: stop "
                        f"{order_list['stopPrice']}, take-profit {order_list['takeProfitPrice']}.",
                    )
                )
            except Exception as error:
                self.events.put(("error", f"Could not protect the managed position: {error}"))

        if self._launch("protect-position", worker):
            self.status_var.set("Submitting protective exits to Binance…")

    def verify_managed_position(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Wait for the current request to finish.", parent=self.root)
            return
        open_symbols = [
            symbol
            for symbol, position in self.ledger.positions().items()
            if position["quantity"] > 0
        ]
        if len(open_symbols) != 1:
            messagebox.showinfo(
                "Position selection required",
                "Exit verification is available when exactly one bot-managed position is open.",
                parent=self.root,
            )
            return
        try:
            engine = self._make_engine(symbol=open_symbols[0])
        except (ValueError, LedgerError) as error:
            messagebox.showwarning("Check configuration", str(error), parent=self.root)
            return

        def worker():
            try:
                message = engine.verify_protection_status()
                self.events.put(("message", message))
            except Exception as error:
                self.events.put(("error", f"Could not verify Binance exit status: {error}"))

        if self._launch("verify-exits", worker):
            self.status_var.set("Checking exit-order status with Binance…")

    def close_managed_position(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Wait for the current request to finish.", parent=self.root)
            return
        open_symbols = [
            symbol
            for symbol, position in self.ledger.positions().items()
            if position["quantity"] > 0
        ]
        if len(open_symbols) != 1:
            messagebox.showinfo(
                "Position selection required",
                "Close is available when exactly one bot-managed position is open.",
                parent=self.root,
            )
            return
        try:
            engine = self._make_engine(symbol=open_symbols[0])
        except (ValueError, LedgerError) as error:
            messagebox.showwarning("Check configuration", str(error), parent=self.root)
            return
        position = self.ledger.position(engine.symbol)
        if position["quantity"] <= 0:
            messagebox.showinfo("No open position", "There is no managed position for the selected symbol.")
            return
        prompt = (
            f"Cancel and verify any bot-managed Binance exit list, then send a MARKET SELL "
            f"for the available tracked {engine.symbol} quantity?"
        )
        if self.mode_var.get() == "LIVE":
            prompt = "LIVE uses real funds.\n\n" + prompt
        if not messagebox.askyesno("Close bot-managed position", prompt, parent=self.root):
            return

        def worker():
            try:
                result = engine.close_open_position()
                order = result.get("order")
                summary = result["message"]
                if order:
                    summary += (
                        f" Order {order.get('orderId')} · estimated realized P/L "
                        f"{result['realized_pnl']:.4f} USDT (fees may be incomplete)."
                    )
                self.events.put(("message", summary))
            except Exception as error:
                self.events.put(("error", f"Could not close the managed position: {error}"))

        if self._launch("close-position", worker):
            self.status_var.set("Verifying exit cancellation before any market sell…")

    def reconcile_pending(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Wait for the current operation to finish.", parent=self.root)
            return
        if not self.ledger.pending_order:
            messagebox.showinfo("No pending order", "There is no pending order to reconcile.")
            return
        try:
            engine = self._make_engine()
        except (ValueError, LedgerError) as error:
            messagebox.showwarning("Check configuration", str(error), parent=self.root)
            return
        mode = self.mode_var.get()
        if mode == "LIVE":
            title = "Reconcile LIVE order"
            prompt = (
                "This queries your Binance LIVE account and may cancel the unfilled "
                "remainder of a real order. Continue?"
            )
        else:
            title = "Reconcile Testnet order"
            prompt = (
                "This queries Binance Spot Testnet and may cancel the unfilled "
                "remainder of an open simulated order. Continue?"
            )
        if not messagebox.askyesno(title, prompt, parent=self.root):
            return

        def worker():
            try:
                message = engine.reconcile_pending_order()
                self.events.put(("message", message))
            except Exception as error:
                self.events.put(("error", f"Order reconciliation failed: {error}"))

        if self._launch("reconcile-order", worker):
            self.status_var.set(
                f"Reconciling pending order with Binance {self.mode_var.get()}…"
            )

    def confirm_no_pending_order(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "Wait for the current request to finish.", parent=self.root)
            return
        pending = self.ledger.pending_order
        if not pending:
            messagebox.showinfo("No pending order", "There is no pending order to clear.")
            return
        confirmation = simpledialog.askstring(
            "Manual exchange-history verification required",
            f"Check Binance {self.mode_var.get()} order and trade history for "
            f"{pending['client_order_id']} on {pending['symbol']} first.\n\n"
            "Only if you have verified there is no order/fill, type NO ORDER EXISTS "
            "to clear the local safety lock.",
            parent=self.root,
        )
        if confirmation != "NO ORDER EXISTS":
            return
        try:
            self.ledger.clear_pending_order(pending["client_order_id"])
        except LedgerError as error:
            self._report_error("Could not clear order lock", error)
            return
        self.status_var.set(
            "Pending intent cleared after your manual exchange-history confirmation."
        )
        self._append_log(self.status_var.get())

    def _process_events(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "live-prices":
                self._render_live_prices(payload)
            elif kind == "live-price-error":
                mode, error, received_at = payload
                if mode == self.mode_var.get():
                    self.market_time_var.set(
                        "LIVE PRICE DELAY · "
                        + datetime.fromtimestamp(received_at).strftime("%H:%M:%S")
                    )
                    if (
                        error != self.last_live_price_error
                        or received_at - self.live_price_error_logged_at >= 60
                    ):
                        self._append_log(f"Live market price update failed: {error}")
                        self.last_live_price_error = error
                        self.live_price_error_logged_at = received_at
            elif kind == "connected":
                (
                    mode,
                    message,
                    profile,
                    balances,
                    balance_records,
                    equity_summary,
                    equity_error,
                    protection_messages,
                    active_trades,
                    save_error,
                    remember_credentials,
                ) = payload
                self.account_var.set(f"{message} · {profile}")
                self.balance_records = balance_records
                self._render_active_trade_records(active_trades)
                for row in self.balance_tree.get_children():
                    self.balance_tree.delete(row)
                for asset, free, locked in balance_records:
                    self.balance_tree.insert(
                        "",
                        tk.END,
                        values=(asset, f"{free:.10g}", f"{locked:.10g}", f"{free + locked:.10g}"),
                    )
                usdt_balance = next(
                    (
                        free + locked
                        for asset, free, locked in balance_records
                        if asset == "USDT"
                    ),
                    0,
                )
                self.balance_total_var.set(f"{usdt_balance:,.4f} USDT")
                self._render_equity_risk(equity_summary, equity_error)
                for symbol, protection_message in protection_messages:
                    self._append_log(f"{symbol} exchange exits: {protection_message}")
                self.status_var.set(
                    "LIVE account connected. Real-money automation remains off until explicitly armed."
                    if mode == "LIVE"
                    else "Testnet account connected. Only simulated funds are accessible."
                )
                self._append_log(
                    f"{message}\n{profile}\nAccount balances: {balances}"
                )
                if save_error:
                    self.status_var.set(
                        f"{self.status_var.get()} Saved-key setting failed: {save_error}"
                    )
                    self._append_log(f"Saved-key setting failed: {save_error}")
                elif remember_credentials:
                    self._append_log("Last connected API credentials saved using Windows DPAPI.")
                else:
                    self._append_log("Saved API credentials removed as requested.")
                self._stop_progress("Account connection complete")
            elif kind == "market-data":
                self.refresh_market_button.configure(state=tk.NORMAL)
                try:
                    self._render_market(payload)
                    self._stop_progress("Market data refreshed")
                except (KeyError, TypeError, ValueError, tk.TclError) as error:
                    self.chart_status_var.set("Market data could not be drawn")
                    self.connection_label_var.set("MARKET DATA INVALID")
                    self.status_var.set(f"Market chart error: {error}")
                    self._append_log(f"Market chart error: {error}")
                    self._stop_progress("Market chart error")
            elif kind == "analysis-progress":
                percent, text = payload
                self._show_prediction_progress(percent, text)
                self._start_progress(text)
            elif kind == "backtest-progress":
                self._start_progress(payload)
                self.status_var.set(payload)
            elif kind == "market-error":
                self.refresh_market_button.configure(state=tk.NORMAL)
                self.chart_status_var.set("Market refresh failed")
                self.connection_label_var.set("MARKET DATA OFFLINE")
                self._append_log(payload)
                self._stop_progress("Market data unavailable")
                if self.auto_refresh_var.get():
                    self.status_var.set(payload)
            elif kind == "trade-history":
                mode, symbol, quote_asset, records = payload
                self.load_history_button.configure(state=tk.NORMAL)
                self._render_trade_history(mode, symbol, quote_asset, records)
                self._append_log(
                    f"Loaded {len(records)} recent {symbol} fills from Binance {mode}; "
                    "read-only request."
                )
                self._stop_progress("Trade history loaded")
            elif kind == "trade-history-error":
                self.load_history_button.configure(state=tk.NORMAL)
                self.history_status_var.set(payload)
                self.status_var.set(payload)
                self._append_log(payload)
                self._stop_progress("Trade history load failed")
            elif kind == "backtest-result":
                text = self._format_backtest_report(payload)
                self.status_var.set(
                    f"Simulation complete for {payload['symbol']} {payload['interval']}; no orders were sent."
                )
                self._append_log(text)
                self._stop_progress("Backtest complete")
                messagebox.showinfo("Historical simulation report", text, parent=self.root)
            elif kind == "scanner-update":
                self._render_market_scan(payload)
            elif kind == "result":
                indicators = payload["indicators"]
                decision = payload["decision"]
                text = (
                    f"{payload['symbol']} · closed price {indicators['last_price']:.8g} · "
                    f"EMA20 {indicators['ema20']:.8g} · EMA50 {indicators['ema50']:.8g} · "
                    f"RSI14 {indicators['rsi14']:.2f}\n"
                    f"AI: {decision['action']} · model-reported confidence {decision['confidence']:.0%}\n"
                    f"Reason: {decision['reason']}\n"
                    f"Execution: {payload['message']}"
                )
                if payload.get("order"):
                    order = payload["order"]
                    text += f"\nOrder ID: {order.get('orderId')} · status: {order.get('status')}"
                if payload.get("scan_candidate"):
                    candidate = payload["scan_candidate"]
                    text += (
                        f"\nScanner selection: {candidate['symbol']} · technical score "
                        f"{candidate['score']:.2f} (not a probability)"
                    )
                if payload.get("exit_order_list"):
                    exit_list = payload["exit_order_list"]
                    text += (
                        f"\nExchange exits: stop {exit_list['stopPrice']} · "
                        f"take-profit {exit_list['takeProfitPrice']} · "
                        f"list {exit_list['orderListId']}"
                    )
                if payload.get("protection_status"):
                    text += f"\nExit protection status: {payload['protection_status']}"
                if "realized_pnl" in payload:
                    text += f"\nEstimated realized PnL: {payload['realized_pnl']:.4f} quote units"
                if "equity_risk" in payload:
                    risk = payload["equity_risk"]
                    self._render_equity_risk(risk)
                    text += (
                        f"\nLIVE account equity: {risk['equity_usdt']:.4f} USDT · "
                        f"daily high-water drawdown: {risk['drawdown_usdt']:.4f} USDT"
                    )
                self.status_var.set(payload["message"])
                self.ai_health_var.set("LOCAL AI · RESPONSE OK")
                self._append_log(text)
                self.position_var.set(self._position_summary())
                self._show_analysis_result(payload)
                self._sync_active_trades_from_ledger()
                try:
                    alert_messages = result_alert_messages(
                        payload, enabled=self.alerts_enabled_var.get()
                    )
                except ValueError as error:
                    alert_messages = []
                    self._append_log(f"Desktop alert could not parse order details: {error}")
                if alert_messages:
                    self._show_desktop_notification(
                        f"Astra AI Trader · Binance {self.mode_var.get()}",
                        "\n".join(alert_messages),
                        risk=any(
                            message.startswith("RISK STOP")
                            for message in alert_messages
                        ),
                    )
                self._stop_progress(
                    "Automation active · waiting for next cycle"
                    if self.auto_trading_active
                    else "Analysis complete"
                )
            elif kind == "automation":
                self.auto_trading_active = bool(payload)
                if payload:
                    self._start_progress("Automatic trading cycle running…")
                else:
                    self.start_button.configure(state=tk.NORMAL)
                    self.stop_button.configure(state=tk.DISABLED)
                    self._stop_progress("Automation stopped")
                    if not self.stop_event.is_set():
                        self.status_var.set("Automation stopped.")
            elif kind == "message":
                self.status_var.set(payload)
                self._append_log(payload)
                self.position_var.set(self._position_summary())
                self._sync_active_trades_from_ledger()
                alert = message_alert(
                    payload, enabled=self.alerts_enabled_var.get()
                )
                if alert:
                    self._show_desktop_notification(
                        f"Astra AI Trader · Binance {self.mode_var.get()}",
                        alert,
                        risk=alert.startswith("RISK STOP"),
                    )
                self._stop_progress("Operation complete")
            elif kind == "error":
                self.status_var.set(payload)
                if "Ollama" in str(payload) or "local AI" in str(payload):
                    self.ai_health_var.set("LOCAL AI · ERROR")
                self._append_log(payload)
                alert = message_alert(
                    payload, enabled=self.alerts_enabled_var.get()
                )
                if alert:
                    self._show_desktop_notification(
                        f"Astra AI Trader · Binance {self.mode_var.get()}",
                        alert,
                        risk=True,
                    )
                self._show_prediction_progress(0, "Process stopped; review the activity log.")
                if self.start_button.instate(["disabled"]):
                    self.start_button.configure(state=tk.NORMAL)
                    self.stop_button.configure(state=tk.DISABLED)
                self.position_var.set(self._position_summary())
                self._sync_active_trades_from_ledger()
                if not self.auto_trading_active:
                    self._stop_progress("Stopped after an error")
                messagebox.showerror("Operation failed", payload, parent=self.root)
        self.root.after(150, self._process_events)

    def close(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            messagebox.showinfo(
                "Operation in progress",
                "Stop was requested. Wait for the in-flight request to finish, then close the window.",
                parent=self.root,
            )
            return
        self._dismiss_desktop_notification()
        if self.market_refresh_after is not None:
            self.root.after_cancel(self.market_refresh_after)
        if self.live_price_after is not None:
            self.root.after_cancel(self.live_price_after)
        if self.instance_lock:
            self.instance_lock.release()
        self.root.destroy()


def main():
    root = tk.Tk()
    lock_path = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "AstraAITrader",
        "astra-ai-trader.lock",
    )
    instance_lock = InstanceLock(lock_path)
    try:
        instance_lock.acquire()
        TraderApp(root, instance_lock)
    except InstanceLockError as error:
        messagebox.showerror("Astra AI Trader is already running", str(error), parent=root)
        root.destroy()
        return
    except LedgerError as error:
        messagebox.showerror("Trade ledger error", str(error), parent=root)
        instance_lock.release()
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
