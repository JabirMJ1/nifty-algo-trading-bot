"""Backtesting engine — runs strategies on historical data."""

from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import pandas as pd
from core.config import settings
from core.logger import get_logger
from modules.strategy.base_strategy import BaseStrategy
from modules.strategy.ema_crossover import EMACrossoverStrategy
from modules.risk.position_sizer import calculate_atr
from backtesting.performance import calculate_metrics, print_report

log = get_logger("backtest_runner")

_SENTIMENT_PATH = Path("data/sentiment_history.parquet")


def _load_sentiment_history() -> Optional[Dict]:
    """Load pre-fetched AlphaVantage sentiment data keyed by date.

    Returns a dict mapping datetime.date → {"score": float, "magnitude": float}
    or None if the parquet file doesn't exist.
    """
    if not _SENTIMENT_PATH.exists():
        return None

    df = pd.read_parquet(_SENTIMENT_PATH)
    df["date"] = pd.to_datetime(df["date"]).dt.date

    lookup = {}
    for _, row in df.iterrows():
        lookup[row["date"]] = {
            "symbol": "MARKET",
            "score": float(row["score"]),
            "magnitude": float(row["magnitude"]),
            "summary": f"Historical: {int(row['article_count'])} articles",
            "headline_count": int(row["article_count"]),
        }

    log.info(
        f"Loaded sentiment history: {len(lookup)} days "
        f"({min(lookup)} → {max(lookup)})"
    )
    return lookup


class BacktestRunner:
    """Runs a strategy on historical OHLCV data with realistic costs."""

    def __init__(
        self,
        strategy: Optional[BaseStrategy] = None,
        initial_capital: float = None,
    ):
        self.strategy = strategy or EMACrossoverStrategy()
        self.initial_capital = initial_capital or float(settings.CAPITAL)
        self.capital = self.initial_capital
        self.position = None
        self.trades: List[Dict[str, Any]] = []
        self.candle_history: List[Dict[str, Any]] = []
        self._sentiment_lookup: Optional[Dict] = _load_sentiment_history()

    def run(self, df: pd.DataFrame, symbol: str = "BACKTEST") -> Dict[str, Any]:
        """Run backtest on a DataFrame of OHLCV data.

        Args:
            df: DataFrame with columns: open, high, low, close, volume
                Index should be datetime
            symbol: Symbol name for the strategy

        Returns:
            Performance metrics dict
        """
        if df.empty:
            log.warning("Empty DataFrame. Nothing to backtest.")
            return {"error": "No data"}

        log.info(
            f"Running backtest on {len(df)} candles | "
            f"Capital: {self.initial_capital} | Strategy: {self.strategy.name}"
        )

        self.capital = self.initial_capital
        self.position = None
        self.trades = []
        self.candle_history = []

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

            self.candle_history.append(candle)

            # Skip first 30 min of each day (9:15 - 9:45)
            if hasattr(idx, "time"):
                candle_time = idx.time()
                if candle_time < time(9, 45):
                    continue

            # Check stops for open position
            if self.position:
                exit_reason = self._check_stops(candle)
                if exit_reason:
                    self._close_position(candle["close"], candle["timestamp"], exit_reason)

            # Inject daily sentiment into strategy cache (if history loaded)
            if self._sentiment_lookup and hasattr(idx, "date"):
                candle_date = idx.date()
                sentiment_today = self._sentiment_lookup.get(candle_date)
                if hasattr(self.strategy, "_sentiment_cache"):
                    if sentiment_today:
                        self.strategy._sentiment_cache["MARKET"] = sentiment_today
                    else:
                        self.strategy._sentiment_cache.pop("MARKET", None)

            # Feed candle to strategy
            # Build history for strategy
            self.strategy._candle_history.setdefault(symbol, []).append(candle)

            signal = self.strategy.on_candle(candle)

            if signal and not self.position:
                # Open new position
                self._open_position(signal, candle)
            elif signal and self.position:
                # Signal in opposite direction — close and reverse
                if signal.direction.value != self.position["direction"]:
                    self._close_position(candle["close"], candle["timestamp"], "SIGNAL_REVERSAL")
                    self._open_position(signal, candle)

            # Close positions at EOD (15:25 to allow for slippage)
            if hasattr(idx, "time") and idx.time() >= time(15, 25):
                if self.position:
                    self._close_position(candle["close"], candle["timestamp"], "EOD_CLOSE")

        # Close any remaining position
        if self.position and self.candle_history:
            last = self.candle_history[-1]
            self._close_position(last["close"], last["timestamp"], "BACKTEST_END")

        metrics = calculate_metrics(self.trades, self.initial_capital)
        return metrics

    def _open_position(self, signal, candle: Dict[str, Any]):
        """Open a position based on signal."""
        atr = calculate_atr(self.candle_history)
        if atr <= 0:
            return

        # Simple sizing: risk 1% per trade
        risk_amount = self.capital * (settings.RISK_PER_TRADE_PCT / 100)
        quantity = max(1, int(risk_amount / atr))

        entry_price = candle["close"]

        # Set stops — use signal metadata overrides if available
        meta = signal.metadata if hasattr(signal, "metadata") else {}
        stop_mult = meta.get("stop_multiplier", settings.STOP_ATR_MULTIPLIER)
        target_mult = meta.get("target_multiplier", settings.TARGET_ATR_MULTIPLIER)
        stop_distance = atr * stop_mult
        target_distance = atr * target_mult

        if signal.direction.value == "BUY":
            stop_loss = entry_price - stop_distance
            target = entry_price + target_distance
        else:
            stop_loss = entry_price + stop_distance
            target = entry_price - target_distance

        self.position = {
            "direction": signal.direction.value,
            "entry_price": entry_price,
            "quantity": quantity,
            "stop_loss": stop_loss,
            "target": target,
            "entry_time": candle["timestamp"],
            "atr": atr,
            "highest_price": entry_price,
            "lowest_price": entry_price,
            "trailing_active": False,
        }

    def _check_stops(self, candle: Dict[str, Any]) -> Optional[str]:
        """Check if stop-loss or target hit within candle."""
        pos = self.position
        if not pos:
            return None

        high = candle["high"]
        low = candle["low"]

        if pos["direction"] == "BUY":
            # Update tracking
            pos["highest_price"] = max(pos["highest_price"], high)

            # Target hit
            if high >= pos["target"]:
                return "TARGET_HIT"
            # Stop hit
            if low <= pos["stop_loss"]:
                return "STOP_HIT"

            # Trailing stop activation
            trail_activate = pos["entry_price"] + pos["atr"] * settings.TRAIL_ATR_MULTIPLIER
            if pos["highest_price"] >= trail_activate:
                pos["trailing_active"] = True
                new_stop = pos["highest_price"] - pos["atr"] * settings.TRAIL_ATR_MULTIPLIER
                if new_stop > pos["stop_loss"]:
                    pos["stop_loss"] = new_stop
        else:
            pos["lowest_price"] = min(pos["lowest_price"], low)

            if low <= pos["target"]:
                return "TARGET_HIT"
            if high >= pos["stop_loss"]:
                return "STOP_HIT"

            trail_activate = pos["entry_price"] - pos["atr"] * settings.TRAIL_ATR_MULTIPLIER
            if pos["lowest_price"] <= trail_activate:
                pos["trailing_active"] = True
                new_stop = pos["lowest_price"] + pos["atr"] * settings.TRAIL_ATR_MULTIPLIER
                if new_stop < pos["stop_loss"]:
                    pos["stop_loss"] = new_stop

        return None

    def _close_position(self, exit_price: float, exit_time, reason: str):
        """Close position and record trade."""
        pos = self.position
        if not pos:
            return

        # Calculate PnL
        if pos["direction"] == "BUY":
            gross_pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
        else:
            gross_pnl = (pos["entry_price"] - exit_price) * pos["quantity"]

        # Costs: brokerage + STT + exchange
        turnover = (pos["entry_price"] + exit_price) * pos["quantity"]
        costs = (
            settings.BROKERAGE_PER_ORDER * 2
            + turnover * (settings.STT_PCT / 100)
            + turnover * (settings.EXCHANGE_CHARGES_PCT / 100)
        )
        net_pnl = gross_pnl - costs

        self.capital += net_pnl

        trade = {
            "instrument": "BACKTEST",
            "direction": pos["direction"],
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "quantity": pos["quantity"],
            "pnl": net_pnl,
            "costs": costs,
            "entry_time": pos["entry_time"],
            "exit_time": exit_time,
            "exit_reason": reason,
            "strategy": self.strategy.name,
        }
        self.trades.append(trade)
        self.position = None


def run_backtest(
    data: pd.DataFrame,
    strategy: Optional[BaseStrategy] = None,
    capital: float = None,
    symbol: str = "BACKTEST",
) -> Dict[str, Any]:
    """Convenience function to run a backtest and print results."""
    runner = BacktestRunner(strategy=strategy, initial_capital=capital)
    metrics = runner.run(data, symbol)
    print_report(metrics)
    return metrics
