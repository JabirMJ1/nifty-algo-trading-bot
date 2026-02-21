"""Performance metrics — Sharpe, drawdown, win rate, profit factor."""

from typing import Any, Dict, List
import numpy as np
import pandas as pd
from core.logger import get_logger

log = get_logger("performance")


def calculate_metrics(trades: List[Dict[str, Any]], initial_capital: float = 100000) -> Dict[str, Any]:
    """Calculate comprehensive performance metrics from a list of trades.

    Args:
        trades: List of trade dicts with at least 'pnl' field
        initial_capital: Starting capital

    Returns:
        Dict with all performance metrics
    """
    if not trades:
        return {"error": "No trades to analyze"}

    pnls = [t.get("pnl", 0) for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]

    # Basic stats
    total_trades = len(pnls)
    winning_trades = len(winners)
    losing_trades = len(losers)
    win_rate = winning_trades / total_trades * 100 if total_trades else 0

    total_pnl = sum(pnls)
    avg_win = np.mean(winners) if winners else 0
    avg_loss = np.mean(losers) if losers else 0
    avg_rr = abs(avg_win / avg_loss) if avg_loss else float("inf")

    # Profit factor
    gross_profit = sum(winners) if winners else 0
    gross_loss = abs(sum(losers)) if losers else 0.001
    profit_factor = gross_profit / gross_loss

    # Equity curve and drawdown
    equity = [initial_capital]
    for pnl in pnls:
        equity.append(equity[-1] + pnl)

    equity_series = pd.Series(equity)
    peak = equity_series.cummax()
    drawdown = (equity_series - peak) / peak * 100
    max_drawdown = drawdown.min()

    # Sharpe ratio (annualized, assuming ~250 trading days)
    returns = pd.Series(pnls) / initial_capital
    if returns.std() > 0:
        sharpe = (returns.mean() / returns.std()) * np.sqrt(250)
    else:
        sharpe = 0.0

    # Monthly PnL breakdown
    monthly_pnl = {}
    for t in trades:
        entry_time = t.get("entry_time", "")
        if entry_time:
            if isinstance(entry_time, str):
                month_key = entry_time[:7]  # YYYY-MM
            else:
                month_key = entry_time.strftime("%Y-%m")
            monthly_pnl[month_key] = monthly_pnl.get(month_key, 0) + t.get("pnl", 0)

    # Consecutive wins/losses
    max_consec_wins = 0
    max_consec_losses = 0
    current_wins = 0
    current_losses = 0
    for pnl in pnls:
        if pnl > 0:
            current_wins += 1
            current_losses = 0
            max_consec_wins = max(max_consec_wins, current_wins)
        elif pnl < 0:
            current_losses += 1
            current_wins = 0
            max_consec_losses = max(max_consec_losses, current_losses)
        else:
            current_wins = 0
            current_losses = 0

    metrics = {
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate_pct": round(win_rate, 2),
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_risk_reward": round(avg_rr, 2),
        "profit_factor": round(profit_factor, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "max_consecutive_wins": max_consec_wins,
        "max_consecutive_losses": max_consec_losses,
        "final_equity": round(equity[-1], 2),
        "return_pct": round((equity[-1] - initial_capital) / initial_capital * 100, 2),
        "monthly_pnl": monthly_pnl,
    }

    return metrics


def print_report(metrics: Dict[str, Any]):
    """Print a formatted performance report."""
    if "error" in metrics:
        print(f"Error: {metrics['error']}")
        return

    print("\n" + "=" * 50)
    print("       BACKTEST PERFORMANCE REPORT")
    print("=" * 50)
    print(f"  Total Trades:         {metrics['total_trades']}")
    print(f"  Winners:              {metrics['winning_trades']}")
    print(f"  Losers:               {metrics['losing_trades']}")
    print(f"  Win Rate:             {metrics['win_rate_pct']}%")
    print("-" * 50)
    print(f"  Total PnL:            Rs. {metrics['total_pnl']}")
    print(f"  Avg Win:              Rs. {metrics['avg_win']}")
    print(f"  Avg Loss:             Rs. {metrics['avg_loss']}")
    print(f"  Avg Risk:Reward:      {metrics['avg_risk_reward']}")
    print(f"  Profit Factor:        {metrics['profit_factor']}")
    print("-" * 50)
    print(f"  Sharpe Ratio:         {metrics['sharpe_ratio']}")
    print(f"  Max Drawdown:         {metrics['max_drawdown_pct']}%")
    print(f"  Max Consec Wins:      {metrics['max_consecutive_wins']}")
    print(f"  Max Consec Losses:    {metrics['max_consecutive_losses']}")
    print("-" * 50)
    print(f"  Final Equity:         Rs. {metrics['final_equity']}")
    print(f"  Return:               {metrics['return_pct']}%")
    print("=" * 50)

    if metrics.get("monthly_pnl"):
        print("\n  Monthly PnL Breakdown:")
        for month, pnl in sorted(metrics["monthly_pnl"].items()):
            marker = "+" if pnl > 0 else ""
            print(f"    {month}: {marker}{pnl:.2f}")
    print()
