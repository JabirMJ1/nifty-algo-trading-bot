"""Options directional buying backtest — CE on BUY signals, PE on SELL signals.

Premium is estimated using a fixed delta model from the underlying spot price.
No theta decay is modelled, so results will be slightly optimistic on slow days.
"""

from datetime import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from backtesting.performance import calculate_metrics
from core.config import settings
from core.logger import get_logger
from modules.strategy.base_strategy import BaseStrategy

log = get_logger("options_backtest")

_SENTIMENT_PATH = Path("data/sentiment_history.parquet")


def _load_sentiment_history() -> Optional[Dict]:
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
    return lookup


class OptionsBacktestRunner:
    """
    Backtests directional options buying (CE for BUY signals, PE for SELL signals).

    Premium model:
      entry_premium = spot × premium_pct   (approximates ATM weekly option)
      exit_premium  = entry_premium + delta × signed_spot_move

    Exit rules:
      - Take profit when premium gains profit_target_pct %
      - Stop loss when premium loses stop_loss_pct %
      - EOD forced close at 15:25

    Args:
        lots:             number of option lots per trade
        delta:            ATM option delta (~0.45 for Nifty weekly)
        premium_pct:      entry premium as fraction of spot (0.005 = 0.5%)
        profit_target_pct: exit when premium up this % (e.g. 10.0)
        stop_loss_pct:    exit when premium down this % (e.g. 50.0)
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        initial_capital: float = 30_000.0,
        lots: int = 3,
        delta: float = 0.45,              # ATM default; overridden per-signal below
        premium_pct: float = 0.005,
        profit_target_pct: float = 10.0,  # unused now; replaced by trailing stop
        stop_loss_pct: float = 10.0,
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.lots = lots
        self.lot_size = settings.OPTIONS_LOT_SIZE
        self.delta = delta
        self.premium_pct = premium_pct
        self.profit_target_pct = profit_target_pct
        self.stop_loss_pct = stop_loss_pct

        self.capital = initial_capital
        self.position: Optional[Dict] = None
        self.trades: List[Dict] = []
        self.candle_history: List[Dict] = []
        self._sentiment_lookup = _load_sentiment_history()

        # Risk controls
        self.max_hold_candles = 8          # time-based exit: ~2 hours on 15-min chart
        self.daily_loss_limit_pct = 15.0   # stop trading if down >15% on the day
        self.max_consecutive_losses = 2    # pause rest of day after N losses in a row

        # Trailing stop config
        self.trail_activate_pct = 8.0      # activate trailing when premium up this %
        self.trail_distance_pct = 5.0      # trail this far below high once active

        # Theta decay: % of entry premium lost per 15-min candle held
        # ~0.5%/candle ≈ 2%/hour ≈ 12%/day — typical for ATM weekly Nifty
        self.theta_per_candle = 0.005

    def run(self, df: pd.DataFrame, symbol: str = "BACKTEST") -> Dict[str, Any]:
        if df.empty:
            return {"error": "No data"}

        self.capital = self.initial_capital
        self.position = None
        self.trades = []
        self.candle_history = []

        # Per-day state
        day_start_capital = self.capital
        consecutive_losses = 0
        trading_paused_today = False
        current_day = None

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

            if hasattr(idx, "time") and idx.time() < time(9, 30):
                continue

            # Reset per-day controls at the start of each new trading day
            candle_day = idx.date() if hasattr(idx, "date") else None
            if candle_day and candle_day != current_day:
                current_day = candle_day
                day_start_capital = self.capital
                consecutive_losses = 0
                trading_paused_today = False

            # Inject daily sentiment into strategy cache
            if self._sentiment_lookup and hasattr(idx, "date"):
                sentiment_today = self._sentiment_lookup.get(candle_day)
                if hasattr(self.strategy, "_sentiment_cache"):
                    if sentiment_today:
                        self.strategy._sentiment_cache["MARKET"] = sentiment_today
                    else:
                        self.strategy._sentiment_cache.pop("MARKET", None)

            # Check exits on open position first
            if self.position:
                self.position["candles_held"] += 1
                exit_reason = self._check_exits(candle)
                if exit_reason:
                    self._close_position(candle, exit_reason)
                    trade = self.trades[-1]
                    if trade["pnl"] < 0:
                        consecutive_losses += 1
                        if consecutive_losses >= self.max_consecutive_losses:
                            trading_paused_today = True
                    else:
                        consecutive_losses = 0

            # Daily loss limit: stop trading if down >15% on the day
            daily_loss_pct = (self.capital - day_start_capital) / day_start_capital * 100
            if daily_loss_pct <= -self.daily_loss_limit_pct:
                trading_paused_today = True

            # Feed candle to strategy
            self.strategy._candle_history.setdefault(symbol, []).append(candle)
            signal = self.strategy.on_candle(candle)

            if not trading_paused_today:
                if signal and not self.position:
                    self._open_position(signal, candle)
                elif signal and self.position:
                    # Opposite direction signal → close and flip
                    if signal.direction.value != self.position["signal_direction"]:
                        self._close_position(candle, "SIGNAL_REVERSAL")
                        self._open_position(signal, candle)

            # Force-close at EOD
            if hasattr(idx, "time") and idx.time() >= time(15, 25):
                if self.position:
                    self._close_position(candle, "EOD_CLOSE")

        if self.position and self.candle_history:
            self._close_position(self.candle_history[-1], "BACKTEST_END")

        return calculate_metrics(self.trades, self.initial_capital)

    def _open_position(self, signal, candle: Dict):
        spot = candle["close"]

        # ── Strike selection ──
        # ATM default — best balance of leverage and theta for ₹30k capital
        strength = getattr(signal, "strength", None)
        strength_name = strength.value if strength is not None else "MODERATE"
        delta = 0.45
        premium_pct = 0.005

        entry_premium = spot * premium_pct

        # Scale down lots if capital is insufficient
        cost_per_lot = entry_premium * self.lot_size
        if cost_per_lot <= 0:
            return
        affordable_lots = max(1, int(self.capital / cost_per_lot))
        actual_lots = min(self.lots, affordable_lots)
        quantity = actual_lots * self.lot_size

        self.position = {
            "signal_direction": signal.direction.value,
            "signal_strength": strength_name,
            "entry_spot": spot,
            "entry_premium": entry_premium,
            "delta": delta,
            "lots": actual_lots,
            "quantity": quantity,
            "cost": entry_premium * quantity,
            "entry_time": candle["timestamp"],
            "stop_premium": entry_premium * (1 - self.stop_loss_pct / 100),
            "highest_premium": entry_premium,
            "trailing_active": False,
            "trailing_stop": None,
            "candles_held": 0,
        }

    def _premium_at_spot(self, spot: float) -> float:
        """Estimate option premium at a given spot price using delta + theta decay."""
        pos = self.position
        move = spot - pos["entry_spot"]
        if pos["signal_direction"] == "SELL":  # PE gains when spot falls
            move = -move
        theta_drag = pos["entry_premium"] * self.theta_per_candle * pos["candles_held"]
        return max(1.0, pos["entry_premium"] + pos["delta"] * move - theta_drag)

    def _check_exits(self, candle: Dict) -> Optional[str]:
        pos = self.position
        # CE benefits from spot rising; PE benefits from spot falling
        if pos["signal_direction"] == "BUY":
            best_prem = self._premium_at_spot(candle["high"])
            worst_prem = self._premium_at_spot(candle["low"])
        else:
            best_prem = self._premium_at_spot(candle["low"])
            worst_prem = self._premium_at_spot(candle["high"])

        # ── Trailing stop: activate at +8%, trail at -5% from high ──
        if best_prem > pos["highest_premium"]:
            pos["highest_premium"] = best_prem

        if not pos["trailing_active"]:
            trigger = pos["entry_premium"] * (1 + self.trail_activate_pct / 100)
            if pos["highest_premium"] >= trigger:
                pos["trailing_active"] = True

        if pos["trailing_active"]:
            new_trail = pos["highest_premium"] * (1 - self.trail_distance_pct / 100)
            if pos["trailing_stop"] is None or new_trail > pos["trailing_stop"]:
                pos["trailing_stop"] = new_trail
            if worst_prem <= pos["trailing_stop"]:
                return "TRAIL_HIT"

        if worst_prem <= pos["stop_premium"]:
            return "STOP_HIT"
        if pos["candles_held"] >= self.max_hold_candles:
            return "TIME_EXIT"
        return None

    def _close_position(self, candle: Dict, reason: str):
        pos = self.position
        if not pos:
            return

        if reason == "STOP_HIT":
            exit_premium = pos["stop_premium"]
        elif reason == "TRAIL_HIT":
            exit_premium = pos["trailing_stop"]
        else:
            exit_premium = self._premium_at_spot(candle["close"])

        gross_pnl = (exit_premium - pos["entry_premium"]) * pos["quantity"]

        close_turnover = exit_premium * pos["quantity"]
        costs = (
            settings.BROKERAGE_PER_ORDER * 2
            + close_turnover * (settings.STT_PCT / 100)
            + close_turnover * (settings.EXCHANGE_CHARGES_PCT / 100)
        )
        net_pnl = gross_pnl - costs
        self.capital += net_pnl

        pnl_on_premium = (exit_premium - pos["entry_premium"]) / pos["entry_premium"] * 100

        self.trades.append({
            "signal_direction": pos["signal_direction"],
            "entry_premium": round(pos["entry_premium"], 2),
            "exit_premium": round(exit_premium, 2),
            "entry_spot": pos["entry_spot"],
            "lots": pos["lots"],
            "quantity": pos["quantity"],
            "pnl": round(net_pnl, 2),
            "costs": round(costs, 2),
            "premium_pnl_pct": round(pnl_on_premium, 1),
            "entry_time": pos["entry_time"],
            "exit_time": candle["timestamp"],
            "exit_reason": reason,
        })
        self.position = None
