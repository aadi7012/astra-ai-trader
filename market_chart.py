import math
import tkinter as tk


class CandlestickChart(tk.Canvas):
    BACKGROUND = "#111a2c"
    GRID = "#2a3b58"
    TEXT = "#9cacc4"
    UP = "#35d0a0"
    DOWN = "#ff7184"
    ACCENT = "#57b8ff"

    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            background=self.BACKGROUND,
            highlightthickness=0,
            **kwargs,
        )
        self.candles = []
        self.symbol = ""
        self.interval = ""
        self.bind("<Configure>", lambda _event: self.redraw())

    def set_data(self, symbol, interval, candles):
        valid = []
        for candle in candles:
            try:
                values = [
                    float(candle[key]) for key in ("open", "high", "low", "close", "volume")
                ]
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("Market chart received malformed candle data.") from error
            if any(not math.isfinite(value) for value in values):
                raise ValueError("Market chart received non-finite candle data.")
            open_price, high, low, close, volume = values
            if (
                min(open_price, high, low, close, volume) < 0
                or low <= 0
                or high < max(open_price, close)
                or low > min(open_price, close)
            ):
                raise ValueError("Market chart received invalid OHLCV candle values.")
            valid.append((open_price, high, low, close, volume))
        if not valid:
            raise ValueError("No market candles are available to draw.")
        self.symbol = symbol
        self.interval = interval
        self.candles = valid[-100:]
        self.redraw()

    def update_live_price(self, price):
        if not self.candles:
            return
        try:
            live_price = float(price)
        except (TypeError, ValueError) as error:
            raise ValueError("Live chart price is invalid.") from error
        if not math.isfinite(live_price) or live_price <= 0:
            raise ValueError("Live chart price must be finite and positive.")
        open_price, high, low, _close, volume = self.candles[-1]
        self.candles[-1] = (
            open_price,
            max(high, live_price),
            min(low, live_price),
            live_price,
            volume,
        )
        self.redraw()

    def redraw(self):
        self.delete("all")
        width = max(self.winfo_width(), 480)
        height = max(self.winfo_height(), 250)
        left, right, top, bottom = 72, width - 18, 24, height - 48
        if not self.candles or right <= left or bottom <= top:
            return

        chart_height = (bottom - top) * 0.78
        volume_top = top + chart_height + 20
        prices = [value for candle in self.candles for value in candle[:4]]
        low, high = min(prices), max(prices)
        spread = max(high - low, abs(high) * 0.001, 1e-9)
        low -= spread * 0.08
        high += spread * 0.08
        plot_height = chart_height

        def y_price(value):
            return top + (high - value) / (high - low) * plot_height

        for tick in range(5):
            ratio = tick / 4
            y = top + ratio * plot_height
            price = high - ratio * (high - low)
            self.create_line(left, y, right, y, fill=self.GRID)
            self.create_text(
                left - 8,
                y,
                text=f"{price:,.6g}",
                anchor=tk.E,
                fill=self.TEXT,
                font=("Segoe UI", 8),
            )

        chart_width = right - left
        step = chart_width / len(self.candles)
        body_width = max(2, min(10, step * 0.62))
        max_volume = max((candle[4] for candle in self.candles), default=0) or 1
        for index, (open_price, candle_high, candle_low, close, volume) in enumerate(
            self.candles
        ):
            center = left + (index + 0.5) * step
            color = self.UP if close >= open_price else self.DOWN
            self.create_line(
                center,
                y_price(candle_high),
                center,
                y_price(candle_low),
                fill=color,
                width=1,
            )
            body_top = y_price(max(open_price, close))
            body_bottom = y_price(min(open_price, close))
            if body_bottom - body_top < 1:
                body_bottom = body_top + 1
            self.create_rectangle(
                center - body_width / 2,
                body_top,
                center + body_width / 2,
                body_bottom,
                fill=color,
                outline=color,
            )
            volume_height = (bottom - volume_top) * volume / max_volume
            self.create_rectangle(
                center - body_width / 2,
                bottom - volume_height,
                center + body_width / 2,
                bottom,
                fill=color,
                outline="",
                stipple="gray50",
            )

        last = self.candles[-1][3]
        last_y = y_price(last)
        self.create_line(left, last_y, right, last_y, fill=self.ACCENT, dash=(4, 3))
        self.create_text(
            right - 2,
            last_y - 8,
            text=f"{last:,.8g}",
            anchor=tk.SE,
            fill=self.ACCENT,
            font=("Segoe UI", 9, "bold"),
        )
        self.create_text(
            left,
            height - 18,
            text=f"{self.symbol} · {self.interval} · {len(self.candles)} candles · volume",
            anchor=tk.W,
            fill=self.TEXT,
            font=("Segoe UI", 8),
        )
