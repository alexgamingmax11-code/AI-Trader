import json, time, math, statistics, urllib.request

def fetch_klines(symbol, interval="15m", days=14):
    end = int(time.time() * 1000)
    start = end - days * 24 * 3600 * 1000
    all_k = []
    cur = start
    while cur < end:
        url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}"
               f"&interval={interval}&startTime={cur}&limit=1000")
        with urllib.request.urlopen(url, timeout=30) as r:
            k = json.loads(r.read())
        if not k:
            break
        all_k.extend(k)
        cur = k[-1][0] + 1
        if len(k) < 1000:
            break
    # dedupe
    seen, out = set(), []
    for k in all_k:
        if k[0] not in seen:
            seen.add(k[0]); out.append(k)
    out.sort(key=lambda x: x[0])
    return out

def analyze(symbol, klines):
    o = [float(k[1]) for k in klines]
    h = [float(k[2]) for k in klines]
    l = [float(k[3]) for k in klines]
    c = [float(k[4]) for k in klines]
    n = len(c)

    # 15m candle ranges (high-low)/close %
    ranges15 = [(h[i]-l[i])/c[i]*100 for i in range(n)]
    r_sorted = sorted(ranges15)
    def pct(arr, p):
        idx = int(len(arr)*p/100)
        return arr[min(idx, len(arr)-1)]

    # 15m body moves |close-open| %
    bodies15 = [abs(c[i]-o[i])/o[i]*100 for i in range(n)]
    b_sorted = sorted(bodies15)

    # 15m close-to-close returns %
    cc15 = [abs(c[i]-c[i-1])/c[i-1]*100 for i in range(1, n)]

    # rolling swings: max excursion (high-low of window)/start close
    def swing_stats(window, label):
        ups, downs, rngs = [], [], []
        for i in range(0, n - window):
            base = c[i]
            wh = max(h[i:i+window]); wl = min(l[i:i+window])
            ups.append((wh-base)/base*100)
            downs.append((base-wl)/base*100)
            rngs.append((wh-wl)/base*100)
        ups.sort(); downs.sort(); rngs.sort()
        return {
            "label": label,
            "med_up": pct(ups, 50), "p75_up": pct(ups, 75), "p90_up": pct(ups, 90),
            "med_down": pct(downs, 50), "p75_down": pct(downs, 75), "p90_down": pct(downs, 90),
            "med_rng": pct(rngs, 50), "p75_rng": pct(rngs, 75), "p90_rng": pct(rngs, 90),
        }

    s1h = swing_stats(4, "1h")
    s2h = swing_stats(8, "2h")
    s4h = swing_stats(16, "4h")
    s8h = swing_stats(32, "8h")
    s24h = swing_stats(96, "24h")

    # realized vol: stdev of 15m log returns annualized + daily
    lr = [math.log(c[i]/c[i-1]) for i in range(1, n)]
    sd15 = statistics.stdev(lr)
    rv_daily = sd15 * math.sqrt(96) * 100          # % per day
    rv_annual = sd15 * math.sqrt(96*365) * 100

    # daily (96-bar) absolute close-to-close moves
    d = [abs(c[i]-c[i-96])/c[i-96]*100 for i in range(96, n)]
    d.sort()

    # adverse excursion after up-moves: for stop calibration - how often does
    # price drop X% below any close within next 16 bars (4h)
    def prob_drop(thresh):
        hits = 0; tot = 0
        for i in range(0, n-16):
            base = c[i]
            wl = min(l[i+1:i+17])
            tot += 1
            if (base-wl)/base*100 >= thresh:
                hits += 1
        return hits/tot*100 if tot else 0

    drop_probs = {t: prob_drop(t) for t in [0.5, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0]}

    return {
        "symbol": symbol, "bars": n, "first": klines[0][0], "last": klines[-1][0],
        "price_last": c[-1],
        "r15_med": pct(r_sorted, 50), "r15_p75": pct(r_sorted, 75),
        "r15_p90": pct(r_sorted, 90), "r15_p99": pct(r_sorted, 99), "r15_max": r_sorted[-1],
        "b15_med": pct(b_sorted, 50), "b15_p90": pct(b_sorted, 90),
        "cc15_med": pct(sorted(cc15), 50), "cc15_p90": pct(sorted(cc15), 90),
        "rv_daily_pct": rv_daily, "rv_annual_pct": rv_annual,
        "swings": [s1h, s2h, s4h, s8h, s24h],
        "daily_abs_move_med": pct(d, 50), "daily_abs_move_p90": pct(d, 90),
        "drop_prob_within_4h": drop_probs,
    }

results = []
for sym in ["BTCEUR", "ETHEUR", "SOLEUR"]:
    k = fetch_klines(sym, "15m", 14)
    results.append(analyze(sym, k))
    time.sleep(0.3)

print(json.dumps(results, indent=1))
with open("/tmp/vol_results.json", "w") as f:
    json.dump(results, f, indent=1)
