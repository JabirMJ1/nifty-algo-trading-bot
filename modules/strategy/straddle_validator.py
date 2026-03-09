"""Straddle premium validator — captures real vs model premiums each day.

At 9:20 AM: fetches the real ATM CE + PE prices from Kite and records them
alongside the model estimate.

At 3:15 PM: fetches EOD option prices and computes actual vs model P&L.

Writes one CSV row per trading day to data/straddle_validation.csv so you can
compare the estimation model against real market prices over time.
"""

import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from core.logger import get_logger
from modules.sensory.market_hours import now_ist

NIFTY_LOT_SIZE = 25
STRIKE_INTERVAL = 50
DATA_DIR = Path("data")
CSV_PATH = DATA_DIR / "straddle_validation.csv"
STATE_PATH = DATA_DIR / "straddle_today.json"  # persists entry across restarts

HEADERS = [
    "date",
    "weekday",
    "strike",
    "spot_at_entry",
    "model_premium",
    "real_ce",
    "real_pe",
    "real_avg",
    "premium_diff_pct",
    "ce_symbol",
    "pe_symbol",
    "eod_spot",
    "eod_ce",
    "eod_pe",
    "real_pnl",
    "model_pnl",
    "notes",
]

log = get_logger("straddle_validator")


def _round_strike(price: float) -> int:
    return int(round(price / STRIKE_INTERVAL) * STRIKE_INTERVAL)


def _estimate_premium(spot: float, dte: float) -> float:
    """Same formula used in Straddle920Strategy."""
    if dte <= 0:
        dte = 0.1
    return round(spot * 0.006 * math.sqrt(dte), 2)


def _days_to_expiry(dt: datetime) -> float:
    """Days to next Thursday (Nifty weekly expiry). Mon=0 … Thu=3."""
    days = (3 - dt.weekday()) % 7
    return days if days > 0 else 0.15  # expiry day itself


class StraddleValidator:
    """Records real vs estimated ATM option premiums for post-analysis.

    Works in both paper and live mode — only needs a Kite REST connection,
    never places any orders.
    """

    def __init__(self, kite=None):
        self.kite = kite
        self._today: Optional[Dict[str, Any]] = None
        DATA_DIR.mkdir(exist_ok=True)
        self._ensure_csv_header()
        self._load_today_state()

    def _ensure_csv_header(self):
        if not CSV_PATH.exists():
            with open(CSV_PATH, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=HEADERS).writeheader()
            log.info(f"Created straddle validation log: {CSV_PATH}")

    def _load_today_state(self):
        """Restore morning entry from disk if bot restarted mid-day."""
        if not STATE_PATH.exists():
            return
        try:
            data = json.loads(STATE_PATH.read_text())
            today_str = now_ist().strftime("%Y-%m-%d")
            if data.get("date") == today_str:
                self._today = data
                log.info(f"Restored straddle entry from disk: {today_str}")
            else:
                STATE_PATH.unlink(missing_ok=True)  # stale from previous day
        except Exception as e:
            log.warning(f"Could not load straddle today state: {e}")

    def _save_today_state(self):
        """Persist today's entry to disk so a mid-day restart doesn't lose it."""
        if not self._today:
            return
        try:
            STATE_PATH.write_text(json.dumps(self._today))
        except Exception as e:
            log.warning(f"Could not save straddle today state: {e}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_option_symbols(self, strike: int) -> Dict[str, str]:
        """Return nearest-expiry NFO CE + PE trading symbols for a strike.

        Picks the option with the earliest expiry date to ensure we get the
        weekly contract, not the monthly (which has the same strike but much
        higher premium and different Greeks).
        """
        if not self.kite:
            return {}
        try:
            instruments = self.kite.instruments("NFO")
            today = now_ist().date()
            candidates: Dict[str, list] = {"CE": [], "PE": []}
            for inst in instruments:
                if (
                    inst["name"] == "NIFTY"
                    and inst["instrument_type"] in ("CE", "PE")
                    and inst["strike"] == float(strike)
                    and inst["expiry"] >= today
                ):
                    candidates[inst["instrument_type"]].append(inst)

            ce_sym = pe_sym = None
            if candidates["CE"]:
                nearest_ce = min(candidates["CE"], key=lambda x: x["expiry"])
                ce_sym = f"NFO:{nearest_ce['tradingsymbol']}"
            if candidates["PE"]:
                nearest_pe = min(candidates["PE"], key=lambda x: x["expiry"])
                pe_sym = f"NFO:{nearest_pe['tradingsymbol']}"

            return {"CE": ce_sym, "PE": pe_sym}
        except Exception as e:
            log.error(f"Failed to resolve option symbols: {e}")
            return {}

    def _fetch_ltp(self, *symbols: str) -> Dict[str, float]:
        """Fetch last traded price for each symbol via kite.quote()."""
        if not self.kite or not symbols:
            return {}
        try:
            quotes = self.kite.quote(list(symbols))
            return {
                sym: quotes[sym]["last_price"]
                for sym in symbols
                if sym in quotes
            }
        except Exception as e:
            log.error(f"Failed to fetch quotes {symbols}: {e}")
            return {}

    def _write_row(self):
        if not self._today:
            return
        with open(CSV_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=HEADERS)
            writer.writerow({k: self._today.get(k, "") for k in HEADERS})
        log.info(f"Straddle validation row saved → {CSV_PATH}")

    # ------------------------------------------------------------------
    # Public API — called by the scheduler
    # ------------------------------------------------------------------

    async def record_entry(self):
        """9:20 AM snapshot: spot price, real option premiums, model estimate."""
        if not self.kite:
            log.warning("Straddle validator: no Kite connection, skipping")
            return

        now = now_ist()
        date_str = now.strftime("%Y-%m-%d")
        weekday = now.strftime("%A")

        # Spot price
        prices = self._fetch_ltp("NSE:NIFTY 50")
        spot = prices.get("NSE:NIFTY 50")
        if not spot:
            log.error("Could not fetch NIFTY 50 spot — skipping validation entry")
            return

        strike = _round_strike(spot)
        dte = _days_to_expiry(now)
        model_premium = _estimate_premium(spot, dte)

        # Real option prices
        symbols = self._resolve_option_symbols(strike)
        ce_sym = symbols.get("CE", "")
        pe_sym = symbols.get("PE", "")

        real_ce = real_pe = None
        if ce_sym and pe_sym:
            opt_prices = self._fetch_ltp(ce_sym, pe_sym)
            real_ce = opt_prices.get(ce_sym)
            real_pe = opt_prices.get(pe_sym)

        real_avg = (
            round((real_ce + real_pe) / 2, 2)
            if real_ce is not None and real_pe is not None
            else None
        )
        diff_pct = (
            round((real_avg - model_premium) / model_premium * 100, 1)
            if real_avg and model_premium
            else None
        )

        self._today = {
            "date": date_str,
            "weekday": weekday,
            "strike": strike,
            "spot_at_entry": round(spot, 2),
            "model_premium": model_premium,
            "real_ce": real_ce,
            "real_pe": real_pe,
            "real_avg": real_avg,
            "premium_diff_pct": diff_pct,
            "ce_symbol": ce_sym,
            "pe_symbol": pe_sym,
            "eod_spot": None,
            "eod_ce": None,
            "eod_pe": None,
            "real_pnl": None,
            "model_pnl": None,
            "notes": "",
        }

        log.info(
            f"STRADDLE VALIDATION ENTRY | {date_str} | Strike={strike} | "
            f"Spot={spot:.0f} | Model={model_premium:.1f} | "
            f"Real CE={real_ce} PE={real_pe} | Diff={diff_pct}%"
        )
        self._save_today_state()

    async def record_exit(self):
        """3:15 PM snapshot: EOD option prices + real vs model P&L."""
        if not self._today:
            log.debug("Straddle validator: no entry recorded today, skipping exit")
            return
        if not self.kite:
            return

        spot_prices = self._fetch_ltp("NSE:NIFTY 50")
        eod_spot = spot_prices.get("NSE:NIFTY 50")

        ce_sym = self._today.get("ce_symbol", "")
        pe_sym = self._today.get("pe_symbol", "")

        eod_ce = eod_pe = None
        if ce_sym and pe_sym:
            opt_prices = self._fetch_ltp(ce_sym, pe_sym)
            eod_ce = opt_prices.get(ce_sym)
            eod_pe = opt_prices.get(pe_sym)

        # Real P&L: credit at entry minus buyback cost at EOD
        real_pnl = None
        if (
            self._today["real_ce"] is not None
            and self._today["real_pe"] is not None
            and eod_ce is not None
            and eod_pe is not None
        ):
            entry_credit = (self._today["real_ce"] + self._today["real_pe"]) * NIFTY_LOT_SIZE
            exit_cost = (eod_ce + eod_pe) * NIFTY_LOT_SIZE
            real_pnl = round(entry_credit - exit_cost, 2)

        # Model P&L: model premium minus intrinsic value at EOD spot
        model_pnl = None
        if self._today["model_premium"] and eod_spot:
            strike = self._today["strike"]
            mp = self._today["model_premium"]
            ce_val = max(0.0, eod_spot - strike)
            pe_val = max(0.0, strike - eod_spot)
            model_pnl = round(
                (mp - ce_val + mp - pe_val) * NIFTY_LOT_SIZE, 2
            )

        self._today.update({
            "eod_spot": round(eod_spot, 2) if eod_spot else None,
            "eod_ce": eod_ce,
            "eod_pe": eod_pe,
            "real_pnl": real_pnl,
            "model_pnl": model_pnl,
        })

        log.info(
            f"STRADDLE VALIDATION EXIT | {self._today['date']} | "
            f"EOD CE={eod_ce} PE={eod_pe} | "
            f"Real PnL={real_pnl} | Model PnL={model_pnl}"
        )

        self._write_row()
        self._today = None
        STATE_PATH.unlink(missing_ok=True)
