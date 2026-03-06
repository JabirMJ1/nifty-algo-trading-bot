"""Extensive tests for CircuitBreaker — the daily kill switch.

Scenario: ₹1,00,000 capital, 2% max daily loss = ₹2,000 limit.
Once losses exceed ₹2,000 in a day, the circuit breaker must:
  - block ALL new signals
  - fire a KILL_SWITCH event
  - remain locked until explicitly reset (at 9:15 AM next day)
"""

import pytest
from unittest.mock import AsyncMock, patch
from modules.risk.circuit_breaker import CircuitBreaker

CAPITAL     = 100_000
MAX_LOSS_PCT = 2.0
MAX_LOSS_ABS = CAPITAL * (MAX_LOSS_PCT / 100)  # ₹2,000


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_breaker(capital=CAPITAL, max_loss_pct=MAX_LOSS_PCT) -> CircuitBreaker:
    b = CircuitBreaker()
    b._max_loss = capital * (max_loss_pct / 100)
    return b


@pytest.fixture
def mock_event_bus():
    with patch("modules.risk.circuit_breaker.event_bus") as mock:
        mock.publish = AsyncMock()
        yield mock


# ═══════════════════════════════════════════════════════════════════════════════
# Initial State
# ═══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreakerInitialState:
    def test_fresh_breaker_allows_trading(self):
        b = make_breaker()
        assert b.can_trade() is True
        assert b.is_active() is False

    def test_initial_daily_pnl_is_zero(self):
        b = make_breaker()
        assert b.get_daily_pnl() == 0.0

    def test_initial_trade_count_is_zero(self):
        b = make_breaker()
        assert b.get_status()["trades_today"] == 0

    def test_status_dict_keys_present(self):
        b = make_breaker()
        status = b.get_status()
        required = {"kill_switch", "daily_pnl", "max_loss", "trades_today", "can_trade"}
        assert required.issubset(status.keys())


# ═══════════════════════════════════════════════════════════════════════════════
# PnL Accumulation
# ═══════════════════════════════════════════════════════════════════════════════

class TestPnLAccumulation:
    @pytest.mark.asyncio
    async def test_profit_accumulates_correctly(self, mock_event_bus):
        b = make_breaker()
        await b.update_pnl(500)
        await b.update_pnl(300)
        assert b.get_daily_pnl() == pytest.approx(800)

    @pytest.mark.asyncio
    async def test_loss_accumulates_correctly(self, mock_event_bus):
        b = make_breaker()
        await b.update_pnl(-500)
        await b.update_pnl(-300)
        assert b.get_daily_pnl() == pytest.approx(-800)

    @pytest.mark.asyncio
    async def test_mixed_pnl_nets_correctly(self, mock_event_bus):
        b = make_breaker()
        await b.update_pnl(1000)
        await b.update_pnl(-400)
        await b.update_pnl(-200)
        assert b.get_daily_pnl() == pytest.approx(400)

    @pytest.mark.asyncio
    async def test_trade_count_increments_on_each_pnl_update(self, mock_event_bus):
        b = make_breaker()
        await b.update_pnl(-100)
        await b.update_pnl(200)
        await b.update_pnl(-50)
        assert b.get_status()["trades_today"] == 3


# ═══════════════════════════════════════════════════════════════════════════════
# Kill Switch Trigger
# ═══════════════════════════════════════════════════════════════════════════════

class TestKillSwitchTrigger:
    @pytest.mark.asyncio
    async def test_small_loss_does_not_trigger(self, mock_event_bus):
        """₹500 loss < ₹2,000 limit → still allowed."""
        b = make_breaker()
        await b.update_pnl(-500)
        assert b.can_trade() is True
        assert b.is_active() is False

    @pytest.mark.asyncio
    async def test_exactly_at_limit_triggers(self, mock_event_bus):
        """Loss == max_loss → trigger (≤ condition)."""
        b = make_breaker()
        await b.update_pnl(-MAX_LOSS_ABS)
        assert b.is_active() is True
        assert b.can_trade() is False

    @pytest.mark.asyncio
    async def test_one_rupee_above_limit_triggers(self, mock_event_bus):
        """₹2,001 loss → breaker must fire."""
        b = make_breaker()
        await b.update_pnl(-(MAX_LOSS_ABS + 1))
        assert b.is_active() is True

    @pytest.mark.asyncio
    async def test_cumulative_trades_trigger_at_limit(self, mock_event_bus):
        """Three trades: ₹800 + ₹700 + ₹600 = ₹2,100 → triggers."""
        b = make_breaker()
        await b.update_pnl(-800)
        assert b.can_trade() is True
        await b.update_pnl(-700)
        assert b.can_trade() is True
        await b.update_pnl(-600)
        assert b.can_trade() is False

    @pytest.mark.asyncio
    async def test_one_trade_large_enough_to_trigger(self, mock_event_bus):
        """Single trade blowing the daily limit."""
        b = make_breaker()
        await b.update_pnl(-3_000)
        assert b.is_active() is True

    @pytest.mark.asyncio
    async def test_profits_do_not_trigger_circuit_breaker(self, mock_event_bus):
        """Winning day → breaker must never fire."""
        b = make_breaker()
        await b.update_pnl(5_000)
        await b.update_pnl(2_000)
        assert b.can_trade() is True
        assert b.is_active() is False

    @pytest.mark.asyncio
    async def test_profits_then_recovery_does_not_trigger(self, mock_event_bus):
        """
        Start with ₹3k profit, then ₹4k loss → net -₹1k (under ₹2k limit).
        Breaker should NOT fire.
        """
        b = make_breaker()
        await b.update_pnl(3_000)
        await b.update_pnl(-4_000)
        # Net = -₹1,000 which is under the ₹2,000 limit
        assert b.can_trade() is True

    @pytest.mark.asyncio
    async def test_trigger_fires_kill_switch_event(self, mock_event_bus):
        """KILL_SWITCH EventType must be published when breaker fires."""
        from core.event_bus import EventType
        b = make_breaker()
        await b.update_pnl(-3_000)

        published_types = [
            call.args[0] for call in mock_event_bus.publish.call_args_list
        ]
        assert EventType.KILL_SWITCH in published_types

    @pytest.mark.asyncio
    async def test_trigger_fires_alert_event(self, mock_event_bus):
        """ALERT EventType must also be published."""
        from core.event_bus import EventType
        b = make_breaker()
        await b.update_pnl(-3_000)

        published_types = [
            call.args[0] for call in mock_event_bus.publish.call_args_list
        ]
        assert EventType.ALERT in published_types

    @pytest.mark.asyncio
    async def test_trigger_fires_only_once(self, mock_event_bus):
        """
        Even if losses keep accumulating, KILL_SWITCH event should fire
        only once — not on every subsequent update_pnl call.
        """
        from core.event_bus import EventType
        b = make_breaker()
        await b.update_pnl(-3_000)  # triggers
        await b.update_pnl(-1_000)  # already triggered
        await b.update_pnl(-500)

        kill_events = [
            call for call in mock_event_bus.publish.call_args_list
            if call.args[0] == EventType.KILL_SWITCH
        ]
        assert len(kill_events) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Trading Blocked After Trigger
# ═══════════════════════════════════════════════════════════════════════════════

class TestTradingBlockedAfterTrigger:
    @pytest.mark.asyncio
    async def test_pnl_update_still_works_after_trigger(self, mock_event_bus):
        """PnL accumulation continues even after kill switch (for reporting)."""
        b = make_breaker()
        await b.update_pnl(-3_000)
        await b.update_pnl(-500)
        assert b.get_daily_pnl() == pytest.approx(-3_500)

    @pytest.mark.asyncio
    async def test_can_trade_remains_false_after_more_losses(self, mock_event_bus):
        b = make_breaker()
        await b.update_pnl(-3_000)
        await b.update_pnl(-500)
        assert b.can_trade() is False

    @pytest.mark.asyncio
    async def test_profit_after_trigger_does_not_unlock(self, mock_event_bus):
        """
        Once breaker fires, profits during the same day must NOT unlock it.
        This prevents gaming the breaker with alternating losses and wins.
        """
        b = make_breaker()
        await b.update_pnl(-3_000)  # triggers
        assert b.can_trade() is False
        await b.update_pnl(5_000)   # big profit — but should remain locked
        assert b.can_trade() is False


# ═══════════════════════════════════════════════════════════════════════════════
# Daily Reset
# ═══════════════════════════════════════════════════════════════════════════════

class TestDailyReset:
    @pytest.mark.asyncio
    async def test_reset_clears_kill_switch(self, mock_event_bus):
        b = make_breaker()
        await b.update_pnl(-3_000)
        assert b.is_active() is True
        await b.reset()
        assert b.can_trade() is True
        assert b.is_active() is False

    @pytest.mark.asyncio
    async def test_reset_zeroes_daily_pnl(self, mock_event_bus):
        b = make_breaker()
        await b.update_pnl(-1_000)
        await b.reset()
        assert b.get_daily_pnl() == 0.0

    @pytest.mark.asyncio
    async def test_reset_zeroes_trade_count(self, mock_event_bus):
        b = make_breaker()
        await b.update_pnl(-100)
        await b.update_pnl(200)
        await b.reset()
        assert b.get_status()["trades_today"] == 0

    @pytest.mark.asyncio
    async def test_new_losses_after_reset_trigger_fresh_breaker(self, mock_event_bus):
        """After reset, the full ₹2,000 budget is available again."""
        b = make_breaker()
        await b.update_pnl(-3_000)
        await b.reset()
        # ₹1,500 loss after reset — should NOT trigger (under ₹2k limit)
        await b.update_pnl(-1_500)
        assert b.can_trade() is True

    @pytest.mark.asyncio
    async def test_fresh_reset_on_untripped_breaker_is_idempotent(self, mock_event_bus):
        """Resetting an already-fresh breaker is safe."""
        b = make_breaker()
        await b.reset()
        assert b.can_trade() is True
        assert b.get_daily_pnl() == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Different Capital Sizes
# ═══════════════════════════════════════════════════════════════════════════════

class TestCapitalSizeVariants:
    @pytest.mark.asyncio
    async def test_50k_trader_triggers_at_1000_loss(self, mock_event_bus):
        """₹50k capital, 2% limit = ₹1,000."""
        b = make_breaker(capital=50_000)
        await b.update_pnl(-1_000)
        assert b.is_active() is True

    @pytest.mark.asyncio
    async def test_50k_trader_safe_at_999_loss(self, mock_event_bus):
        """₹50k capital, ₹999 loss < ₹1,000 limit → safe."""
        b = make_breaker(capital=50_000)
        await b.update_pnl(-999)
        assert b.can_trade() is True

    @pytest.mark.asyncio
    async def test_5L_trader_triggers_at_10k_loss(self, mock_event_bus):
        """₹5L capital, 2% = ₹10,000 limit."""
        b = make_breaker(capital=500_000)
        await b.update_pnl(-10_000)
        assert b.is_active() is True

    @pytest.mark.asyncio
    async def test_tight_1pct_limit_triggers_sooner(self, mock_event_bus):
        """
        1% max loss on ₹1L = ₹1,000 limit.
        ₹1,500 loss must trigger even though it's under the 2% default.
        """
        b = make_breaker(capital=CAPITAL, max_loss_pct=1.0)
        await b.update_pnl(-1_500)
        assert b.is_active() is True
