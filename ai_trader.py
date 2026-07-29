#!/usr/bin/env python3
"""
AI Day Trader - LLM-Powered Crypto Trading Bot
Uses Claude to analyze market data and make intelligent trading decisions.
No hardcoded rules — pure AI discretion.
"""
import os
import sys
import time
import json
import base64
import smtplib
import requests
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from cryptography.hazmat.primitives import serialization

# ── Load .env file if present (no dependency needed) ─────────────────────────
def _load_dotenv():
    """Load KEY=VALUE pairs from .env into os.environ (without overwriting existing)."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

_load_dotenv()

# Fix Windows console encoding
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

# ── Configuration ──────────────────────────────────────────────────────────────
API_KEY = os.environ.get("REVOLUT_X_API_KEY", "KavAnfSVkBrxYPixZVItrWOMvDTzeHtY20pjw2KSHUxZONX3HB6G7t4s8paQBJNH")
PRIVATE_KEY_PATH = os.environ.get("REVOLUT_X_PRIVATE_KEY_PATH", os.path.expanduser("~/.config/revolut-x/private.pem"))
BASE_URL = "https://revx.revolut.com"
PORTFOLIO_FILE = os.environ.get("PORTFOLIO_FILE", os.path.join(os.path.expanduser("~"), "ai_trader_portfolio.json"))
DECISION_LOG = os.environ.get("DECISION_LOG", os.path.join(os.path.expanduser("~"), "ai_trader_decisions.jsonl"))

# LLM API — defaults to Groq (free tier, Llama 3.3 70B)
# Supported providers: Groq, Cerebras, OpenRouter, Anthropic, or any OpenAI-compatible
# Set ANTHROPIC_BASE_URL and LLM_MODEL env vars to switch providers
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://openrouter.ai/api/v1")
BOT_SOURCE = os.environ.get("BOT_SOURCE", "local")  # "local" (PC) or "cloud" (GitHub Actions)

# Local uses the best free model available
_DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
LLM_MODEL = os.environ.get("LLM_MODEL", _DEFAULT_MODEL)
LLM_MAX_TOKENS = 16000
# Detect API format: "openai" for Groq/Cerebras/OpenRouter, "anthropic" for native Anthropic
LLM_API_FORMAT = os.environ.get("LLM_API_FORMAT", "openai" if "/openai" in ANTHROPIC_BASE_URL or "/v1" in ANTHROPIC_BASE_URL else "anthropic")

# Model fallback chain — try each in order
LLM_MODEL_CHAIN = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",   # 550B — best free general reasoning
    "inclusionai/ling-3.0-flash:free",           # Flash tier — fast fallback
]

# Email (Gmail SMTP)
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "alexgamingmax11@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

# Trading
SYMBOLS = ["BTC-EUR", "ETH-EUR", "SOL-EUR"]
CANDLE_CONFIGS = [
    {"interval": 15,   "limit": 48, "label": "15m"},   # 12h of 15-min (trend + momentum)
    {"interval": 5,    "limit": 12, "label": "5m"},    # 1h of 5-min (entry patterns)
    {"interval": 1,    "limit": 6,  "label": "1m"},    # 6m of 1-min (timing)
]
CHECK_INTERVAL = 300      # 5 minutes between checks (responsive to intraday moves)
OPPORTUNITY_INTERVAL = 60 # 60s fast-poll when a dip is detected
POSITION_SIZE_PCT = 0.25  # Max 25% per position
MAX_EXPOSURE_PCT = 0.75   # Max 75% total deployed
TRADING_FEE_PCT = 0.001   # 0.1% taker fee (Revolut X standard for crypto)
STARTING_CAPITAL = float(os.environ.get("STARTING_CAPITAL", "500"))  # Default €500

# ── Revolut X API ──────────────────────────────────────────────────────────────

def sign_request(method, path, query_string=""):
    """Create Ed25519 signature for Revolut X API"""
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method}{path}{query_string}"

    with open(PRIVATE_KEY_PATH, "rb") as f:
        private_key_bytes = f.read()

    private_key = serialization.load_pem_private_key(private_key_bytes, password=None)
    signature = private_key.sign(message.encode('utf-8'))
    signature_b64 = base64.b64encode(signature).decode('utf-8')

    return {
        "Accept": "application/json",
        "X-Revx-API-Key": API_KEY,
        "X-Revx-Timestamp": timestamp,
        "X-Revx-Signature": signature_b64
    }


def fetch_candles(symbol, interval, limit):
    """Fetch OHLCV candles from Revolut X"""
    path = f"/api/1.0/candles/{symbol}"
    query_string = f"interval={interval}&limit={limit}"
    headers = sign_request("GET", path, query_string)
    url = f"{BASE_URL}{path}?{query_string}"

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get("data", []) if isinstance(data, dict) else data
        else:
            print(f"⚠️ Candles fetch failed ({symbol}): {response.status_code} {response.text[:200]}")
            return None
    except Exception as e:
        print(f"⚠️ Error fetching candles ({symbol}): {e}")
        return None


# ── Market Data Formatting ─────────────────────────────────────────────────────

def format_candles_for_llm(candles, symbol, label):
    """Format candle data into a concise table for Claude"""
    if not candles:
        return f"{symbol} ({label}): No data available"

    # Show appropriate number of candles per timeframe
    max_per_tf = {"15m": 12, "5m": 8, "1m": 4}
    limit = max_per_tf.get(label, 12)
    rows = []
    for c in candles[-limit:]:
        ts = c.get('start', c.get('timestamp', c.get('time', '')))
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime('%m-%d %H:%M')
        o, h, l, cl = float(c['open']), float(c['high']), float(c['low']), float(c['close'])
        v = float(c.get('volume', 0))
        rows.append(
            f"  {ts} | O:{o:>10.2f} H:{h:>10.2f} "
            f"L:{l:>10.2f} C:{cl:>10.2f} V:{v:>12.2f}"
        )

    header = f"{symbol} ({label} candles, most recent last):"
    return header + "\n" + "\n".join(rows)


def get_market_summary(candles, label):
    """Quick numeric summary — adapts to candle timeframe"""
    if not candles or len(candles) < 2:
        return {}

    closes = [float(c['close']) for c in candles]
    highs = [float(c['high']) for c in candles]
    lows  = [float(c['low'])  for c in candles]
    volumes = [float(c.get('volume', 0)) for c in candles]

    current = closes[-1]

    # Candle-count lookbacks that map to ~real-time windows
    lookbacks = {
        "15m": {"recent": 4, "medium": 16, "full": None},   # 1h / 4h / all
        "5m":  {"recent": 12, "medium": 48, "full": None},   # 1h / 4h / all
        "1m":  {"recent": 15, "medium": 60, "full": None},   # 15m / 1h / all
    }
    lb = lookbacks.get(label, {"recent": 2, "medium": 5, "full": None})

    def pct_change(back):
        return round(((closes[-1] / closes[-back]) - 1) * 100, 2) if len(closes) > back else None

    # RSI (14-period)
    rsi = None
    if len(closes) >= 15:
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [max(d, 0) for d in deltas[-14:]]
        losses = [max(-d, 0) for d in deltas[-14:]]
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        if avg_loss > 0:
            rsi = round(100 - (100 / (1 + avg_gain / avg_loss)), 1)

    vol_window = min(len(volumes), 24)
    avg_vol = sum(volumes[-vol_window:]) / vol_window if vol_window else 0
    vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

    # Bollinger Bands (20-period, 2 standard deviations)
    bb_period = 20
    bb_upper = bb_middle = bb_lower = bb_pct_b = bb_width = None
    if len(closes) >= bb_period:
        bb_window = closes[-bb_period:]
        bb_middle = sum(bb_window) / bb_period
        variance = sum((x - bb_middle) ** 2 for x in bb_window) / bb_period
        bb_std = variance ** 0.5
        bb_upper = bb_middle + 2 * bb_std
        bb_lower = bb_middle - 2 * bb_std
        bb_pct_b = round((current - bb_lower) / (bb_upper - bb_lower), 3) if bb_upper != bb_lower else 0.5
        bb_width = round((bb_upper - bb_lower) / bb_middle * 100, 2) if bb_middle else 0

    full_highs = highs if lb["full"] is None else highs[-lb["full"]:]
    full_lows  = lows  if lb["full"] is None else lows[-lb["full"]:]

    return {
        "current_price": current,
        "recent_pct":   pct_change(lb["recent"]),
        "medium_pct":   pct_change(lb["medium"]),
        "full_range_high": max(full_highs),
        "full_range_low":  min(full_lows),
        "full_range_pct":  round(((max(full_highs) - min(full_lows)) / min(full_lows)) * 100, 2) if min(full_lows) > 0 else 0,
        "rsi_14": rsi,
        "volume_ratio": vol_ratio,
        "bb_upper": round(bb_upper, 2) if bb_upper else None,
        "bb_middle": round(bb_middle, 2) if bb_middle else None,
        "bb_lower": round(bb_lower, 2) if bb_lower else None,
        "bb_pct_b": bb_pct_b,
        "bb_width": bb_width,
    }


# ── Opportunity Detector ──────────────────────────────────────────────────────

def detect_dip_opportunity(market_data):
    """Check if any symbol is near the lower Bollinger Band (opportunity zone).
    Returns True if we should fast-poll on the next cycle.
    """
    for symbol, tf_data in market_data.items():
        candles = tf_data.get("5m")
        if not candles or len(candles) < 20:
            continue
        summary = get_market_summary(candles, "5m")
        if summary:
            bb_pct_b = summary.get("bb_pct_b")
            if bb_pct_b is not None and bb_pct_b < 0.15:
                print(f"   🔍 {symbol} 5m BB%b={bb_pct_b:.2f} — dip detected, staying alert")
                return True
    return False


# ── LLM Trading Decision ──────────────────────────────────────────────────────

def get_llm_decision(market_data_list, portfolio, trades_today=0):
    """
    Ask Claude to analyze multi-timeframe market data and decide on a trade.
    No rules, no thresholds — pure AI judgment.
    """
    if not ANTHROPIC_API_KEY:
        print("⚠️ ANTHROPIC_API_KEY not set — cannot make AI decisions")
        return None

    current_exposure = sum(p['value_eur'] for p in portfolio['positions'])
    total_value = portfolio['current_cash_eur'] + current_exposure

    portfolio_text = f"""Portfolio State:
  Cash: EUR {portfolio['current_cash_eur']:.2f}
  Starting Capital: EUR {portfolio['starting_capital_eur']:.2f}
  Total Value: EUR {total_value:.2f}
  Open Positions: {len(portfolio['positions'])}
  Total P&L: EUR {portfolio['total_pnl_eur']:+.2f} ({portfolio['total_pnl_percent']:+.2f}%)
  Fees Paid: EUR {portfolio.get('total_fees_paid_eur', 0):.2f}
  Win Rate: {portfolio['win_rate']:.1f}% ({portfolio['total_trades']} trades total)"""

    if portfolio['positions']:
        portfolio_text += "\n  Open positions:"
        for p in portfolio['positions']:
            pnl = ((p.get('current_price', p['entry_price']) / p['entry_price']) - 1) * 100
            portfolio_text += f"\n    - {p['symbol']}: EUR {p['value_eur']:.2f} @ EUR {p['entry_price']:.2f} ({pnl:+.1f}%)"

    # Build multi-timeframe data: summaries for higher TF, raw candles for lower TFs
    market_text_parts = []
    for symbol, tf_data in market_data_list.items():
        market_text_parts.append(f"\n{'='*40}\n{symbol}:")

        # 15m: summary only (trend + momentum context)
        candles_15m = tf_data.get("15m")
        if candles_15m:
            summary = get_market_summary(candles_15m, "15m")
            if summary:
                bb_text = ""
                if summary.get('bb_pct_b') is not None:
                    bb_text = f", BB%b={summary['bb_pct_b']:.2f}"
                market_text_parts.append(
                    f"  15m summary: price={summary['current_price']:.2f}, "
                    f"recent={summary.get('recent_pct', 0):+.2f}%, "
                    f"medium={summary.get('medium_pct', 0):+.2f}%, "
                    f"range={summary.get('full_range_pct', 0):.2f}%, "
                    f"RSI={summary.get('rsi_14', '?')}, "
                    f"vol={summary.get('volume_ratio', '?')}x"
                    f"{bb_text}"
                )

        # 5m and 1m: raw candles for precise entry analysis
        for label in ["5m", "1m"]:
            candles = tf_data.get(label)
            if candles:
                market_text_parts.append(format_candles_for_llm(candles, symbol, label))

    market_text = "\n".join(market_text_parts) if market_text_parts else "No market data available"

    prompt = f"""You are an experienced cryptocurrency day trader. Analyze the MULTI-TIMEFRAME data below and decide.

TIMEFRAMES (use all of them):
- 15m: trend direction, momentum, support/resistance, entry zones (summary only)
- 5m: recent candles — look for patterns, consolidation, breakouts, reversals
- 1m: latest candles — pinpoint entry timing, immediate momentum

{portfolio_text}

MAX POSITION SIZE: EUR {portfolio['starting_capital_eur'] * POSITION_SIZE_PCT:.2f}
MAX TOTAL EXPOSURE: {MAX_EXPOSURE_PCT * 100:.0f}%

MARKET DATA (multi-timeframe):
{market_text}

Current UTC time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

Analyze ALL timeframes together. Look for:
- BB bounce: price hits BB_lower (%b near 0), next candle shows recovery — buy the first pullback-to-band
- Reversal: 5m/1m showing bullish structure (higher lows, momentum shift) against 15m oversold
- Pullback entry: uptrend on 15m, price pulls back to 5m BB_middle or EMA — buy continuation
- Volume confirmation: increasing volume on reversal candles
- Existing positions: is the thesis still valid on the 15m trend?

Respond with ONLY a JSON object (no markdown, no code fences):
{{"action": "BUY" or "SELL" or "HOLD", "symbol": "BTC-EUR" or "ETH-EUR" or "SOL-EUR" or null, "reasoning": "your 1-2 sentence analysis referencing specific timeframes", "position_size_eur": number or null}}

Rules:
- BUY: you see a concrete entry setup with aligned timeframes
- SELL: an existing position should be closed (trend invalidated or profit target reached)
- HOLD: no compelling entry or exit — wait
- position_size_eur: only for BUY, max EUR {portfolio['starting_capital_eur'] * POSITION_SIZE_PCT:.2f}
- NOTE: Each trade costs {TRADING_FEE_PCT * 100:.1f}% taker fee each way (buy + sell = {TRADING_FEE_PCT * 200:.1f}% round-trip). Factor this into your risk/reward."""

    # Try each model in the chain — fall back on failure
    for model_name in LLM_MODEL_CHAIN:
        print(f"🧠 Trying {model_name}...")
        result = _call_llm(model_name, prompt)
        if result is not None:
            print(f"✅ {model_name} responded successfully")
            return result
        print(f"⚠️ {model_name} failed — trying next model...")

    print("⚠️ All models in chain failed")
    return None


def _call_llm(model_name, prompt):
    """Call a single LLM model. Returns parsed decision dict or None on failure."""
    text = ""
    try:
        if LLM_API_FORMAT == "openai":
            response = requests.post(
                f"{ANTHROPIC_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {ANTHROPIC_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model_name,
                    "max_tokens": LLM_MAX_TOKENS,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=180
            )

            if response.status_code != 200:
                print(f"⚠️ LLM API error ({model_name}): {response.status_code} {response.text[:200]}")
                return None

            result = response.json()
            text = result['choices'][0]['message']['content'].strip()
        else:
            response = requests.post(
                f"{ANTHROPIC_BASE_URL}/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "Authorization": f"Bearer {ANTHROPIC_API_KEY}",
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model_name,
                    "max_tokens": LLM_MAX_TOKENS,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=180
            )

            if response.status_code != 200:
                print(f"⚠️ Claude API error ({model_name}): {response.status_code} {response.text[:200]}")
                return None

            result = response.json()
            text = ""
            for block in result.get('content', []):
                if block.get('type') == 'text' and block.get('text', '').strip():
                    text = block['text'].strip()
                    break

        if not text:
            print(f"⚠️ No text content from {model_name}")
            return None

        # Parse JSON from response (handle potential markdown fences)
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        decision = json.loads(text)

        if decision.get('action') not in ('BUY', 'SELL', 'HOLD'):
            print(f"⚠️ Invalid action from {model_name}: {decision.get('action')}")
            return None

        return decision

    except json.JSONDecodeError as e:
        print(f"⚠️ Failed to parse {model_name} response as JSON: {e}")
        if text:
            print(f"   Raw response: {text[:300]}")
        return None
    except Exception as e:
        print(f"⚠️ LLM call failed ({model_name}): {e}")
        return None


# ── Portfolio Management ───────────────────────────────────────────────────────

def load_portfolio():
    """Load portfolio state from JSON"""
    try:
        with open(PORTFOLIO_FILE, 'r') as f:
            return json.load(f)
    except:
        return {
            "initialized_at": datetime.now(timezone.utc).isoformat(),
            "starting_capital_eur": STARTING_CAPITAL,
            "current_cash_eur": STARTING_CAPITAL,
            "positions": [],
            "closed_trades": [],
            "total_trades": 0,
            "win_rate": 0,
            "total_pnl_eur": 0,
            "total_pnl_percent": 0,
            "total_fees_paid_eur": 0
        }


def save_portfolio(portfolio):
    """Save portfolio state to JSON"""
    with open(PORTFOLIO_FILE, 'w') as f:
        json.dump(portfolio, f, indent=2)


def log_decision(action, symbol, reasoning, price=None):
    """Log every LLM decision for review"""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "symbol": symbol,
        "price": price,
        "source": BOT_SOURCE,
        "reasoning": reasoning
    }
    with open(DECISION_LOG, 'a') as f:
        f.write(json.dumps(entry) + '\n')


# ── Email Notifications ────────────────────────────────────────────────────────

def send_email(subject, body):
    """Send email notification via Gmail SMTP"""
    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL_ADDRESS
        msg['To'] = GMAIL_ADDRESS
        msg['Subject'] = f"[{BOT_SOURCE}] {subject}" if BOT_SOURCE and f"[{BOT_SOURCE}]" not in subject else subject
        msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)

        print(f"📧 Email sent: {subject}")
        return True
    except Exception as e:
        print(f"⚠️ Email failed: {e}")
        return False


def send_startup_email(portfolio):
    """Send startup notification"""
    tf_desc = ", ".join(f"{c['label']}({c['limit']}×)" for c in CANDLE_CONFIGS)
    subject = f"🤖 [{BOT_SOURCE}] AI Trader Started (Multi-Timeframe LLM)"
    body = f"""AI Trader is running with LLM-powered decisions.

Mode: Claude AI discretion (no hardcoded rules)
Models: {' → '.join(LLM_MODEL_CHAIN)}
Capital: EUR {portfolio['starting_capital_eur']:.2f}
Symbols: {', '.join(SYMBOLS)}
Check interval: {CHECK_INTERVAL // 60} minutes (fast-polls at 60s on dip detection)
Timeframes: {tf_desc}
No minimum trades — patient entries only

Every decision is made by Claude analyzing 15m/5m/1m candle data.
Check ai_trader_decisions.jsonl for full decision history.

Time: {datetime.now(timezone.utc).isoformat()}
"""
    send_email(subject, body)


def send_trade_email(action, symbol, price, reasoning, portfolio, pnl_eur=None, net_pnl=None, fee=None):
    """Send trade execution email"""
    if action == 'BUY':
        subject = f"🟢 [{BOT_SOURCE}] BUY: {symbol} @ EUR {price:.2f}"
    else:
        subject = f"🔴 [{BOT_SOURCE}] SELL: {symbol} @ EUR {price:.2f}"

    body = f"""AI Trader - {action} Signal
{'='*40}

Symbol: {symbol}
Price: EUR {price:.2f}
Action: {action}

AI Reasoning:
{reasoning}

"""
    if action == 'BUY':
        last_pos = portfolio['positions'][-1] if portfolio['positions'] else None
        if last_pos:
            pos_fee = last_pos.get('fee_paid', 0)
            pos_size = last_pos['value_eur']
            body += f"""Trade Details:
  Position Size: EUR {pos_size:.2f}
  Fee: EUR {pos_fee:.2f} ({pos_fee / pos_size * 100:.1f}%)

"""
    elif action == 'SELL' and net_pnl is not None:
        body += f"""Trade Details:
  Gross P&L: EUR {pnl_eur:+.2f}
  Fee: EUR {fee:.2f}
  Net P&L: EUR {net_pnl:+.2f}

"""

    body += f"""Portfolio:
  Cash: EUR {portfolio['current_cash_eur']:.2f}
  Positions: {len(portfolio['positions'])}
  Total P&L: EUR {portfolio['total_pnl_eur']:+.2f} ({portfolio['total_pnl_percent']:+.2f}%)
  Fees Paid: EUR {portfolio.get('total_fees_paid_eur', 0):.2f}
  Trades: {portfolio['total_trades']}

Time: {datetime.now(timezone.utc).isoformat()}
"""
    send_email(subject, body)


def send_status_email(portfolio, market_data):
    """Periodic status email so you know the bot is alive. market_data: symbol -> {label: candles}"""
    market_lines = []
    for symbol, tf in market_data.items():
        candles = tf.get('15m')
        if candles:
            summary = get_market_summary(candles, "15m")
            if summary:
                market_lines.append(
                    f"  {symbol}: EUR {summary['current_price']:.2f} "
                    f"| 15m: {summary.get('recent_pct', 0):+.2f}% "
                    f"| RSI: {summary.get('rsi_14', '?')}"
                )

    current_exposure = sum(p['value_eur'] for p in portfolio['positions'])
    total_value = portfolio['current_cash_eur'] + current_exposure

    subject = f"📊 [{BOT_SOURCE}] AI Trader Status | EUR {total_value:.2f}"
    body = f"""AI Trader - Hourly Status
{'='*40}

Portfolio:
  Cash: EUR {portfolio['current_cash_eur']:.2f}
  Exposure: EUR {current_exposure:.2f}
  Total Value: EUR {total_value:.2f}
  P&L: EUR {portfolio['total_pnl_eur']:+.2f} ({portfolio['total_pnl_percent']:+.2f}%)
  Fees Paid: EUR {portfolio.get('total_fees_paid_eur', 0):.2f}
  Trades: {portfolio['total_trades']} | Win Rate: {portfolio['win_rate']:.1f}%

Open Positions:
{chr(10).join(f"  - {p['symbol']}: EUR {p['value_eur']:.2f} @ EUR {p['entry_price']:.2f}" for p in portfolio['positions']) if portfolio['positions'] else "  None"}

Market:
{chr(10).join(market_lines) if market_lines else "  No data"}

Time: {datetime.now(timezone.utc).isoformat()}
Bot is running and analyzing markets every {CHECK_INTERVAL // 60} minutes (fast-polls at 60s on dip).
"""
    send_email(subject, body)


# ── Trade Execution ────────────────────────────────────────────────────────────

def execute_buy(symbol, price, reasoning, portfolio):
    """Execute a buy order — deducts taker fee from cash"""
    max_position = portfolio['starting_capital_eur'] * POSITION_SIZE_PCT
    position_value = min(max_position, portfolio['current_cash_eur'])
    fee = round(position_value * TRADING_FEE_PCT, 2)
    total_cost = position_value + fee

    if position_value < 100:
        print(f"⚠️ Insufficient cash (EUR {portfolio['current_cash_eur']:.2f})")
        return False

    if total_cost > portfolio['current_cash_eur']:
        print(f"⚠️ Cannot afford position + fee (need EUR {total_cost:.2f}, have EUR {portfolio['current_cash_eur']:.2f})")
        return False

    # Check if already holding this symbol — if so, skip (single position per symbol)
    existing = next((p for p in portfolio['positions'] if p['symbol'] == symbol), None)
    if existing:
        print(f"⚠️ Already holding {symbol} — skipping second position")
        return False

    position = {
        'symbol': symbol,
        'entry_time': datetime.now(timezone.utc).isoformat(),
        'entry_price': price,
        'current_price': price,
        'value_eur': position_value,
        'fee_paid': fee,
    }

    portfolio['positions'].append(position)
    portfolio['current_cash_eur'] -= total_cost  # position + fee
    portfolio['total_trades'] += 1
    portfolio['total_fees_paid_eur'] = portfolio.get('total_fees_paid_eur', 0) + fee

    print(f"\n🟢 BUY {symbol} @ EUR {price:.2f} | Size: EUR {position_value:.2f} | Fee: EUR {fee:.2f}")
    print(f"   Reasoning: {reasoning}")

    send_trade_email('BUY', symbol, price, reasoning, portfolio)
    log_decision('BUY', symbol, reasoning, price)
    save_portfolio(portfolio)
    return True


def execute_sell(symbol, price, reasoning, portfolio):
    """Execute a sell order — deducts taker fee, reports net P&L"""
    position = next((p for p in portfolio['positions'] if p['symbol'] == symbol), None)
    if not position:
        print(f"⚠️ No position found for {symbol}")
        return False

    entry_price = position['entry_price']
    buy_fee = position.get('fee_paid', 0)
    exit_value = position['value_eur'] * (price / entry_price)
    sell_fee = round(exit_value * TRADING_FEE_PCT, 2)
    cash_returned = exit_value - sell_fee

    # Gross P&L (price movement only)
    pnl_eur = round(exit_value - position['value_eur'], 2)
    pnl_pct = round(((price / entry_price) - 1) * 100, 2)

    # Net P&L (after both buy and sell fees)
    net_pnl = round(pnl_eur - buy_fee - sell_fee, 2)
    total_fee = round(buy_fee + sell_fee, 2)

    portfolio['positions'].remove(position)
    portfolio['current_cash_eur'] += cash_returned
    portfolio['total_pnl_eur'] += net_pnl  # track net P&L
    portfolio['total_fees_paid_eur'] = portfolio.get('total_fees_paid_eur', 0) + sell_fee

    portfolio['closed_trades'].append({
        'symbol': symbol,
        'entry_time': position['entry_time'],
        'exit_time': datetime.now(timezone.utc).isoformat(),
        'entry_price': entry_price,
        'exit_price': price,
        'pnl_eur': pnl_eur,
        'pnl_pct': pnl_pct,
        'fee_paid': total_fee,
        'net_pnl_eur': net_pnl,
        'reasoning': reasoning
    })

    wins = sum(1 for t in portfolio['closed_trades'] if t.get('net_pnl_eur', t['pnl_eur']) > 0)
    portfolio['win_rate'] = wins / len(portfolio['closed_trades']) * 100
    total_value = portfolio['current_cash_eur']
    portfolio['total_pnl_percent'] = round(((total_value / portfolio['starting_capital_eur']) - 1) * 100, 2)

    emoji = "💰" if net_pnl > 0 else "❌"
    print(f"\n{emoji} SELL {symbol} @ EUR {price:.2f}")
    print(f"   Gross P&L: EUR {pnl_eur:+.2f} ({pnl_pct:+.2f}%) | Fee: EUR {total_fee:.2f} | Net: EUR {net_pnl:+.2f}")
    print(f"   Reasoning: {reasoning}")

    send_trade_email('SELL', symbol, price, reasoning, portfolio, pnl_eur=pnl_eur, net_pnl=net_pnl, fee=total_fee)
    log_decision('SELL', symbol, reasoning, price)
    save_portfolio(portfolio)
    return True


# ── Display ────────────────────────────────────────────────────────────────────

def print_status(portfolio, market_data):
    """Print current status. market_data is dict: symbol -> {label: candles}"""
    total_exposure = sum(p['value_eur'] for p in portfolio['positions'])
    total_value = portfolio['current_cash_eur'] + total_exposure

    print(f"\n{'='*70}")
    print(f"🤖 AI TRADER (LLM Mode) — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*70}")
    print(f"💰 Cash: EUR {portfolio['current_cash_eur']:.2f} | Total: EUR {total_value:.2f}")

    if portfolio['positions']:
        print(f"\n📊 Positions:")
        for pos in portfolio['positions']:
            tf = market_data.get(pos['symbol'], {})
            candles = tf.get('5m') or tf.get('15m')
            if candles:
                current = float(candles[-1]['close'])
                pnl = ((current / pos['entry_price']) - 1) * 100
                pos['current_price'] = current
                emoji = "📈" if pnl > 0 else "📉"
                print(f"   {emoji} {pos['symbol']}: EUR {pos['value_eur']:.2f} @ EUR {pos['entry_price']:.2f} -> EUR {current:.2f} ({pnl:+.2f}%)")
    else:
        print(f"\n📊 No open positions")

    print(f"\n📈 Market:")
    for symbol, tf in market_data.items():
        candles = tf.get('15m')
        if candles:
            s = get_market_summary(candles, "15m")
            if s:
                print(f"   {symbol}: EUR {s['current_price']:.2f} | 15m: {s.get('recent_pct', 0):+.2f}% | RSI: {s.get('rsi_14', '?')}")

    print(f"\n📊 Stats: {portfolio['total_trades']} trades | Win Rate: {portfolio['win_rate']:.1f}% | P&L: EUR {portfolio['total_pnl_eur']:+.2f} | Fees: EUR {portfolio.get('total_fees_paid_eur', 0):.2f}")


# ── Main Loop ──────────────────────────────────────────────────────────────────

def run_cycle(portfolio, trades_today):
    """Run a single trading cycle. Returns (trades_today, market_data)."""
    # Reset daily trade counter at midnight UTC
    today = datetime.now(timezone.utc).date()

    # Fetch multi-timeframe market data
    market_data = {}
    for symbol in SYMBOLS:
        market_data[symbol] = {}
        for cfg in CANDLE_CONFIGS:
            candles = fetch_candles(symbol, cfg["interval"], cfg["limit"])
            market_data[symbol][cfg["label"]] = candles

    # Print status
    print_status(portfolio, market_data)

    # Update current prices on open positions
    for pos in portfolio['positions']:
        tf = market_data.get(pos['symbol'], {})
        candles = tf.get('1m') or tf.get('5m')
        if candles:
            pos['current_price'] = float(candles[-1]['close'])

    # Ask Claude for a decision
    print(f"\n🧠 Asking LLM for analysis... (trades today: {trades_today})")
    decision = get_llm_decision(market_data, portfolio, trades_today=trades_today)

    if decision:
        action = decision['action']
        symbol = decision.get('symbol')
        reasoning = decision.get('reasoning', 'No reasoning provided')

        print(f"\n🧠 AI Decision: {action} {symbol or ''}")
        print(f"   Reasoning: {reasoning}")

        price = None
        if symbol:
            tf = market_data.get(symbol, {})
            candles = tf.get('1m') or tf.get('5m')
            if candles:
                price = float(candles[-1]['close'])

        if action == 'BUY' and symbol and price:
            execute_buy(symbol, price, reasoning, portfolio)
            trades_today += 1
        elif action == 'SELL' and symbol and price:
            execute_sell(symbol, price, reasoning, portfolio)
            trades_today += 1
        elif action == 'HOLD':
            print(f"   Holding — {reasoning}")
            log_decision(action, symbol, reasoning, price)
    else:
        print("⚠️ Could not get AI decision — will retry next cycle")

    return trades_today, market_data


def main():
    run_once = os.environ.get("RUN_ONCE", "").lower() in ("1", "true", "yes")

    print("🤖 AI TRADER — Starting (LLM-Powered, Multi-Timeframe)")
    print(f"📅 {datetime.now(timezone.utc).isoformat()}")
    print(f"🧠 Models: {' → '.join(LLM_MODEL_CHAIN)}")
    print(f"💰 Capital: EUR {STARTING_CAPITAL:,.0f}")
    print(f"📧 Alerts: {GMAIL_ADDRESS}")
    print(f"⏱️ Check interval: {CHECK_INTERVAL // 60} min (fast-poll: {OPPORTUNITY_INTERVAL}s on dip)")
    print(f"📊 Timeframes: 15m (summary), 5m+1m (raw candles)")
    print(f"🎯 Decision maker: Claude (no hardcoded rules)")
    print(f"💰 Fee: {TRADING_FEE_PCT * 100:.1f}% taker per trade ({TRADING_FEE_PCT * 200:.1f}% round-trip)")
    print(f"🆔 Source: {BOT_SOURCE}")
    print(f"📈 Min trades: none — patient entries only")
    if run_once:
        print(f"☁️ Cloud mode: RUN_ONCE enabled — will exit after one cycle\n")
    else:
        print()

    if not ANTHROPIC_API_KEY:
        print("⚠️ WARNING: ANTHROPIC_API_KEY not set!")
        print("   Set it as an environment variable or in the script.")
        print("   The bot will run but cannot make trading decisions.\n")

    portfolio = load_portfolio()

    if run_once:
        # Cloud mode: single cycle, no email, no loop
        trades_today = 0
        today = datetime.now(timezone.utc).date()
        # Count today's trades from decision log
        try:
            with open(DECISION_LOG, 'r') as f:
                for line in f:
                    entry = json.loads(line.strip())
                    if entry.get('timestamp', '').startswith(today.isoformat()):
                        trades_today += 1
        except FileNotFoundError:
            pass

        trades_today, _ = run_cycle(portfolio, trades_today)
        save_portfolio(portfolio)
        print(f"\n✅ Cycle complete — exiting (cloud mode)")
        return

    # Local mode: continuous loop
    send_startup_email(portfolio)
    last_status_email = time.time()
    trades_today = 0
    current_day = datetime.now(timezone.utc).date()
    market_data = {}

    while True:
        try:
            today = datetime.now(timezone.utc).date()
            if today != current_day:
                trades_today = 0
                current_day = today
                print(f"\n📅 New day: {today} — trade counter reset to 0")

            trades_today, market_data = run_cycle(portfolio, trades_today)

            if time.time() - last_status_email >= 3600:
                send_status_email(portfolio, market_data)
                last_status_email = time.time()

            # Check for dip opportunity — if price is near lower BB, fast-poll
            if detect_dip_opportunity(market_data):
                print(f"   ⚡ Dip opportunity detected — next check in {OPPORTUNITY_INTERVAL}s")
                time.sleep(OPPORTUNITY_INTERVAL)
            else:
                print(f"\n⏳ Next scan in {CHECK_INTERVAL // 60} min... (trades today: {trades_today})")
                time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n🛑 Trader stopped by user")
            send_email("🛑 AI Trader Stopped", "The AI Trader has been stopped.")
            break
        except Exception as e:
            print(f"\n⚠️ Error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    main()
