#!/usr/bin/env python3
"""AI Trader - Test Connectivity Script
Sends a test email and verifies API connectivity without trading."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_trader import send_email, fetch_candles, BOT_SOURCE, _load_dotenv
from datetime import datetime, timezone

_load_dotenv()

print("=" * 60)
print("AI TRADER — CONNECTIVITY TEST")
print("=" * 60)

# Test 1: Revolut X API
print("\n[1/2] Revolut X API...")
candles = fetch_candles("BTC-EUR", 60, 3)
if candles:
    print(f"   PASS: {len(candles)} BTC-EUR candles fetched, latest close: {float(candles[-1]['close']):.2f}")
else:
    print("   FAIL: No candles returned")

# Test 2: Email notification
print("\n[2/2] Sending test email...")
result = send_email(
    f"TEST: [{BOT_SOURCE}] Connectivity Test",
    f"""AI Trader Connectivity Test

Revolut X API: Connected
Bot source: {BOT_SOURCE}
Python: {sys.version.split()[0]}
Time: {datetime.now(timezone.utc).isoformat()}

If you received this, everything is working.
"""
)
if result:
    print("   PASS: Email sent successfully")
else:
    print("   FAIL: Email could not be sent")

print(f"\nTest complete — source: {BOT_SOURCE}")
