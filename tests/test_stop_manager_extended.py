"""Extensive tests for StopManager and StopLevel.

Real-world scenario: you're long Nifty futures at 22,000.
ATR = 100 pts.
  - Hard stop   = 22,000 - 1.5×100 = 21,850  (below entry)
  - Trail active = 22,000 + 1.0×100 = 22,100  (1 ATR above entry)
  - Target       = 22,000 + 3.0×100 = 22,300  (3 ATR above entry)

For a short at 22,000:
  - Hard stop   = 22,000 + 1.5×100 = 22,150
  - Trail active = 22,000 - 1.0×100 = 21,900
  - Target       = 22,000 - 3.0×100 = 21,700
"""

import pytest
from modules.risk.stop_manager import StopManager, StopLevel
from core.config import settings

ENTRY = 22_000.0
ATR   = 100.0
STOP_MULT  = settings.STOP_ATR_MULTIPLIER   # 1.5
TRAIL_MULT = settings.TRAIL_ATR_MULTIPLIER  # 1.0
TARGET_MULT = settings.TARGET_ATR_MULTIPLIER # 3.0


# ═══════════════════════════════════════════════════════════════════════════════
# StopLevel Initialisation
# ═══════════════════════════════════════════════════════════════════════════════

class TestStopLevelInit:
    def test_buy_stop_below_entry(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        assert sl.stop_loss == pytest.approx(ENTRY - ATR * STOP_MULT)

    def test_buy_target_above_entry(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        assert sl.target == pytest.approx(ENTRY + ATR * TARGET_MULT)

    def test_buy_trail_activation_above_entry(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        assert sl.trail_activation == pytest.approx(ENTRY + ATR * TRAIL_MULT)

    def test_sell_stop_above_entry(self):
        sl = StopLevel(ENTRY, "SELL", ATR)
        assert sl.stop_loss == pytest.approx(ENTRY + ATR * STOP_MULT)

    def test_sell_target_below_entry(self):
        sl = StopLevel(ENTRY, "SELL", ATR)
        assert sl.target == pytest.approx(ENTRY - ATR * TARGET_MULT)

    def test_sell_trail_activation_below_entry(self):
        sl = StopLevel(ENTRY, "SELL", ATR)
        assert sl.trail_activation == pytest.approx(ENTRY - ATR * TRAIL_MULT)

    def test_trailing_not_active_at_entry(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        assert sl.trailing_active is False

    def test_highest_pnl_price_initialised_to_entry(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        assert sl.highest_pnl_price == ENTRY


# ═══════════════════════════════════════════════════════════════════════════════
# Long Position — Hard Stop
# ═══════════════════════════════════════════════════════════════════════════════

class TestLongHardStop:
    def test_price_above_stop_returns_none(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        stop = sl.stop_loss
        assert sl.update(stop + 1) is None

    def test_price_exactly_at_stop_triggers(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        result = sl.update(sl.stop_loss)
        assert result == "STOP_HIT"

    def test_price_below_stop_triggers(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        result = sl.update(sl.stop_loss - 10)
        assert result == "STOP_HIT"

    def test_small_dip_close_to_stop_does_not_trigger(self):
        """Price at stop + 0.05 (1 tick above) → no exit."""
        sl = StopLevel(ENTRY, "BUY", ATR)
        result = sl.update(sl.stop_loss + 0.05)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Long Position — Target
# ═══════════════════════════════════════════════════════════════════════════════

class TestLongTarget:
    def test_price_at_target_triggers(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        result = sl.update(sl.target)
        assert result == "TARGET_HIT"

    def test_price_above_target_triggers(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        result = sl.update(sl.target + 50)
        assert result == "TARGET_HIT"

    def test_price_just_below_target_does_not_trigger(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        result = sl.update(sl.target - 0.05)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Long Position — Trailing Stop
# ═══════════════════════════════════════════════════════════════════════════════

class TestLongTrailingStop:
    def test_trailing_not_active_before_activation_price(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        sl.update(sl.trail_activation - 1)
        assert sl.trailing_active is False

    def test_trailing_activates_at_activation_price(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        sl.update(sl.trail_activation)
        assert sl.trailing_active is True

    def test_trailing_activates_above_activation_price(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        sl.update(sl.trail_activation + 20)
        assert sl.trailing_active is True

    def test_trailing_stop_ratchets_up_with_price(self):
        """Stop must move up as price rises — never fall back down."""
        sl = StopLevel(ENTRY, "BUY", ATR)
        sl.update(sl.trail_activation + 10)  # activate
        stop_after_activation = sl.stop_loss

        sl.update(sl.trail_activation + 50)  # price rises
        assert sl.stop_loss > stop_after_activation

    def test_trailing_stop_never_moves_down_on_pullback(self):
        """Once stop ratcheted up, pullback must NOT lower the stop."""
        sl = StopLevel(ENTRY, "BUY", ATR)
        sl.update(sl.trail_activation + 100)  # move up
        high_water_stop = sl.stop_loss

        sl.update(sl.trail_activation + 50)   # price pulls back
        assert sl.stop_loss >= high_water_stop

    def test_trail_stop_hit_after_ratchet(self):
        """After trailing activates and price drops below ratcheted stop → TRAIL_STOP_HIT."""
        sl = StopLevel(ENTRY, "BUY", ATR)
        sl.update(sl.trail_activation + 50)   # activate and push up
        assert sl.trailing_active is True
        ratcheted_stop = sl.stop_loss

        result = sl.update(ratcheted_stop - 1)
        assert result == "TRAIL_STOP_HIT"

    def test_initial_stop_hit_without_trailing_is_stop_hit(self):
        """Without trailing ever activating, hitting stop returns STOP_HIT."""
        sl = StopLevel(ENTRY, "BUY", ATR)
        result = sl.update(sl.stop_loss - 1)
        assert result == "STOP_HIT"  # not TRAIL_STOP_HIT

    def test_highest_pnl_price_tracked_correctly(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        sl.update(ENTRY + 150)  # new high
        assert sl.highest_pnl_price == pytest.approx(ENTRY + 150)

        sl.update(ENTRY + 100)  # pullback
        assert sl.highest_pnl_price == pytest.approx(ENTRY + 150)  # unchanged


# ═══════════════════════════════════════════════════════════════════════════════
# Short Position — Hard Stop
# ═══════════════════════════════════════════════════════════════════════════════

class TestShortHardStop:
    def test_price_below_stop_returns_none(self):
        sl = StopLevel(ENTRY, "SELL", ATR)
        result = sl.update(sl.stop_loss - 1)
        assert result is None

    def test_price_at_stop_triggers(self):
        sl = StopLevel(ENTRY, "SELL", ATR)
        result = sl.update(sl.stop_loss)
        assert result == "STOP_HIT"

    def test_price_above_stop_triggers(self):
        sl = StopLevel(ENTRY, "SELL", ATR)
        result = sl.update(sl.stop_loss + 10)
        assert result == "STOP_HIT"


# ═══════════════════════════════════════════════════════════════════════════════
# Short Position — Target and Trail
# ═══════════════════════════════════════════════════════════════════════════════

class TestShortTargetAndTrail:
    def test_short_target_hit_below_entry(self):
        sl = StopLevel(ENTRY, "SELL", ATR)
        result = sl.update(sl.target)
        assert result == "TARGET_HIT"

    def test_short_target_below_entry(self):
        sl = StopLevel(ENTRY, "SELL", ATR)
        assert sl.target < ENTRY

    def test_short_trailing_activates_below_entry(self):
        sl = StopLevel(ENTRY, "SELL", ATR)
        sl.update(sl.trail_activation)
        assert sl.trailing_active is True

    def test_short_trailing_ratchets_down_with_price(self):
        """For short: stop must ratchet DOWN as price falls."""
        sl = StopLevel(ENTRY, "SELL", ATR)
        sl.update(sl.trail_activation - 10)   # activate
        stop_after_activation = sl.stop_loss

        sl.update(sl.trail_activation - 50)   # price falls further
        assert sl.stop_loss < stop_after_activation

    def test_short_trail_stop_hit_on_bounce(self):
        sl = StopLevel(ENTRY, "SELL", ATR)
        sl.update(sl.trail_activation - 50)    # activate and ratchet down
        assert sl.trailing_active is True
        ratcheted_stop = sl.stop_loss

        result = sl.update(ratcheted_stop + 1)
        assert result == "TRAIL_STOP_HIT"


# ═══════════════════════════════════════════════════════════════════════════════
# StopManager (Multi-Symbol Dispatch)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStopManager:
    def test_set_and_check_stop(self):
        mgr = StopManager()
        mgr.set_stops("NSE:NIFTY26MARFUT", ENTRY, "BUY", ATR)
        result = mgr.check_stops("NSE:NIFTY26MARFUT", ENTRY - ATR * STOP_MULT - 1)
        assert result == "STOP_HIT"

    def test_check_unknown_symbol_returns_none(self):
        mgr = StopManager()
        result = mgr.check_stops("NSE:UNKNOWN", 22_000)
        assert result is None

    def test_remove_stops_clears_symbol(self):
        mgr = StopManager()
        mgr.set_stops("NSE:NIFTY26MARFUT", ENTRY, "BUY", ATR)
        mgr.remove_stops("NSE:NIFTY26MARFUT")
        assert mgr.check_stops("NSE:NIFTY26MARFUT", ENTRY - 1000) is None

    def test_get_stop_level_returns_correct_object(self):
        mgr = StopManager()
        mgr.set_stops("NSE:NIFTY26MARFUT", ENTRY, "BUY", ATR)
        sl = mgr.get_stop_level("NSE:NIFTY26MARFUT")
        assert isinstance(sl, StopLevel)
        assert sl.entry_price == ENTRY

    def test_get_stop_level_none_for_unknown(self):
        mgr = StopManager()
        assert mgr.get_stop_level("UNKNOWN") is None

    def test_multiple_symbols_tracked_independently(self):
        """
        Long NSE:NIFTY @ 22,000 and short MCX:CRUDEOIL @ 5,000.
        Both managed without interfering.
        """
        mgr = StopManager()
        mgr.set_stops("NSE:NIFTY26MARFUT", 22_000, "BUY",  100)
        mgr.set_stops("MCX:CRUDEOILFUT",   5_000,  "SELL", 30)

        # Nifty: price above its stop → no exit
        assert mgr.check_stops("NSE:NIFTY26MARFUT", 22_100) is None
        # Crude: price above its stop (short) → stop hit
        crude_stop = mgr.get_stop_level("MCX:CRUDEOILFUT").stop_loss
        assert mgr.check_stops("MCX:CRUDEOILFUT", crude_stop + 1) == "STOP_HIT"

    def test_overwrite_stops_replaces_previous(self):
        """Setting stops twice for the same symbol replaces the old StopLevel."""
        mgr = StopManager()
        mgr.set_stops("NSE:NIFTY26MARFUT", 22_000, "BUY", 100)
        mgr.set_stops("NSE:NIFTY26MARFUT", 22_500, "BUY", 50)  # new entry

        sl = mgr.get_stop_level("NSE:NIFTY26MARFUT")
        assert sl.entry_price == 22_500

    def test_get_all_stops_returns_dict(self):
        mgr = StopManager()
        mgr.set_stops("NSE:NIFTY26MARFUT", ENTRY, "BUY", ATR)
        mgr.set_stops("MCX:CRUDEOILFUT",   5_000, "SELL", 30)
        all_stops = mgr.get_all_stops()
        assert "NSE:NIFTY26MARFUT" in all_stops
        assert "MCX:CRUDEOILFUT" in all_stops
        assert all_stops["NSE:NIFTY26MARFUT"]["entry_price"] == ENTRY

    def test_remove_one_symbol_leaves_others_intact(self):
        mgr = StopManager()
        mgr.set_stops("A", ENTRY, "BUY", ATR)
        mgr.set_stops("B", ENTRY, "SELL", ATR)
        mgr.remove_stops("A")
        assert mgr.get_stop_level("A") is None
        assert mgr.get_stop_level("B") is not None

    def test_stop_level_to_dict_contains_required_keys(self):
        sl = StopLevel(ENTRY, "BUY", ATR)
        d = sl.to_dict()
        for key in ("entry_price", "direction", "stop_loss", "target",
                    "trailing_active", "highest_pnl_price"):
            assert key in d


# ═══════════════════════════════════════════════════════════════════════════════
# Edge Cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestStopEdgeCases:
    def test_tiny_atr_produces_tight_stops(self):
        """Calm market (ATR=1): stops are very close to entry."""
        sl = StopLevel(ENTRY, "BUY", atr=1)
        stop_distance = ENTRY - sl.stop_loss
        assert stop_distance == pytest.approx(1.5, rel=0.01)

    def test_large_atr_produces_wide_stops(self):
        """Volatile market (ATR=500): stops are wide."""
        sl = StopLevel(ENTRY, "BUY", atr=500)
        stop_distance = ENTRY - sl.stop_loss
        assert stop_distance == pytest.approx(750, rel=0.01)

    def test_same_entry_price_for_buy_and_sell(self):
        """Both directions from the same entry — stops must be mirrored."""
        buy_sl = StopLevel(ENTRY, "BUY",  ATR)
        sel_sl = StopLevel(ENTRY, "SELL", ATR)
        assert ENTRY - buy_sl.stop_loss == pytest.approx(sel_sl.stop_loss - ENTRY)

    def test_target_always_further_than_trail_activation(self):
        """Target (3×ATR) must always be further away than trail activation (1×ATR)."""
        for direction in ("BUY", "SELL"):
            sl = StopLevel(ENTRY, direction, ATR)
            if direction == "BUY":
                assert sl.target > sl.trail_activation
            else:
                assert sl.target < sl.trail_activation

    def test_no_exit_at_entry_price(self):
        """At entry price, neither stop nor target should trigger."""
        for direction in ("BUY", "SELL"):
            sl = StopLevel(ENTRY, direction, ATR)
            assert sl.update(ENTRY) is None
