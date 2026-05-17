"""Fetch real historical option prices for straddle entries and compute P&L.

Entries recorded by StraddleValidator:
  Mar 5  (Thu, expiry day) : Strike 24600  CE=244.8  PE=249.2
  Mar 6  (Fri, 6 DTE)      : Strike 24600  CE=210.85 PE=217.0
  Mar 9  (Mon, 3 DTE)      : Strike 23800  CE=241.15 PE=248.45

Strategy: short straddle — sell CE + PE at 9:20 AM, buy back at 3:15 PM.
P&L per lot = (entry_total - exit_total) * NIFTY_LOT_SIZE

Usage:
    python check_straddle_pnl.py
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytz
from dotenv import load_dotenv
from kiteconnect import KiteConnect

load_dotenv()

API_KEY = os.getenv("KITE_API_KEY")
ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN")

if not API_KEY or not ACCESS_TOKEN:
    print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN must be set in .env")
    sys.exit(1)

kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)

IST = pytz.timezone("Asia/Kolkata")
NIFTY_LOT_SIZE = 25

# ──────────────────────────────────────────────────────────────────────────────
# Entries from logs (real prices fetched by StraddleValidator at 9:20 AM)
# ──────────────────────────────────────────────────────────────────────────────
ENTRIES = [
    {"date": date(2026, 3, 5),  "strike": 24600, "entry_ce": 244.80, "entry_pe": 249.20},
    {"date": date(2026, 3, 6),  "strike": 24600, "entry_ce": 210.85, "entry_pe": 217.00},
    {"date": date(2026, 3, 9),  "strike": 23800, "entry_ce": 241.15, "entry_pe": 248.45},
]

# ──────────────────────────────────────────────────────────────────────────────
# Helper: find nearest-expiry option token for a given strike/type
# ──────────────────────────────────────────────────────────────────────────────
def resolve_token(instruments, strike: int, opt_type: str, as_of: date):
    candidates = [
        i for i in instruments
        if i["name"] == "NIFTY"
        and i["instrument_type"] == opt_type
        and i["strike"] == float(strike)
        and i["expiry"] >= as_of
    ]
    if not candidates:
        return None, None
    nearest = min(candidates, key=lambda x: x["expiry"])
    return nearest["instrument_token"], nearest["tradingsymbol"]


# ──────────────────────────────────────────────────────────────────────────────
# Helper: fetch 1-min candle at a specific IST time (returns close price)
# ──────────────────────────────────────────────────────────────────────────────
def price_at(token, target_dt_ist: datetime) -> float | None:
    """Get the close price of the 1-min candle at target_dt_ist."""
    from_dt = target_dt_ist - timedelta(minutes=5)
    to_dt   = target_dt_ist + timedelta(minutes=2)
    try:
        candles = kite.historical_data(
            instrument_token=token,
            from_date=from_dt,
            to_date=to_dt,
            interval="minute",
        )
    except Exception as e:
        print(f"    [ERROR fetching history for token {token}]: {e}")
        return None

    if not candles:
        return None

    # Find the candle whose timestamp is closest to (and ≤) target
    best = None
    for c in candles:
        c_time = c["date"]
        if hasattr(c_time, "tzinfo") and c_time.tzinfo is None:
            c_time = IST.localize(c_time)
        elif hasattr(c_time, "tzinfo") and c_time.tzinfo is not None:
            c_time = c_time.astimezone(IST)
        if c_time <= target_dt_ist:
            best = c
    return best["close"] if best else candles[-1]["close"]


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
print("Fetching NFO instruments...")
instruments = kite.instruments("NFO")
print(f"  {len(instruments)} instruments loaded\n")

print("=" * 70)
print(f"{'Date':<12} {'Strike':>7} {'Type':<5} {'Entry':>7} {'Exit':>7} {'P&L/lot':>9} {'Symbol'}")
print("=" * 70)

grand_pnl = 0.0

for entry in ENTRIES:
    d = entry["date"]
    strike = entry["strike"]

    # We use the entry prices we already have from logs at 9:20 AM
    entry_ce = entry["entry_ce"]
    entry_pe = entry["entry_pe"]

    # Exit time: 3:15 PM IST on the same day
    exit_time = IST.localize(datetime(d.year, d.month, d.day, 15, 15, 0))

    # Resolve tokens
    ce_token, ce_sym = resolve_token(instruments, strike, "CE", d)
    pe_token, pe_sym = resolve_token(instruments, strike, "PE", d)

    if not ce_token or not pe_token:
        print(f"{d}  Strike={strike}  — could not resolve tokens (expired?)")
        continue

    print(f"\n{d}  Strike={strike}  [{ce_sym}]  [{pe_sym}]")

    # Fetch exit prices at 3:15 PM
    exit_ce = price_at(ce_token, exit_time)
    exit_pe = price_at(pe_token, exit_time)

    if exit_ce is None or exit_pe is None:
        print(f"  Could not fetch exit prices for {d} — skipping")
        continue

    pnl_ce = (entry_ce - exit_ce) * NIFTY_LOT_SIZE
    pnl_pe = (entry_pe - exit_pe) * NIFTY_LOT_SIZE
    total_pnl = pnl_ce + pnl_pe
    grand_pnl += total_pnl

    print(f"  {'CE':<5}  entry={entry_ce:>7.2f}  exit={exit_ce:>7.2f}  P&L={pnl_ce:>+8.0f}")
    print(f"  {'PE':<5}  entry={entry_pe:>7.2f}  exit={exit_pe:>7.2f}  P&L={pnl_pe:>+8.0f}")
    print(f"  {'TOTAL':<5}  {'':>7}  {'':>7}  P&L={total_pnl:>+8.0f}  (1 lot = {NIFTY_LOT_SIZE} units)")

print("\n" + "=" * 70)
print(f"  Grand total P&L (3 days, 1 lot each): ₹{grand_pnl:+.0f}")
print("=" * 70)
