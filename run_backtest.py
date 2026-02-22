"""Run backtests comparing strategies.

Usage:
    python run_backtest.py              # run all strategies
    python run_backtest.py v1           # run only v1
    python run_backtest.py adaptive     # run only adaptive
    python run_backtest.py straddle     # run only 9:20 straddle
    python run_backtest.py straddle 15  # run straddle on 15-min candles
"""

import os
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

from backtesting.data_fetcher import DataFetcher
from backtesting.backtest_runner import run_backtest
from backtesting.straddle_backtest import run_straddle_backtest
from modules.strategy.ema_crossover import EMACrossoverStrategy
from modules.strategy.ema_crossover_v2 import EMACrossoverV2Strategy
from modules.strategy.adaptive_strategy import AdaptiveStrategy


def run(strategy_version="all", interval="15minute", days_back=60):
    fetcher = DataFetcher()
    interval_short = interval.replace("minute", "min")

    # Try loading NIFTY 50 index data (longer history)
    df = fetcher.load_from_parquet("NSE:NIFTY50", interval_short)
    symbol = "NSE:NIFTY50"

    if df.empty:
        df = fetcher.load_from_parquet("NFO:NIFTY26MARFUT", interval_short)
        symbol = "NFO:NIFTY26MARFUT"

    if df.empty:
        print("No cached data found. Downloading from Kite...")
        symbol = "NFO:NIFTY26MARFUT"
        df = fetcher.fetch_and_cache(symbol, days_back=days_back, interval=interval)

    if df.empty:
        print("ERROR: Could not fetch data. Check your Kite credentials.")
        sys.exit(1)

    capital = 100000
    print(f"\nData: {len(df)} candles | {df.index[0]} to {df.index[-1]}")
    print(f"Symbol: {symbol} | Interval: {interval} | Capital: Rs. {capital}\n")

    if strategy_version in ("v1", "all"):
        print("\n" + "#" * 50)
        print("  STRATEGY V1: EMA 9/21 (original)")
        print("#" * 50)
        v1 = EMACrossoverStrategy()
        run_backtest(data=df.copy(), strategy=v1, capital=capital, symbol=symbol)

    if strategy_version in ("v2", "all"):
        print("\n" + "#" * 50)
        print("  STRATEGY V2: EMA 9/21 + ADX + VWAP")
        print("#" * 50)
        v2 = EMACrossoverV2Strategy()
        run_backtest(data=df.copy(), strategy=v2, capital=capital, symbol=symbol)

    if strategy_version in ("adaptive", "all"):
        print("\n" + "#" * 50)
        print("  ADAPTIVE SMART STRATEGY")
        print("  (VIX regime + Gap fill + First hour + Scoring)")
        print("#" * 50)
        adaptive = AdaptiveStrategy()
        run_backtest(data=df.copy(), strategy=adaptive, capital=capital, symbol=symbol)

    if strategy_version in ("straddle", "all"):
        print("\n" + "#" * 50)
        print("  9:20 SHORT STRADDLE (Options)")
        print("  (Sell ATM CE+PE at 9:20, SL 25%, exit 3:10)")
        print("#" * 50)
        run_straddle_backtest(data=df.copy(), stop_loss_pct=25.0, capital=capital, symbol=symbol)


if __name__ == "__main__":
    version = sys.argv[1] if len(sys.argv) > 1 else "all"
    interval = sys.argv[2] + "minute" if len(sys.argv) > 2 else "15minute"
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 60

    run(strategy_version=version, interval=interval, days_back=days)
