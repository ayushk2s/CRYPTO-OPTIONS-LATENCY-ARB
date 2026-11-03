"""Polymarket "Bitcoin above $K on <date>?" (digital, Binance BTCUSDT 1m close at 12:00 ET) vs the
Deribit-implied probability of the same event.

usage: python scripts/08_polymarket_vs_deribit.py <minutes> [interval_s=30]
Per snapshot, for every open daily "Bitcoin above" event:
  - Polymarket YES best bid/ask + sizes from the CLOB book (never Gamma mids; empty-middle books are fake)
  - Deribit fair P(S_T > K): smile (mark_iv vs log-moneyness) for the two Deribit expiries that bracket the
    Polymarket resolution time; total variance interpolated linearly in time at fixed moneyness; forward
    interpolated; strike converted USDT->USD (K_usd = K * p, p = USD per USDT from Deribit/Bybit index ratio);
    digital = N(d2) - F*phi(d1)*sqrt(T) * dsigma/dK  (smile-slope correction)
  - intra-Polymarket monotonicity: YES(K_lo) must be >= YES(K_hi); a bid(K_hi) > ask(K_lo) is a riskless arb
    (buy YES K_lo, buy NO K_hi -> payoff >= 1 for cost < 1)
Output: results/pm_vs_deribit/<run>/snapshots.jsonl + printed summary.
NOTE: the residual risk of any Polymarket-vs-Deribit trade is the gap between Deribit settlement
(07:30-08:00 UTC USD TWAP) and Polymarket resolution (single 1m Binance close at 12:00 ET) -> not riskless.
"""
import sys, json, time, math, pathlib, re, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import numpy as np, requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0"
POOL = ThreadPoolExecutor(8)
N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
phi = lambda x: math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def get(url, **p):
    for i in range(4):
        try:
            return S.get(url, params=p, timeout=20).json()
        except Exception:
            time.sleep(1 + i)
    return None


def pm_events():
    evs = get("https://gamma-api.polymarket.com/events", closed="false", limit=500, tag_slug="bitcoin") or []
    out = []
    for e in evs:
        if not e.get("title", "").startswith("Bitcoin above"):
            continue
        end = dt.datetime.fromisoformat(e["endDate"].replace("Z", "+00:00"))
        mk = []
        for m in e["markets"]:
            if m.get("closed"):
                continue
            K = float(m["groupItemTitle"].replace(",", "").replace("$", ""))
            yes = json.loads(m["clobTokenIds"])[0]
            mk.append({"K": K, "yes": yes, "fees": m.get("feesEnabled")})
        out.append({"title": e["title"], "end": end, "markets": sorted(mk, key=lambda x: x["K"])})
    return out


def pm_book(token):
    b = get("https://clob.polymarket.com/book", token_id=token) or {}
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks", []))
    return (bids[0] if bids else (None, 0)), (asks[0] if asks else (None, 0))


def deribit_surface():
    r = get("https://www.deribit.com/api/v2/public/get_book_summary_by_currency", currency="BTC", kind="option")["result"]
    idx = get("https://www.deribit.com/api/v2/public/get_index_price", index_name="btc_usd")["result"]["index_price"]
    byb = get("https://api.bybit.com/v5/market/tickers", category="option", baseCoin="BTC")["result"]["list"][0]
    p = idx / float(byb["indexPrice"])  # USD per USDT
    MON = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
    exps = {}
    for x in r:
        _, e, K, cp = x["instrument_name"].split("-")
        m = re.fullmatch(r"(\d{1,2})([A-Z]{3})(\d{2})", e)
        T = dt.datetime(2000 + int(m[3]), MON[m[2]], int(m[1]), 8, tzinfo=dt.timezone.utc)
        if not x.get("mark_iv"):
            continue
        d = exps.setdefault(T, {"F": x["underlying_price"], "pts": {}})
        d["pts"].setdefault(float(K), []).append(x["mark_iv"] / 100)
    for T, d in exps.items():
        ks = np.array(sorted(d["pts"]))
        d["x"] = np.log(ks / d["F"])
        d["iv"] = np.array([np.mean(d["pts"][k]) for k in ks])
    return exps, idx, p


def fair_digital(exps, T_target, K_usd, now):
    Ts = sorted(t for t in exps if t > now)
    lo = max((t for t in Ts if t <= T_target), default=None)
    hi = min((t for t in Ts if t > T_target), default=None)
    if lo is None or hi is None:
        return None
    yr = lambda t: (t - now).total_seconds() / (365 * 86400)
    t1, t2, ts = yr(lo), yr(hi), yr(T_target)
    a = (ts - t1) / (t2 - t1)
    F = exps[lo]["F"] * (1 - a) + exps[hi]["F"] * a

    def sig(K):
        x = math.log(K / F)
        w1 = np.interp(x, exps[lo]["x"], exps[lo]["iv"]) ** 2 * t1
        w2 = np.interp(x, exps[hi]["x"], exps[hi]["iv"]) ** 2 * t2
        return math.sqrt(max(w1 + (w2 - w1) * a, 1e-12) / ts)
    s = sig(K_usd)
    dK = K_usd * 0.002
    slope = (sig(K_usd + dK) - sig(K_usd - dK)) / (2 * dK)
    d1 = (math.log(F / K_usd) + 0.5 * s * s * ts) / (s * math.sqrt(ts))
    d2 = d1 - s * math.sqrt(ts)
    return N(d2) - F * phi(d1) * math.sqrt(ts) * slope, s, F, (lo, hi)


def main(minutes, interval):
    run = ROOT / "results" / "pm_vs_deribit" / time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    run.mkdir(parents=True)
    fh = open(run / "snapshots.jsonl", "a", buffering=1)
    end = time.time() + minutes * 60
    while time.time() < end:
        t0 = time.time()
        now = dt.datetime.now(dt.timezone.utc)
        try:
            evs = pm_events()
            exps, idx, p = deribit_surface()
        except Exception as e:
            print("fetch error", repr(e)[:100], flush=True); time.sleep(interval); continue
        for e in evs:
            if (e["end"] - now).total_seconds() < 1800:
                continue
            books = list(POOL.map(lambda m: pm_book(m["yes"]), e["markets"]))
            rows = []
            for m, ((bp, bs), (ap, as_)) in zip(e["markets"], books):
                fd = fair_digital(exps, e["end"], m["K"] * p, now)
                if fd is None:
                    continue
                P, s, F, br = fd
                rows.append({"K": m["K"], "bid": bp, "bsz": bs, "ask": ap, "asz": as_, "fair": P, "iv": s,
                             "buy_edge": (P - ap) if ap else None, "sell_edge": (bp - P) if bp else None})
            # intra-Polymarket riskless monotonicity arb: bid(YES K_hi) > ask(YES K_lo), K_lo < K_hi
            mono = []
            for i, lo in enumerate(rows):
                for hi in rows[i + 1:]:
                    if lo["ask"] and hi["bid"] and hi["bid"] > lo["ask"]:
                        mono.append({"K_lo": lo["K"], "K_hi": hi["K"], "profit_per_share": hi["bid"] - lo["ask"],
                                     "size": min(lo["asz"], hi["bsz"])})
            rec = {"t": t0, "event": e["title"], "end": e["end"].isoformat(), "hours_left": (e["end"] - now).total_seconds() / 3600,
                   "spot_usd": idx, "p_usdt": p, "rows": rows, "mono_arbs": mono}
            fh.write(json.dumps(rec, default=str) + "\n")
            live = [r for r in rows if r["bid"] and r["ask"] and 0.02 < (r["bid"] + r["ask"]) / 2 < 0.98]
            be = max((r["buy_edge"] for r in live if r["buy_edge"] is not None), default=float("nan"))
            se = max((r["sell_edge"] for r in live if r["sell_edge"] is not None), default=float("nan"))
            dev = [((r["bid"] + r["ask"]) / 2 - r["fair"]) for r in live]
            print(f"{time.strftime('%H:%M:%S')} {e['title'][:34]:34s} {rec['hours_left']:5.1f}h live={len(live):2d} "
                  f"mid-fair mean={np.mean(dev) * 100 if dev else float('nan'):+5.1f}c maxabs={np.max(np.abs(dev)) * 100 if dev else float('nan'):4.1f}c "
                  f"best buy-edge={be * 100:+5.1f}c best sell-edge={se * 100:+5.1f}c mono_arbs={len(mono)}", flush=True)
        time.sleep(max(0, interval - (time.time() - t0)))


if __name__ == "__main__":
    main(float(sys.argv[1]), float(sys.argv[2]) if len(sys.argv) > 2 else 30)
