#!/usr/bin/env python3
"""Smoke test for the deterministic risk overlay — no network, no email, no file writes.
Monkeypatches side-effecting functions, then exercises sizing, guards and exit paths."""
import copy
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai_trader as t

# Neutralize side effects (email / log) for the whole test; saves are recorded
# (deep copies) so we can assert exactly what got persisted, and when.
t.send_email = lambda *a, **k: True
t.send_trade_email = lambda *a, **k: True
t.send_hold_email = lambda *a, **k: True
t.send_failure_email = lambda *a, **k: True
t.log_decision = lambda *a, **k: None
SAVED = []
t.save_portfolio = lambda p: SAVED.append(copy.deepcopy(p))

FAIL = []

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        FAIL.append(name)

def fresh_portfolio(cash=500.0):
    return {
        'starting_capital_eur': 500.0,
        'current_cash_eur': cash,
        'positions': [],
        'total_trades': 0,
        'win_rate': 0.0,
        'total_pnl_eur': 0.0,
        'total_fees_paid_eur': 0.0,
        'closed_trades': [],
    }

def md(price, symbol="BTC-EUR"):
    return {symbol: {'5m': [{'close': str(price)}]}}

# ── 1. execute_buy sizing ──────────────────────────────────────────────
p = fresh_portfolio()
ok = t.execute_buy("BTC-EUR", 100.0, "test sizing", p)
pos = p['positions'][0] if p['positions'] else None
check("buy executes", ok and pos is not None)
# Expected: 1% of 500 = 5 EUR risk / 3.5% stop = 142.86, under 30% cap (150) and cash
check("buy size = risk-based", pos and abs(pos['value_eur'] - 142.86) < 0.01,
      f"got {pos['value_eur'] if pos else None}, want ~142.86")
check("buy fee deducted", abs(p['current_cash_eur'] - (500 - 142.86 - 0.14)) < 0.01,
      f"cash {p['current_cash_eur']:.2f}")
check("buy sets overlay fields", pos and pos['peak_price'] == 100.0 and pos['tp1_done'] is False
      and pos['invalidation'] == 96.5, f"inv={pos['invalidation'] if pos else None}")

# ETH/SOL sizing sanity
for sym, want in [("ETH-EUR", 111.11), ("SOL-EUR", 100.0)]:
    p2 = fresh_portfolio()
    t.execute_buy(sym, 100.0, "t", p2)
    got = p2['positions'][0]['value_eur'] if p2['positions'] else None
    check(f"size {sym}", got and abs(got - want) < 0.01, f"got {got}, want ~{want}")

# Duplicate symbol blocked
p3 = fresh_portfolio()
t.execute_buy("BTC-EUR", 100.0, "t", p3)
check("duplicate symbol blocked", not t.execute_buy("BTC-EUR", 100.0, "t", p3))

# MIN_TRADE_EUR floor: tiny equity → risk size < 50 but affordable → floor to 50
p4 = fresh_portfolio(cash=160.0)
ok4 = t.execute_buy("BTC-EUR", 100.0, "t", p4)
check("min-trade floor", ok4 and p4['positions'][0]['value_eur'] == 50.0,
      f"got {p4['positions'][0]['value_eur'] if p4['positions'] else None}")

# Cash check: can't afford min trade → reject
p5 = fresh_portfolio(cash=30.0)
check("broke → reject", not t.execute_buy("BTC-EUR", 100.0, "t", p5))

# ── 2. buy_guard ───────────────────────────────────────────────────────
p6 = fresh_portfolio()
t.update_circuit_breaker(p6)
check("guard: clean book allows", t.buy_guard("BTC-EUR", p6, 0) is None)
cooldown_expiry = (now_dt := datetime.now(timezone.utc)) + timedelta(hours=1)
check("guard: cooldown blocks", t.buy_guard("BTC-EUR", {**p6, 'cooldowns': {'BTC-EUR': cooldown_expiry.isoformat()}}, 0) is not None)
expired_cd = (now_dt - timedelta(hours=1)).isoformat()
check("guard: expired cooldown allows", t.buy_guard("BTC-EUR", {**p6, 'cooldowns': {'BTC-EUR': expired_cd}}, 0) is None)
check("guard: trade cap blocks", t.buy_guard("ETH-EUR", p6, t.MAX_TRADES_PER_DAY) is not None)

# circuit breaker: equity 4.9k vs day start 10k (>5% drop) → block
p7 = fresh_portfolio(cash=4900.0)
p7['cb_day_start_value'] = 10000.0
p7['cb_date'] = datetime.now(timezone.utc).date().isoformat()
check("guard: circuit breaker blocks", t.buy_guard("ETH-EUR", p7, 0) is not None)

# max positions
p8 = fresh_portfolio()
t.execute_buy("BTC-EUR", 100.0, "t", p8)
t.execute_buy("ETH-EUR", 100.0, "t", p8)
check("guard: max positions blocks 3rd", t.buy_guard("SOL-EUR", p8, 0) is not None)

# one-at-risk: existing position not yet +0.75% → block new symbol
p9 = fresh_portfolio()
t.execute_buy("BTC-EUR", 100.0, "t", p9)
p9['positions'][0]['current_price'] = 100.2  # +0.2% < +0.75%
check("guard: one-at-risk blocks", t.buy_guard("ETH-EUR", p9, 0) is not None)
p9['positions'][0]['current_price'] = 101.0  # +1.0% > +0.75%
check("guard: profitable runner allows", t.buy_guard("ETH-EUR", p9, 0) is None)

# blind-data guard: held symbol with no fresh candles this cycle — its stale
# persisted current_price would feed the circuit breaker / one-at-risk rule /
# sizing with phantom pnl. buy_guard must refuse ALL new buys until data returns.
p9b = fresh_portfolio()
t.execute_buy("BTC-EUR", 100.0, "t", p9b)
p9b['positions'][0]['current_price'] = 101.2  # stale +1.2% mark (BTC fetch failed)
check("guard: blind held position blocks new buys",
      t.buy_guard("ETH-EUR", p9b, 0, {"ETH-EUR": {'5m': [{'close': '100'}]}}) is not None)
check("guard: fresh data on held position allows",
      t.buy_guard("ETH-EUR", p9b, 0,
                  {"BTC-EUR": {'5m': [{'close': '101.2'}]},
                   "ETH-EUR": {'5m': [{'close': '100'}]}}) is None)

# ── 3. overlay exits ──────────────────────────────────────────────────
now = datetime.now(timezone.utc)

def mkpos(symbol, entry, hours_ago, **kw):
    d = {'symbol': symbol, 'entry_time': (now - timedelta(hours=hours_ago)).isoformat(),
         'entry_price': entry, 'current_price': entry, 'peak_price': entry,
         'trough_price': entry, 'value_eur': 100.0, 'fee_paid': 0.1, 'tp1_done': False}
    d.update(kw)
    return d

# hard stop: BTC down 4% (stop 3.5%)
p10 = fresh_portfolio()
p10['positions'].append(mkpos("BTC-EUR", 100.0, 1))
exits = t.apply_risk_overlay(p10, md(96.0))
check("overlay: hard stop fires", len(exits) == 1 and exits[0][1].startswith("HARD STOP"),
      f"{exits}")
check("overlay: hard stop full exit + cooldown", not p10['positions'] and 'BTC-EUR' in p10.get('cooldowns', {}))

# cooldown persistence: the full-exit path must save AFTER setting the cooldown
# (execute_sell's internal save runs before it exists). A crash between the two
# used to lose the cooldown while the SELL was durable — next run re-buys instantly.
SAVED.clear()
p10b = fresh_portfolio()
p10b['positions'].append(mkpos("BTC-EUR", 100.0, 1))
t.apply_risk_overlay(p10b, md(96.0))
check("overlay: cooldown persisted immediately on full exit",
      bool(SAVED) and 'BTC-EUR' in SAVED[-1].get('cooldowns', {}),
      f"saves={len(SAVED)}, last has cooldown={'BTC-EUR' in (SAVED[-1].get('cooldowns', {}) if SAVED else {})}")

# TP1: up 1.6% → partial, tp1_done, position stays
p11 = fresh_portfolio()
p11['positions'].append(mkpos("BTC-EUR", 100.0, 1))
exits = t.apply_risk_overlay(p11, md(101.6))
check("overlay: TP1 fires as partial", len(exits) == 1 and exits[0][1].startswith("TP1:"), f"{exits}")
check("overlay: TP1 keeps position, flags tp1_done", len(p11['positions']) == 1 and p11['positions'][0]['tp1_done'],
      f"pos={p11['positions']}")
check("overlay: TP1 no cooldown", 'cooldowns' not in p11 or 'BTC-EUR' not in p11.get('cooldowns', {}))

# trailing: after TP1, drop 1.2%+ from peak
p12 = fresh_portfolio()
p12['positions'].append(mkpos("BTC-EUR", 100.0, 1, peak_price=102.0, tp1_done=True))
exits = t.apply_risk_overlay(p12, md(100.7))  # -1.27% from peak
check("overlay: trailing stop fires post-TP1", len(exits) == 1 and not exits[0][1].startswith("TP1:"), f"{exits}")
check("overlay: trailing full exit", not p12['positions'])

# trailing should NOT fire before TP1
p13 = fresh_portfolio()
p13['positions'].append(mkpos("BTC-EUR", 100.0, 1, peak_price=101.5))
exits = t.apply_risk_overlay(p13, md(100.2))  # -1.28% from peak but tp1 not done, +0.2% pnl
check("overlay: no trailing before TP1", len(exits) == 0, f"{exits}")

# time stop: 48h+, underwater
p14 = fresh_portfolio()
p14['positions'].append(mkpos("BTC-EUR", 100.0, 49))
exits = t.apply_risk_overlay(p14, md(99.0))
check("overlay: time stop fires (48h+, <0)", len(exits) == 1 and "TIME STOP" in exits[0][1], f"{exits}")

# time stop does NOT fire when profitable
p15 = fresh_portfolio()
p15['positions'].append(mkpos("BTC-EUR", 100.0, 49))
exits = t.apply_risk_overlay(p15, md(101.0))  # +1% < TP1 1.5%, profitable → TP1? no, 1.0<1.5
check("overlay: no time stop when profitable", len(exits) == 0, f"{exits}")

# stall: 20h+, pnl in (-1.5%, +0.75%)
p16 = fresh_portfolio()
p16['positions'].append(mkpos("ETH-EUR", 100.0, 21))
exits = t.apply_risk_overlay(p16, {"ETH-EUR": {'5m': [{'close': '99.5'}]}})
check("overlay: stall exit fires", len(exits) == 1 and "STALL EXIT" in exits[0][1], f"{exits}")

# stall does NOT fire when pnl outside band
p17 = fresh_portfolio()
p17['positions'].append(mkpos("ETH-EUR", 100.0, 21))
exits = t.apply_risk_overlay(p17, {"ETH-EUR": {'5m': [{'close': '98.0'}]}})  # -2% < band low
check("overlay: no stall when deep red (soft-invalidation zone)", len(exits) == 0, f"{exits}")

# nothing to do: fresh position, flat price
p18 = fresh_portfolio()
p18['positions'].append(mkpos("SOL-EUR", 100.0, 1))
exits = t.apply_risk_overlay(p18, {"SOL-EUR": {'5m': [{'close': '100.1'}]}})
check("overlay: quiet when nothing triggered", len(exits) == 0, f"{exits}")

# ── 4. circuit breaker day roll ───────────────────────────────────────
p19 = fresh_portfolio(cash=500.0)
t.update_circuit_breaker(p19)
check("cb: sets day start", p19.get('cb_day_start_value') == 500.0 and p19.get('cb_date'))
yesterday = (now - timedelta(days=1)).date().isoformat()
p19['cb_date'] = yesterday
p19['current_cash_eur'] = 480.0
t.update_circuit_breaker(p19)
check("cb: rolls on new UTC day", p19['cb_day_start_value'] == 480.0)

print()
if FAIL:
    print(f"❌ {len(FAIL)} FAILURES: {FAIL}")
    sys.exit(1)
print("✅ ALL OVERLAY SMOKE TESTS PASSED")
