"""Find correct instrument symbols on Kite Connect."""

import os
from dotenv import load_dotenv
from kiteconnect import KiteConnect

load_dotenv()

kite = KiteConnect(api_key=os.getenv("KITE_API_KEY"))
kite.set_access_token(os.getenv("KITE_ACCESS_TOKEN"))

print("Searching for NIFTY futures...")
nse_instruments = kite.instruments("NFO")
for inst in nse_instruments:
    if "NIFTY" in inst["tradingsymbol"] and "FUT" in inst["tradingsymbol"]:
        if "BANK" not in inst["tradingsymbol"] and "FIN" not in inst["tradingsymbol"]:
            print(f"  {inst['exchange']}:{inst['tradingsymbol']} | token={inst['instrument_token']} | expiry={inst.get('expiry')}")

print("\nSearching for GOLD futures...")
mcx_instruments = kite.instruments("MCX")
for inst in mcx_instruments:
    if "GOLD" in inst["tradingsymbol"] and "FUT" in inst["segment"]:
        print(f"  {inst['exchange']}:{inst['tradingsymbol']} | token={inst['instrument_token']} | expiry={inst.get('expiry')}")
