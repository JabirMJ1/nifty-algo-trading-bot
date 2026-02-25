"""Auto-refresh Kite access token using stored credentials + pyotp.

If KITE_USER_ID, KITE_PASSWORD, and KITE_TOTP_SECRET are set in .env,
this module logs into Zerodha programmatically on startup so no manual
token refresh is required each morning.
"""

import re
import requests
import pyotp
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from kiteconnect import KiteConnect

from core.config import settings
from core.logger import get_logger

log = get_logger("auto_token")


def refresh_kite_token() -> str | None:
    """
    Log into Zerodha programmatically and return a fresh access_token.
    Returns None if credentials are not configured or login fails.
    """
    if not all([settings.KITE_USER_ID, settings.KITE_PASSWORD, settings.KITE_TOTP_SECRET]):
        log.info("Auto token: credentials not configured, skipping.")
        return None

    log.info("Auto token: refreshing Kite access token...")
    session = requests.Session()

    # Step 1: Password login
    r = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": settings.KITE_USER_ID, "password": settings.KITE_PASSWORD},
    )
    r.raise_for_status()
    resp = r.json()
    if resp.get("status") != "success":
        raise RuntimeError(f"Zerodha login failed: {resp.get('message')}")
    request_id = resp["data"]["request_id"]

    # Step 2: TOTP — use the twofa_type returned by the login response
    twofa_type = resp["data"].get("twofa_type", "app_code")
    totp_code = pyotp.TOTP(settings.KITE_TOTP_SECRET).now()
    r = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id": settings.KITE_USER_ID,
            "request_id": request_id,
            "twofa_value": totp_code,
            "twofa_type": twofa_type,
        },
    )
    r.raise_for_status()

    # Step 3: Walk the redirect chain manually, stop when we see request_token
    # Chain: kite.trade → kite.zerodha.com/connect/login → /connect/finish → redirect_url?request_token=...
    # The final redirect goes to the app's registered redirect URL (may be localhost) — never follow it.
    next_url = f"https://kite.trade/connect/login?api_key={settings.KITE_API_KEY}&v=3"
    request_token = None
    for _ in range(10):
        r = session.get(next_url, allow_redirects=False)
        location = r.headers.get("Location", "")
        params = parse_qs(urlparse(location).query)
        if "request_token" in params:
            request_token = params["request_token"][0]
            break
        if location:
            next_url = location
        else:
            break
    if not request_token:
        raise RuntimeError("No request_token found after following redirects.")

    # Step 4: Exchange request_token for access_token
    kite = KiteConnect(api_key=settings.KITE_API_KEY)
    data = kite.generate_session(request_token, api_secret=settings.KITE_API_SECRET)
    access_token = data["access_token"]

    # Step 5: Persist to .env so restarts within the day reuse it
    env_path = Path(".env")
    if env_path.exists():
        content = env_path.read_text()
        if "KITE_ACCESS_TOKEN=" in content:
            content = re.sub(
                r"KITE_ACCESS_TOKEN=.*",
                f"KITE_ACCESS_TOKEN={access_token}",
                content,
            )
        else:
            content += f"\nKITE_ACCESS_TOKEN={access_token}\n"
        env_path.write_text(content)

    log.info("Auto token: Kite access token refreshed successfully.")
    return access_token
