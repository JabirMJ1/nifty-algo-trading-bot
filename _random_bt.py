import random
import pandas as pd

from backtesting.options_backtest import OptionsBacktestRunner
from modules.strategy.adaptive_strategy import AdaptiveStrategy

from loguru import logger
logger.remove()

df = pd.read_parquet("data/NSE_NIFTY50_15min.parquet")
all_days = sorted(df.index.normalize().unique())

random.seed(42)

WINDOW = 40  # consecutive trading days per trial
# Pick random start indices ensuring a full window fits
valid_starts = range(0, len(all_days) - WINDOW)
start_indices = sorted(random.sample(list(valid_starts), 20))

print(f"Options backtest — 3 lots, ₹30k capital, 10% target / 10% stop on premium")
print(f"Entry premium ≈ 0.5% of spot (~₹115/unit for Nifty at 23,000)")
print(f"Using {WINDOW}-day CONSECUTIVE windows (not random days) so EMA50 is meaningful")
print()
print(f"{'#':<4} {'Date range':<27} {'Trd':<5} {'Win%':<7} {'Ret%':<8} {'PF':<6} {'MaxDD%':<8} {'Final ₹'}")
print("─" * 74)

total_pnl = 0.0
initial = 30_000.0
all_metrics = []

for trial, start_idx in enumerate(start_indices):
    window_days = all_days[start_idx: start_idx + WINDOW]
    sample_df = df[df.index.normalize().isin(window_days)].copy()
    runner = OptionsBacktestRunner(
        strategy=AdaptiveStrategy(),
        initial_capital=initial,
        lots=3,
        delta=0.45,
        premium_pct=0.005,
        profit_target_pct=10.0,
        stop_loss_pct=10.0,
    )
    m = runner.run(sample_df, symbol="NSE:NIFTY50")
    dr = f"{window_days[0].date()} – {window_days[-1].date()}"
    if "error" in m:
        print(f"{trial+1:<4} {dr:<27} no trades")
        continue
    all_metrics.append(m)
    pnl = m["final_equity"] - initial
    total_pnl += pnl
    marker = "✓" if pnl > 0 else "✗"
    print(
        f"{trial+1:<4} {dr:<27} {m['total_trades']:<5} "
        f"{m['win_rate_pct']:<7.1f} {m['return_pct']:<8.1f} "
        f"{m['profit_factor']:<6.2f} {m['max_drawdown_pct']:<8.1f} "
        f"₹{m['final_equity']:,.0f} {marker}"
    )

print("─" * 74)
# Compound ₹30k through all 8 trials sequentially
capital = initial
for m in all_metrics:
    capital = capital * (1 + m["return_pct"] / 100)
overall_pct = (capital - initial) / initial * 100
n = len(all_metrics)
profitable = sum(1 for m in all_metrics if m["return_pct"] > 0)
print(f"Starting capital:  ₹{initial:,.0f}")
print(f"After {n} trials:   ₹{capital:,.0f}  ({overall_pct:+.1f}% compounded)")
print(f"Profitable trials: {profitable}/{n}")
print(f"Avg return/period: {sum(m['return_pct'] for m in all_metrics)/n:+.1f}% per 40-day window")
