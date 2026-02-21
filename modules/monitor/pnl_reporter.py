"""End-of-day PnL reporting module."""

from typing import Any, Dict, List
from core.event_bus import event_bus, EventType
from core.logger import get_logger
from modules.sensory.market_hours import now_ist

log = get_logger("pnl_reporter")


class PnLReporter:
    """Generates and sends EOD PnL summary."""

    def __init__(self, trade_journal=None, circuit_breaker=None, paper_trader=None):
        self._journal = trade_journal
        self._breaker = circuit_breaker
        self._trader = paper_trader

    async def generate_eod_report(self) -> str:
        """Generate end-of-day summary."""
        trades = []
        if self._journal:
            trades = self._journal.get_trades_today()
        elif self._trader:
            trades = [t.to_event_data() for t in self._trader.trade_history]

        if not trades:
            return self._format_no_trades()

        total_trades = len(trades)
        closed_trades = [t for t in trades if t.get("exit_price")]
        winners = [t for t in closed_trades if t.get("pnl", 0) > 0]
        losers = [t for t in closed_trades if t.get("pnl", 0) < 0]

        gross_pnl = sum(t.get("pnl", 0) for t in closed_trades)
        total_costs = sum(t.get("costs", 0) for t in closed_trades)
        net_pnl = gross_pnl  # costs already deducted in paper_trader

        win_rate = (len(winners) / len(closed_trades) * 100) if closed_trades else 0

        balance = self._trader.balance if self._trader else 0
        daily_pnl = self._breaker.get_daily_pnl() if self._breaker else net_pnl

        report = (
            f"<b>EOD Summary — {now_ist().strftime('%d %b %Y')}</b>\n"
            f"{'='*30}\n"
            f"Trades Taken: {total_trades}\n"
            f"Closed: {len(closed_trades)}\n"
            f"Winners: {len(winners)} | Losers: {len(losers)}\n"
            f"Win Rate: {win_rate:.1f}%\n"
            f"{'='*30}\n"
            f"Gross PnL: {gross_pnl:.2f}\n"
            f"Costs: {total_costs:.2f}\n"
            f"<b>Net PnL: {net_pnl:.2f}</b>\n"
            f"{'='*30}\n"
            f"Balance: {balance:.2f}\n"
        )

        # Per-trade breakdown
        if closed_trades:
            report += "\n<b>Trade Details:</b>\n"
            for t in closed_trades:
                pnl = t.get("pnl", 0)
                result = "W" if pnl > 0 else "L"
                report += (
                    f"  [{result}] {t['instrument']} {t['direction']} "
                    f"| PnL: {pnl:.2f} | {t.get('exit_reason', 'N/A')}\n"
                )

        return report

    def _format_no_trades(self) -> str:
        return (
            f"<b>EOD Summary — {now_ist().strftime('%d %b %Y')}</b>\n"
            f"No trades taken today."
        )

    async def send_eod_report(self):
        """Generate and send EOD report via Telegram."""
        report = await self.generate_eod_report()
        log.info(f"EOD Report:\n{report}")
        await event_bus.publish(
            EventType.ALERT,
            {"type": "EOD_SUMMARY", "message": report},
        )

    def start(self):
        log.info("PnL reporter started")
