"""Backtest engine for 9:20 Short Straddle strategy.

Since we don't have historical options data, we simulate premiums
using spot price movement + Black-Scholes approximation.
"""

from datetime import time
from typing import Any, Dict, List

import pandas as pd
from core.config import settings
from core.logger import get_logger
from modules.strategy.straddle_920 import Straddle920Strategy
from backtesting.performance import calculate_metrics, print_report

log = get_logger("straddle_backtest")

# Costs for options: flat brokerage + STT on sell side only
OPTIONS_BROKERAGE_PER_ORDER = 20.0  # Rs per order
OPTIONS_STT_SELL_PCT = 0.0625  # STT on sell side of options (0.0625%)
OPTIONS_EXCHANGE_PCT = 0.05  # Exchange + SEBI charges %


def calculate_options_costs(premium: float, quantity: int, legs: int = 2) -> float:
    """Calculate transaction costs for options trades.

    Args:
        premium: Average premium per lot
        quantity: Lot size
        legs: Number of legs (2 for straddle)
    """
    turnover = premium * quantity * legs
    brokerage = OPTIONS_BROKERAGE_PER_ORDER * legs * 2  # entry + exit per leg
    stt = turnover * (OPTIONS_STT_SELL_PCT / 100)  # sell side only
    exchange = turnover * (OPTIONS_EXCHANGE_PCT / 100)
    return brokerage + stt + exchange


class StraddleBacktester:
    """Backtests the 9:20 Short Straddle on historical spot data."""

    def __init__(self, stop_loss_pct: float = 25.0, initial_capital: float = None):
        self.stop_loss_pct = stop_loss_pct
        self.initial_capital = initial_capital or float(settings.CAPITAL)
        self.capital = self.initial_capital
        self.trades: List[Dict[str, Any]] = []

    def run(self, df: pd.DataFrame, symbol: str = "BACKTEST") -> Dict[str, Any]:
        """Run straddle backtest on OHLCV spot data.

        Uses 5-minute or 15-minute candles to simulate intraday price movement.
        """
        if df.empty:
            return {"error": "No data"}

        strategy = Straddle920Strategy(stop_loss_pct=self.stop_loss_pct)

        log.info(
            f"Running straddle backtest on {len(df)} candles | "
            f"Capital: {self.initial_capital} | SL: {self.stop_loss_pct}%"
        )

        current_day = ""
        day_entry = None
        stop_losses = 0.0  # accumulated stop losses for current day

        for idx, row in df.iterrows():
            candle = {
                "symbol": symbol,
                "timestamp": idx,
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row.get("volume", 0),
            }

            day = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]

            # Track day transitions
            if day != current_day:
                # Close previous day's trade if still open
                if day_entry and strategy.has_position:
                    result = strategy._exit_all(candle, candle["close"], "DAY_END")
                    if result:
                        self._record_trade(day_entry, result, current_day, stop_losses)
                elif day_entry and stop_losses != 0:
                    # Both legs stopped out, no EOD exit — record stop-only trade
                    stop_result = {"action": "EXIT", "reason": "BOTH_STOP",
                                   "pnl": 0, "spot": candle["close"],
                                   "ce_exit": 0, "pe_exit": 0}
                    self._record_trade(day_entry, stop_result, current_day, stop_losses)
                current_day = day
                stop_losses = 0.0
                day_entry = None

            result = strategy.on_candle(candle)

            if result:
                action = result.get("action")

                if action == "ENTRY":
                    day_entry = result

                elif action in ("CE_STOP", "PE_STOP", "BOTH_STOP"):
                    # Accumulate stop loss P&L
                    stop_losses += result.get("ce_loss", 0)
                    stop_losses += result.get("pe_loss", 0)

                elif action == "EXIT":
                    if day_entry:
                        self._record_trade(day_entry, result, current_day, stop_losses)
                        day_entry = None
                        stop_losses = 0.0

        # Final cleanup
        if strategy.has_position and day_entry:
            last_candle = {
                "symbol": symbol,
                "timestamp": df.index[-1],
                "close": df.iloc[-1]["close"],
                "open": df.iloc[-1]["open"],
                "high": df.iloc[-1]["high"],
                "low": df.iloc[-1]["low"],
            }
            result = strategy._exit_all(last_candle, last_candle["close"], "BACKTEST_END")
            if result:
                self._record_trade(day_entry, result, current_day, stop_losses)
        elif day_entry and stop_losses != 0:
            stop_result = {"action": "EXIT", "reason": "BOTH_STOP",
                           "pnl": 0, "spot": df.iloc[-1]["close"],
                           "ce_exit": 0, "pe_exit": 0}
            self._record_trade(day_entry, stop_result, current_day, stop_losses)

        metrics = calculate_metrics(self.trades, self.initial_capital)
        return metrics

    def _record_trade(self, entry: Dict, exit_result: Dict, day: str, stop_losses: float = 0.0):
        """Record a completed straddle trade."""
        # EOD P&L from remaining active legs + accumulated stop losses
        pnl = exit_result.get("pnl", 0) - stop_losses

        # Calculate costs
        avg_premium = (entry.get("ce_premium", 0) + entry.get("pe_premium", 0)) / 2
        costs = calculate_options_costs(avg_premium, NIFTY_LOT_SIZE := 25)
        net_pnl = pnl - costs

        self.capital += net_pnl

        trade = {
            "instrument": "STRADDLE",
            "direction": "SELL",
            "entry_price": entry.get("strike", 0),
            "exit_price": exit_result.get("spot", 0),
            "quantity": 25,
            "pnl": net_pnl,
            "costs": costs,
            "entry_time": day,
            "exit_time": day,
            "exit_reason": exit_result.get("reason", "UNKNOWN"),
            "strategy": "straddle_920",
            "ce_premium": f"{entry.get('ce_premium', 0):.1f}->{exit_result.get('ce_exit', 0):.1f}",
            "pe_premium": f"{entry.get('pe_premium', 0):.1f}->{exit_result.get('pe_exit', 0):.1f}",
            "total_credit": entry.get("total_credit", 0),
        }
        self.trades.append(trade)


def run_straddle_backtest(
    data: pd.DataFrame,
    stop_loss_pct: float = 25.0,
    capital: float = None,
    symbol: str = "BACKTEST",
) -> Dict[str, Any]:
    """Convenience function to run straddle backtest and print results."""
    runner = StraddleBacktester(stop_loss_pct=stop_loss_pct, initial_capital=capital)
    metrics = runner.run(data, symbol)
    print_report(metrics)

    # Print straddle-specific stats
    if runner.trades:
        eod_exits = sum(1 for t in runner.trades if t["exit_reason"] == "EOD_EXIT")
        stop_exits = sum(1 for t in runner.trades if "STOP" in t.get("exit_reason", ""))
        print(f"  Straddle Stats:")
        print(f"    EOD exits (theta wins): {eod_exits}")
        print(f"    Stop exits (losses):    {stop_exits}")
        avg_credit = sum(t["total_credit"] for t in runner.trades) / len(runner.trades)
        print(f"    Avg credit received:    Rs. {avg_credit:.0f}")
        print()

    return metrics
