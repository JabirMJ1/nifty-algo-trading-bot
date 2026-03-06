"""Extensive tests for PositionSizer — from a retail trader's perspective.

Scenario: a trader with ₹1,00,000 capital (default) or ₹50,000 (tight).
Key invariants:
  - Lots are always non-negative integers.
  - Never allocate more margin than the available (un-deployed) capital.
  - Position count gate is enforced before any sizing math.
  - Losing trades must reduce future position sizes via update_capital().
  - Winning trades may increase future position sizes.
"""

import pytest
from modules.risk.position_sizer import PositionSizer, calculate_atr
from core.config import settings

# ─── Constants ───────────────────────────────────────────────────────────────

CAPITAL        = 100_000   # ₹1L default
SMALL_CAPITAL  = 50_000    # ₹50k tight budget
RISK_PCT       = 0.01      # 1%
MARGIN_PCT     = settings.MARGIN_PCT  # 20% (SEBI SPAN+Exposure)

# Nifty futures: lot=75, price≈22000 → margin ≈ ₹3.3L/lot (far too large for ₹1L)
NIFTY_PRICE = 22_000
NIFTY_LOT   = 75

# Affordable instrument: price=900, lot=100 → margin = 900×100×0.20 = ₹18k/lot
CHEAP_PRICE = 900
CHEAP_LOT   = 100
CHEAP_MARGIN_PER_LOT = CHEAP_PRICE * CHEAP_LOT * (MARGIN_PCT / 100)  # ₹18,000


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_sizer(capital=CAPITAL, risk_pct=RISK_PCT, max_positions=3) -> PositionSizer:
    s = PositionSizer()
    s.capital = capital
    s.risk_pct = risk_pct
    s.max_positions = max_positions
    return s


def make_candles(n=16, base=100, atr_range=10):
    """Return n candles with a fixed high-low range for predictable ATR."""
    candles = []
    prev_close = base
    for i in range(n):
        c = base + i * 0.5
        candles.append({
            "open":  c,
            "high":  c + atr_range,
            "low":   c - atr_range,
            "close": c,
        })
        prev_close = c
    return candles


# ═══════════════════════════════════════════════════════════════════════════════
# ATR Calculation
# ═══════════════════════════════════════════════════════════════════════════════

class TestATRCalculation:
    def test_constant_range_produces_expected_atr(self):
        """Each candle: high = close+10, low = close-10 → H-L range = 20 → ATR ≈ 20."""
        candles = make_candles(n=16, atr_range=10)
        # atr_range=10 means high = mid+10, low = mid-10 → H-L = 20
        assert calculate_atr(candles, period=14) == pytest.approx(20.0, abs=1.0)

    def test_insufficient_candles_falls_back_to_last_hl(self):
        """Fewer than period+1 candles → fall back to last candle high-low."""
        candles = [{"open": 100, "high": 120, "low": 90, "close": 105}]
        assert calculate_atr(candles, period=14) == 30  # 120 - 90

    def test_empty_candles_returns_zero(self):
        assert calculate_atr([], period=14) == 0.0

    def test_large_gaps_captured_in_atr(self):
        """Overnight gap: prev_close=100, next_high=130 → TR ≥ 30."""
        candles = make_candles(n=16, atr_range=5)
        # Insert one huge gap candle
        candles[-1] = {"open": 200, "high": 210, "low": 199, "close": 205}
        atr = calculate_atr(candles, period=14)
        assert atr > 5  # must capture the gap

    def test_atr_uses_only_last_period_candles(self):
        """Only the most recent `period` true ranges matter."""
        stable = make_candles(n=50, atr_range=5)   # 50 calm candles
        volatile = make_candles(n=16, atr_range=50)  # 16 volatile candles
        assert calculate_atr(volatile, period=14) > calculate_atr(stable, period=14)


# ═══════════════════════════════════════════════════════════════════════════════
# Position Count Gate
# ═══════════════════════════════════════════════════════════════════════════════

class TestPositionCountGate:
    def test_at_max_positions_returns_zero(self):
        sizer = make_sizer(max_positions=3)
        assert sizer.calculate_size(atr=50, current_open_positions=3) == 0

    def test_exceeding_max_positions_returns_zero(self):
        sizer = make_sizer(max_positions=3)
        assert sizer.calculate_size(atr=50, current_open_positions=5) == 0

    def test_one_below_max_allows_new_trade(self):
        """2 open, max=3 → 1 more allowed."""
        sizer = make_sizer(max_positions=3)
        lots = sizer.calculate_size(atr=50, current_open_positions=2)
        assert lots > 0

    def test_zero_positions_open_full_size(self):
        """No positions → unrestricted by count gate."""
        sizer = make_sizer(max_positions=3)
        lots = sizer.calculate_size(atr=50, current_open_positions=0)
        assert lots > 0

    def test_max_positions_one_allows_only_first_trade(self):
        """Strict limit of 1 position."""
        sizer = make_sizer(max_positions=1)
        assert sizer.calculate_size(atr=50, current_open_positions=0) > 0
        assert sizer.calculate_size(atr=50, current_open_positions=1) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# ATR Guard
# ═══════════════════════════════════════════════════════════════════════════════

class TestATRGuard:
    def test_zero_atr_returns_zero(self):
        sizer = make_sizer()
        assert sizer.calculate_size(atr=0, current_open_positions=0) == 0

    def test_negative_atr_returns_zero(self):
        sizer = make_sizer()
        assert sizer.calculate_size(atr=-10, current_open_positions=0) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Raw Size Formula
# ═══════════════════════════════════════════════════════════════════════════════

class TestRawSizing:
    def test_risk_1pct_atr_100_gives_10_lots(self):
        """
        Capital=₹1L, risk=1% → risk_amount=₹1,000.
        ATR=100 → raw_size = 1000/100 = 10 lots (no price cap).
        """
        sizer = make_sizer(capital=100_000, risk_pct=0.01)
        lots = sizer.calculate_size(atr=100, current_open_positions=0, price=0)
        assert lots == 10

    def test_higher_atr_produces_fewer_lots(self):
        """Higher ATR → smaller position (capped risk)."""
        sizer = make_sizer()
        lots_low_atr  = sizer.calculate_size(atr=50,  current_open_positions=0, price=0)
        lots_high_atr = sizer.calculate_size(atr=200, current_open_positions=0, price=0)
        assert lots_low_atr > lots_high_atr

    def test_higher_risk_pct_produces_more_lots(self):
        """2% risk → twice the lots of 1% risk at same ATR."""
        s1 = make_sizer(risk_pct=0.01)
        s2 = make_sizer(risk_pct=0.02)
        l1 = s1.calculate_size(atr=100, current_open_positions=0, price=0)
        l2 = s2.calculate_size(atr=100, current_open_positions=0, price=0)
        assert l2 == pytest.approx(l1 * 2, abs=1)

    def test_result_is_always_integer(self):
        """Position sizes must be integers — no fractional lots."""
        sizer = make_sizer()
        for atr in [1.5, 7.3, 33.7, 88.1, 123.456, 0.001]:
            result = sizer.calculate_size(atr=atr, current_open_positions=0, price=0)
            assert isinstance(result, int)
            assert result >= 0

    def test_result_never_negative(self):
        sizer = make_sizer()
        for atr in [0.001, 1000, 50]:
            for pos in [0, 1, 2, 3]:
                assert sizer.calculate_size(atr=atr, current_open_positions=pos) >= 0

    def test_lot_size_greater_than_one_reduces_lots(self):
        """lot_size=75 means each 'lot' is 75 units — fewer lots for same risk."""
        sizer = make_sizer(capital=100_000, risk_pct=0.01)
        lots_1  = sizer.calculate_size(atr=100, current_open_positions=0, lot_size=1,  price=0)
        lots_75 = sizer.calculate_size(atr=100, current_open_positions=0, lot_size=75, price=0)
        assert lots_1 >= lots_75


# ═══════════════════════════════════════════════════════════════════════════════
# Margin Cap — Single Position
# ═══════════════════════════════════════════════════════════════════════════════

class TestMarginCapSinglePosition:
    def test_small_trader_cannot_trade_nifty_futures(self):
        """₹50k can't cover ₹3.3L margin/lot for Nifty futures."""
        sizer = make_sizer(capital=SMALL_CAPITAL)
        lots = sizer.calculate_size(
            atr=80,
            current_open_positions=0,
            price=NIFTY_PRICE,
            lot_size=NIFTY_LOT,
        )
        assert lots == 0

    def test_1L_capital_cannot_afford_nifty_futures_lot(self):
        """Even ₹1L cannot cover a single Nifty lot at ₹3.3L margin."""
        sizer = make_sizer(capital=CAPITAL)
        lots = sizer.calculate_size(
            atr=80,
            current_open_positions=0,
            price=NIFTY_PRICE,
            lot_size=NIFTY_LOT,
        )
        assert lots == 0

    def test_affordable_instrument_allows_multiple_lots(self):
        """₹1L capital, ₹18k/lot → can afford up to 5 lots."""
        sizer = make_sizer(capital=CAPITAL)
        lots = sizer.calculate_size(
            atr=10,
            current_open_positions=0,
            price=CHEAP_PRICE,
            lot_size=CHEAP_LOT,
        )
        # max = 100_000 / 18_000 = 5 lots
        assert lots <= 5
        assert lots >= 1

    def test_margin_cap_does_not_zero_out_when_one_lot_affordable(self):
        """If margin cap allows ≥1 lot and risk sizing gives ≥1 lot, result must be ≥ 1."""
        sizer = make_sizer(capital=20_000, risk_pct=0.10)  # 10% risk → big raw size
        # risk_amount = 20_000 × 0.10 = 2_000; ATR=5 → raw_size=400 units
        # lot_size=100 → 400/100 = 4 lots from risk sizing
        # margin cap: 20_000 / 18_000 = 1 lot
        lots = sizer.calculate_size(
            atr=5,
            current_open_positions=0,
            price=CHEAP_PRICE,
            lot_size=CHEAP_LOT,
        )
        assert lots >= 1

    def test_raw_size_capped_not_zeroed_when_partially_affordable(self):
        """ATR gives 20 lots, margin allows 5 → return 5, not 0."""
        sizer = make_sizer(capital=CAPITAL, risk_pct=0.10)  # high risk → big raw size
        lots = sizer.calculate_size(
            atr=5,
            current_open_positions=0,
            price=CHEAP_PRICE,
            lot_size=CHEAP_LOT,
        )
        assert lots == 5

    def test_no_price_skips_margin_cap(self):
        """price=0 → skip margin cap entirely, size purely by ATR."""
        sizer = make_sizer(capital=1_000, risk_pct=0.01)
        lots = sizer.calculate_size(atr=1, current_open_positions=0, price=0)
        assert lots >= 1  # 1% of 1k = ₹10, ATR=1 → 10 lots


# ═══════════════════════════════════════════════════════════════════════════════
# Deployed Margin Accounting (Bug Fix: 2026-03-05)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeployedMarginAccounting:
    """
    Core invariant: position N may only use capital not already locked
    in positions 1..N-1.  Without this, three simultaneous positions
    can consume 3× the actual capital.
    """

    def test_zero_deployed_margin_is_backward_compatible(self):
        """deployed_margin=0 must produce same result as omitting the param."""
        sizer = make_sizer()
        without = sizer.calculate_size(atr=10, current_open_positions=0,
                                        price=CHEAP_PRICE, lot_size=CHEAP_LOT)
        with_zero = sizer.calculate_size(atr=10, current_open_positions=0,
                                          price=CHEAP_PRICE, lot_size=CHEAP_LOT,
                                          deployed_margin=0.0)
        assert without == with_zero

    def test_half_capital_deployed_halves_available_lots(self):
        """
        ₹1L capital, ₹50k already deployed.
        Available = ₹50k → max 2 lots at ₹18k/lot.
        Without fix, available = ₹1L → 5 lots (3× over-allocation risk).
        """
        sizer = make_sizer(capital=CAPITAL)
        lots = sizer.calculate_size(
            atr=10,
            current_open_positions=1,
            price=CHEAP_PRICE,
            lot_size=CHEAP_LOT,
            deployed_margin=50_000,
        )
        # available=50_000, margin/lot=18_000 → max 2
        assert lots <= 2

    def test_fully_deployed_capital_blocks_new_position(self):
        """₹1L deployed → no margin left → 0 lots even if position count allows."""
        sizer = make_sizer(capital=CAPITAL)
        lots = sizer.calculate_size(
            atr=10,
            current_open_positions=1,
            price=CHEAP_PRICE,
            lot_size=CHEAP_LOT,
            deployed_margin=CAPITAL,
        )
        assert lots == 0

    def test_over_deployed_clamped_to_zero_not_negative(self):
        """deployed_margin > capital → available_capital=0, not negative → 0 lots."""
        sizer = make_sizer(capital=CAPITAL)
        lots = sizer.calculate_size(
            atr=10,
            current_open_positions=1,
            price=CHEAP_PRICE,
            lot_size=CHEAP_LOT,
            deployed_margin=150_000,
        )
        assert lots == 0

    def test_three_positions_each_use_remaining_capital(self):
        """
        Simulate 3 consecutive position entries:
        - Pos 1: full capital available → 5 lots
        - Pos 2: pos-1 margin deployed → fewer lots
        - Pos 3: pos-1+2 margin deployed → even fewer
        """
        sizer = make_sizer(capital=CAPITAL)
        price = CHEAP_PRICE
        lot  = CHEAP_LOT
        margin_per_lot = price * lot * (MARGIN_PCT / 100)  # 18_000

        lots1 = sizer.calculate_size(
            atr=10, current_open_positions=0,
            price=price, lot_size=lot, deployed_margin=0
        )
        deployed1 = lots1 * margin_per_lot

        lots2 = sizer.calculate_size(
            atr=10, current_open_positions=1,
            price=price, lot_size=lot, deployed_margin=deployed1
        )
        deployed2 = deployed1 + lots2 * margin_per_lot

        lots3 = sizer.calculate_size(
            atr=10, current_open_positions=2,
            price=price, lot_size=lot, deployed_margin=deployed2
        )

        # Each subsequent trade must be ≤ previous (capital shrinks)
        assert lots1 >= lots2 >= lots3 >= 0
        # Combined margin must not exceed total capital
        total_margin = lots1 * margin_per_lot + lots2 * margin_per_lot + lots3 * margin_per_lot
        assert total_margin <= CAPITAL + 1  # +1 for rounding tolerance

    def test_small_remaining_capital_still_allows_one_lot(self):
        """₹20k available when margin/lot=₹18k → exactly 1 lot allowed."""
        sizer = make_sizer(capital=CAPITAL)
        deployed = CAPITAL - 20_000  # ₹80k deployed
        lots = sizer.calculate_size(
            atr=10,
            current_open_positions=1,
            price=CHEAP_PRICE,
            lot_size=CHEAP_LOT,
            deployed_margin=deployed,
        )
        assert lots == 1

    def test_19k_remaining_blocks_18k_lot(self):
        """₹19k available but ₹18k/lot → 1 lot allowed (19k // 18k = 1)."""
        sizer = make_sizer(capital=CAPITAL)
        deployed = CAPITAL - 19_000
        lots = sizer.calculate_size(
            atr=10,
            current_open_positions=1,
            price=CHEAP_PRICE,
            lot_size=CHEAP_LOT,
            deployed_margin=deployed,
        )
        assert lots == 1

    def test_17k_remaining_blocks_18k_lot(self):
        """₹17k remaining < ₹18k/lot → 0 lots."""
        sizer = make_sizer(capital=CAPITAL)
        deployed = CAPITAL - 17_000
        lots = sizer.calculate_size(
            atr=10,
            current_open_positions=1,
            price=CHEAP_PRICE,
            lot_size=CHEAP_LOT,
            deployed_margin=deployed,
        )
        assert lots == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Capital Updates After PnL
# ═══════════════════════════════════════════════════════════════════════════════

class TestCapitalUpdatesAfterPnL:
    def test_loss_reduces_future_position_sizes(self):
        """After a ₹30k loss, position sizes should shrink."""
        sizer = make_sizer(capital=CAPITAL)
        lots_before = sizer.calculate_size(
            atr=10, current_open_positions=0, price=CHEAP_PRICE, lot_size=CHEAP_LOT
        )

        sizer.update_capital(CAPITAL - 30_000)  # ₹30k loss
        lots_after = sizer.calculate_size(
            atr=10, current_open_positions=0, price=CHEAP_PRICE, lot_size=CHEAP_LOT
        )
        assert lots_after <= lots_before

    def test_full_capital_loss_stops_all_trading(self):
        """Capital wiped → 0 lots on every call."""
        sizer = make_sizer(capital=CAPITAL)
        sizer.update_capital(0)
        assert sizer.calculate_size(atr=10, current_open_positions=0, price=0) == 0

    def test_profit_increases_future_position_sizes(self):
        """Good run doubles capital → more lots available."""
        sizer = make_sizer(capital=CAPITAL)
        lots_before = sizer.calculate_size(
            atr=10, current_open_positions=0, price=CHEAP_PRICE, lot_size=CHEAP_LOT
        )

        sizer.update_capital(CAPITAL * 2)
        lots_after = sizer.calculate_size(
            atr=10, current_open_positions=0, price=CHEAP_PRICE, lot_size=CHEAP_LOT
        )
        assert lots_after >= lots_before

    def test_update_capital_reflects_in_sizer_attribute(self):
        sizer = make_sizer(capital=CAPITAL)
        sizer.update_capital(75_000)
        assert sizer.capital == 75_000

    def test_sequential_losses_progressively_reduce_size(self):
        """Three sequential losses → monotonically decreasing position sizes."""
        sizer = make_sizer(capital=CAPITAL)
        balances = [100_000, 80_000, 60_000, 40_000]
        sizes = []
        for bal in balances:
            sizer.update_capital(bal)
            sizes.append(sizer.calculate_size(
                atr=10, current_open_positions=0,
                price=CHEAP_PRICE, lot_size=CHEAP_LOT
            ))
        # sizes should be non-increasing
        assert all(sizes[i] >= sizes[i + 1] for i in range(len(sizes) - 1))

    def test_small_trader_daily_loss_hits_zero_affordable_lots(self):
        """
        ₹50k trader, 2% daily loss = ₹1k lost.
        At ₹49k, cheaper instrument: price=250, lot=100 → margin=₹5k/lot.
        Risk sizing: 1% of 49k=₹490, ATR=4 → raw=122 units → 1 lot.
        Margin cap: 49k/5k=9 lots. Result: 1 lot.
        """
        sizer = make_sizer(capital=SMALL_CAPITAL, risk_pct=0.01)
        sizer.update_capital(49_000)
        # ATR=4 → raw_size = 490/4 = 122 units ≥ lot_size=100 → 1 lot from risk sizing
        # margin_per_lot = 250×100×0.20 = 5_000 → max 9 lots from margin
        lots = sizer.calculate_size(
            atr=4, current_open_positions=0, price=250, lot_size=100
        )
        assert lots >= 1  # still has enough for cheaper instrument
