FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY app.py account_display.py backtest.py binance_permissions.py binance_testnet.py credential_store.py desktop_alerts.py ledger_storage.py local_ai.py market_chart.py market_scanner.py trade_history.py trade_ledger.py trading_engine.py \
     instance_lock.py test_trading.py ./

RUN python -m py_compile app.py account_display.py backtest.py binance_permissions.py binance_testnet.py credential_store.py desktop_alerts.py ledger_storage.py local_ai.py market_chart.py market_scanner.py trade_history.py trade_ledger.py \
    trading_engine.py instance_lock.py test_trading.py

CMD ["python", "-m", "unittest", "discover", "-v", "-s", ".", "-p", "test_*.py"]
