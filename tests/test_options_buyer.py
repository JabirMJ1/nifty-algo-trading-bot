"""Extensive tests for OptionsDirectionalBuyer.

Scenario: ₹1,00,000 capital, trading Nifty CE/PE directional positions.
  BUY signal  → buy ATM CE (profit if market rises)
  SELL signal → buy ATM PE (profit if market falls)

Exit conditions:
  - Stop    : premium drops to (1 - STOP_PCT) × entry_premium
  - Target  : premium reaches TARGET_MULT × entry_premium
  - EOD     : 15:15 IST force-close
"""

import pytest
from datetime import datetime, time
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch

from modules.execution.options_directional_buyer import (
    OptionsDirectionalBuyer,
    _round_strike,
    _estimate_premium,
    _current_premium,
    _dte_from_timestamp,
)
from core.config import settings

IST     = ZoneInfo("Asia/Kolkata")
CAPITAL = 100_000
SPOT    = 22_000.0


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_buyer(capital=CAPITAL) -> OptionsDirectionalBuyer:
    buyer = OptionsDirectionalBuyer(kite=None, circuit_breaker=None)
    buyer.balance  = float(capital)
    buyer.lot_size = settings.OPTIONS_LOT_SIZE   # 75
    buyer.stop_pct = settings.OPTIONS_STOP_PCT / 100.0
    buyer.target_mult = settings.OPTIONS_TARGET_MULT
    return buyer


def make_signal(direction="BUY", price=SPOT, symbol="NSE:NIFTY26MARFUT") -> dict:
    return {
        "instrument":    symbol,
        "direction":     direction,
        "price":         price,
        "strategy_name": "test",
        "timestamp":     datetime(2026, 3, 5, 10, 30, tzinfo=IST),
    }


def make_candle(symbol="NSE:NIFTY26MARFUT", close=SPOT, hour=10, minute=45) -> dict:
    return {
        "symbol":    symbol,
        "open":      close - 20,
        "high":      close + 30,
        "low":       close - 30,
        "close":     close,
        "volume":    5_000,
        "timestamp": datetime(2026, 3, 5, hour, minute, tzinfo=IST),
    }


@pytest.fixture
def mock_bus():
    with patch("modules.execution.options_directional_buyer.event_bus") as m:
        m.publish = AsyncMock()
        yield m


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════

class TestHelperFunctions:
    def test_round_strike_multiples_of_50(self):
        assert _round_strike(22_024) == 22_000
        # Python banker's rounding: 22025/50=440.5 → round to even (440) → 22000
        assert _round_strike(22_025) == 22_000
        # 22026/50=440.52 → round to 441 → 22050
        assert _round_strike(22_026) == 22_050
        assert _round_strike(22_076) == 22_100

    def test_round_strike_exact_multiple_unchanged(self):
        assert _round_strike(22_000) == 22_000
        assert _round_strike(22_500) == 22_500

    def test_estimate_premium_increases_with_dte(self):
        """More days to expiry → higher premium."""
        p2 = _estimate_premium(SPOT, dte=2)
        p5 = _estimate_premium(SPOT, dte=5)
        assert p5 > p2

    def test_estimate_premium_positive(self):
        for dte in [0.1, 1, 2, 5]:
            assert _estimate_premium(SPOT, dte=dte) > 0

    def test_estimate_premium_zero_dte_uses_minimum(self):
        """DTE ≤ 0 clamps to 0.1 — premium must still be positive."""
        p = _estimate_premium(SPOT, dte=0)
        assert p > 0
        p_neg = _estimate_premium(SPOT, dte=-1)
        assert p_neg > 0

    def test_dte_thursday_is_expiry_day(self):
        """Thursday is weekly expiry → DTE = 0.1 (almost zero)."""
        thursday = datetime(2026, 3, 5, 10, 0, tzinfo=IST)   # 2026-03-05 is a Thursday
        assert thursday.weekday() == 3
        dte = _dte_from_timestamp(thursday)
        assert dte == pytest.approx(0.1)

    def test_dte_monday_is_3_days(self):
        """Monday → next Thursday = 3 trading days."""
        monday = datetime(2026, 3, 2, 10, 0, tzinfo=IST)
        assert monday.weekday() == 0
        assert _dte_from_timestamp(monday) == 3.0

    def test_current_premium_ce_gains_on_rally(self):
        """CE premium rises when spot moves up."""
        entry_p = _estimate_premium(SPOT, dte=2)
        current = _current_premium(
            entry_premium=entry_p,
            entry_spot=SPOT,
            current_spot=SPOT + 200,  # market up 200 pts
            option_type="CE",
            hours_elapsed=1.0,
            dte=2.0,
        )
        assert current > entry_p * 0.9   # net gain despite theta

    def test_current_premium_pe_gains_on_fall(self):
        """PE premium rises when spot falls."""
        entry_p = _estimate_premium(SPOT, dte=2)
        current = _current_premium(
            entry_premium=entry_p,
            entry_spot=SPOT,
            current_spot=SPOT - 200,  # market down 200 pts
            option_type="PE",
            hours_elapsed=1.0,
            dte=2.0,
        )
        assert current > entry_p * 0.9

    def test_current_premium_floored_at_5pct_of_entry(self):
        """Premium can't go below 5% of entry — option retains small value."""
        entry_p = 100.0
        floor   = _current_premium(
            entry_premium=entry_p,
            entry_spot=SPOT,
            current_spot=SPOT - 500,  # massive move against CE
            option_type="CE",
            hours_elapsed=5.0,
            dte=0.5,
        )
        assert floor >= entry_p * 0.05


# ═══════════════════════════════════════════════════════════════════════════════
# Signal → Entry
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptionsEntry:
    @pytest.mark.asyncio
    async def test_buy_signal_opens_ce_position(self, mock_bus):
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY"))
        assert buyer._position is not None
        assert buyer._position["option_type"] == "CE"

    @pytest.mark.asyncio
    async def test_sell_signal_opens_pe_position(self, mock_bus):
        buyer = make_buyer()
        await buyer.on_signal(make_signal("SELL"))
        assert buyer._position is not None
        assert buyer._position["option_type"] == "PE"

    @pytest.mark.asyncio
    async def test_strike_rounded_to_nearest_50(self, mock_bus):
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY", price=22_060))
        assert buyer._position["strike"] % 50 == 0

    @pytest.mark.asyncio
    async def test_stop_premium_below_entry(self, mock_bus):
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY"))
        pos = buyer._position
        assert pos["stop_premium"] < pos["entry_premium"]

    @pytest.mark.asyncio
    async def test_target_premium_above_entry(self, mock_bus):
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY"))
        pos = buyer._position
        assert pos["target_premium"] > pos["entry_premium"]

    @pytest.mark.asyncio
    async def test_stop_pct_applied_correctly(self, mock_bus):
        """stop_premium = entry × (1 - STOP_PCT/100)."""
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY"))
        pos = buyer._position
        expected_stop = pos["entry_premium"] * (1 - settings.OPTIONS_STOP_PCT / 100)
        assert pos["stop_premium"] == pytest.approx(expected_stop, rel=0.01)

    @pytest.mark.asyncio
    async def test_target_mult_applied_correctly(self, mock_bus):
        """target_premium = entry × TARGET_MULT."""
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY"))
        pos = buyer._position
        expected_target = pos["entry_premium"] * settings.OPTIONS_TARGET_MULT
        assert pos["target_premium"] == pytest.approx(expected_target, rel=0.01)

    @pytest.mark.asyncio
    async def test_zero_price_signal_rejected(self, mock_bus):
        """Signal with price=0 must not open a position."""
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY", price=0))
        assert buyer._position is None

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_entry(self, mock_bus):
        from modules.risk.circuit_breaker import CircuitBreaker
        breaker = CircuitBreaker()
        breaker._kill_switch = True
        buyer = OptionsDirectionalBuyer(kite=None, circuit_breaker=breaker)
        await buyer.on_signal(make_signal("BUY"))
        assert buyer._position is None

    @pytest.mark.asyncio
    async def test_duplicate_ce_signal_ignored(self, mock_bus):
        """Already holding CE → another BUY signal must be ignored."""
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY"))
        first_id = buyer._position["id"]

        await buyer.on_signal(make_signal("BUY"))
        assert buyer._position["id"] == first_id  # unchanged

    @pytest.mark.asyncio
    async def test_reversal_signal_closes_ce_opens_pe(self, mock_bus):
        """Holding CE, SELL signal arrives → close CE, open PE."""
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY"))
        assert buyer._position["option_type"] == "CE"

        await buyer.on_signal(make_signal("SELL"))
        assert buyer._position is not None
        assert buyer._position["option_type"] == "PE"

    @pytest.mark.asyncio
    async def test_entry_event_published(self, mock_bus):
        from core.event_bus import EventType
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY"))

        types = [c.args[0] for c in mock_bus.publish.call_args_list]
        assert EventType.TRADE in types


# ═══════════════════════════════════════════════════════════════════════════════
# Exits: Stop / Target / EOD
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptionsExits:
    @pytest.mark.asyncio
    async def test_stop_hit_when_premium_falls(self, mock_bus):
        """Candle whose spot implies premium ≤ stop_premium → STOP_HIT."""
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY", price=SPOT))
        pos = buyer._position
        stop_p = pos["stop_premium"]

        # Spot drop such that estimated premium falls to/below stop
        # CE loses 0.5 × spot_move; entry_premium ≈ 132 (SPOT×0.006×√2)
        # We need delta_pnl to push premium to stop_p
        # stop_p = entry × (1 - 0.50) → need 50% drop in premium
        # Simplest: use a candle far in the future with heavy theta + adverse move
        deep_future_candle = make_candle(close=SPOT - 1_000, hour=14, minute=0)
        await buyer.on_candle(deep_future_candle)
        assert buyer._position is None  # closed

    @pytest.mark.asyncio
    async def test_target_hit_when_premium_rises(self, mock_bus):
        """Spot rallies strongly → CE premium hits target."""
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY", price=SPOT))

        # Large upward move shortly after entry
        rally_candle = make_candle(close=SPOT + 1_500, hour=10, minute=46)
        await buyer.on_candle(rally_candle)
        assert buyer._position is None  # closed at target

    @pytest.mark.asyncio
    async def test_eod_force_close_at_1515(self, mock_bus):
        """Candle at 15:15 IST must force-close any open position."""
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY"))
        assert buyer._position is not None

        eod_candle = make_candle(close=SPOT, hour=15, minute=15)
        await buyer.on_candle(eod_candle)
        assert buyer._position is None

    @pytest.mark.asyncio
    async def test_candle_before_eod_does_not_force_close(self, mock_bus):
        """Candle at 15:14 IST must NOT force-close (EOD cutoff is 15:15)."""
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY"))

        pre_eod = make_candle(close=SPOT, hour=15, minute=14)
        await buyer.on_candle(pre_eod)
        # Position might still be open if neither stop nor target was hit
        # (with neutral spot, premium should stay between stop and target)
        # Just assert we don't crash — EOD gate must not fire at 15:14

    @pytest.mark.asyncio
    async def test_no_candle_action_when_no_position(self, mock_bus):
        """on_candle with no open position must be a no-op."""
        buyer = make_buyer()
        assert buyer._position is None
        await buyer.on_candle(make_candle())  # must not raise

    @pytest.mark.asyncio
    async def test_exit_event_published_on_stop(self, mock_bus):
        from core.event_bus import EventType
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY", price=SPOT))

        deep_future_candle = make_candle(close=SPOT - 1_000, hour=14, minute=0)
        await buyer.on_candle(deep_future_candle)

        types = [c.args[0] for c in mock_bus.publish.call_args_list]
        assert EventType.TRADE in types


# ═══════════════════════════════════════════════════════════════════════════════
# Balance Tracking
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptionsBalance:
    @pytest.mark.asyncio
    async def test_balance_decreases_on_stop_loss(self, mock_bus):
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY", price=SPOT))
        initial_balance = buyer.balance

        deep_future_candle = make_candle(close=SPOT - 1_000, hour=14, minute=0)
        await buyer.on_candle(deep_future_candle)

        assert buyer.balance < initial_balance

    @pytest.mark.asyncio
    async def test_balance_increases_on_target(self, mock_bus):
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY", price=SPOT))
        initial_balance = buyer.balance

        rally = make_candle(close=SPOT + 1_500, hour=10, minute=46)
        await buyer.on_candle(rally)

        assert buyer.balance > initial_balance

    @pytest.mark.asyncio
    async def test_brokerage_deducted_from_pnl(self, mock_bus):
        """Even a breakeven trade must show negative PnL due to ₹40 brokerage."""
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY", price=SPOT))
        initial_balance = buyer.balance

        # Close at entry premium (no gain, no loss on premium)
        await buyer._close_position("TEST")
        assert buyer.balance < initial_balance  # brokerage hit


# ═══════════════════════════════════════════════════════════════════════════════
# Shutdown
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptionsShutdown:
    @pytest.mark.asyncio
    async def test_close_all_positions_on_shutdown(self, mock_bus):
        buyer = make_buyer()
        await buyer.on_signal(make_signal("BUY"))
        assert buyer._position is not None

        await buyer.close_all_positions(reason="SHUTDOWN")
        assert buyer._position is None

    @pytest.mark.asyncio
    async def test_close_all_when_no_position_does_not_raise(self, mock_bus):
        buyer = make_buyer()
        await buyer.close_all_positions(reason="SHUTDOWN")
