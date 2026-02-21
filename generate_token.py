"""Helper script to generate Kite Connect access token.

Usage:
    1. Open the login URL printed by this script in your browser
    2. Log in with your Zerodha credentials
    3. Copy the 'request_token' from the redirected URL
    4. Paste it when prompted
    5. The access token will be saved to your .env file
"""

import os
import re
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")

if not API_KEY or not API_SECRET:
    print("ERROR: Set KITE_API_KEY and KITE_API_SECRET in your .env file first")
    exit(1)

kite = KiteConnect(api_key=API_KEY)

print("\n" + "=" * 50)
print("  KITE ACCESS TOKEN GENERATOR")
print("=" * 50)
print(f"\nStep 1: Open this URL in your browser:\n")
print(f"  {kite.login_url()}\n")
print("Step 2: Log in and authorize the app")
print("Step 3: You'll be redirected to a URL like:")
print("  https://127.0.0.1?request_token=XXXXX&action=login")
print("  (The page won't load — that's fine)\n")

request_token = input("Paste the request_token from the URL: ").strip()

if not request_token:
    print("ERROR: No request token provided")
    exit(1)

try:
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]

    print(f"\nAccess Token: {access_token}")

    # Update .env file
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            content = f.read()

        if "KITE_ACCESS_TOKEN=" in content:
            content = re.sub(
                r"KITE_ACCESS_TOKEN=.*",
                f"KITE_ACCESS_TOKEN={access_token}",
                content,
            )
        else:
            content += f"\nKITE_ACCESS_TOKEN={access_token}\n"

        with open(env_path, "w") as f:
            f.write(content)

        print("\n.env file updated with new access token!")
    else:
        print("\nNo .env file found. Add this to your .env manually:")
        print(f"  KITE_ACCESS_TOKEN={access_token}")

    print("\nYou can now run: python main.py")
    print("Note: This token expires daily. Run this script again tomorrow morning.")

except Exception as e:
    print(f"\nERROR: {e}")
    print("Make sure the request_token is correct and hasn't expired.")
