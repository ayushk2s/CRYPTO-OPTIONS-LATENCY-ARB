"""Idea 1: full-chain crossed-market scan (all strikes, all expiries) incl. cheap wings where the
premium cap makes fees proportional to premium.

usage: python scripts/07_wing_scan.py <minutes> [interval_s=20]
Every interval: pull all BTC/ETH options on all 3 venues in parallel (REST), compute for every
instrument and venue pair (buy A ask, sell B bid), in USD:
  edge = bid_B*p_B - ask_A*p_A + strike_gap_worst
         - fee_A - fee_B                      (min(0.03% S, cap*premium) each)
         - exercise: 0.015% S * |delta|       (expected; long leg pays only if ITM at expiry)
         - settle:   2 bp S * |delta|         (two settlement indexes straddling the strike)
Candidates with edge > 0 are re-checked on order books fetched simultaneously from both venues
(real top-of-book prices + sizes). Logs to results/wing_scan/<run>/.
"""
import sys, json, time, pathlib, re, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.paper.engine import gap_worst, CAP, TAKER, EXERCISE, SETTLE_BP  # noqa: E402

MON = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
S_ = requests.Session()
POOL = ThreadPoolExecutor(8)
MAX_DTE = 30


def key_of(sym):
    p = sym.split("-")
    e = p[1]
    if re.fullmatch(r"\d{6}", e):
        d = dt.date(2000 + int(e[:2]), int(e[2:4]), int(e[4:]))
    else:
        m = re.fullmatch(r"(\d{1,2})([A-Z]{3})(\d{2})", e)
        d = dt.date(2000 + int(m[3]), MON[m[2]], int(m[1]))
    return f"{p[0]}-{d:%Y%m%d}-{int(float(p[2]))}-{p[3][0]}"


def f(x):
    try:
        x = float(x)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def get(url, **params):
    return S_.get(url, params=params, timeout=10).json()


def snapshot():
    jobs = {"der_BTC": POOL.submit(get, "https://www.deribit.com/api/v2/public/get_book_summary_by_currency", currency="BTC", kind="option"),
            "der_ETH": POOL.submit(get, "https://www.deribit.com/api/v2/public/get_book_summary_by_currency", currency="ETH", kind="option"),
            "byb_BTC": POOL.submit(get, "https://api.bybit.com/v5/market/tickers", category="option", baseCoin="BTC"),
            "byb_ETH": POOL.submit(get, "https://api.bybit.com/v5/market/tickers", category="option", baseCoin="ETH"),
            "bin_t": POOL.submit(get, "https://eapi.binance.com/eapi/v1/ticker"),
            "bin_m": POOL.submit(get, "https://eapi.binance.com/eapi/v1/mark")}
    out, idx = {}, {}
    q = {"deribit": {}, "bybit": {}, "binance": {}}
    for c in ("BTC", "ETH"):
        try:
            for x in jobs[f"der_{c}"].result()["result"]:
                S = x["estimated_delivery_price"]
                idx[("deribit", c)] = S
                q["deribit"][key_of(x["instrument_name"])] = dict(sym=x["instrument_name"], S=S,
                    bid=x["bid_price"] * S if x["bid_price"] else None, ask=x["ask_price"] * S if x["ask_price"] else None)
        except Exception as e:
            out[f"err_der_{c}"] = repr(e)[:80]
        try:
            for x in jobs[f"byb_{c}"].result()["result"]["list"]:
                if not x["symbol"].endswith("-USDT"):
                    continue
                idx[("bybit", c)] = float(x["indexPrice"])
                q["bybit"][key_of(x["symbol"])] = dict(sym=x["symbol"], bid=f(x["bid1Price"]), ask=f(x["ask1Price"]),
                    bsz=f(x["bid1Size"]), asz=f(x["ask1Size"]), delta=float(x["delta"] or 0))
        except Exception as e:
            out[f"err_byb_{c}"] = repr(e)[:80]
    try:
        mk = {x["symbol"]: x for x in jobs["bin_m"].result()}
        for x in jobs["bin_t"].result():
            if x["symbol"][:3] not in ("BTC", "ETH"):
                continue
            m = mk.get(x["symbol"], {})
            q["binance"][key_of(x["symbol"])] = dict(sym=x["symbol"], bid=f(x["bidPrice"]), ask=f(x["askPrice"]),
                                                     delta=float(m.get("delta") or 0))
    except Exception as e:
        out["err_bin"] = repr(e)[:80]
    return q, idx, out


def fee(v, S, prem):
    return min(TAKER * S, CAP[v] * prem)


def edges(q, idx):
    p = {"deribit": 1.0}
    pb = idx[("deribit", "BTC")] / idx[("bybit", "BTC")]
    p["bybit"] = p["binance"] = pb
    rows = []
    keys = set(q["deribit"]) | set(q["bybit"]) | set(q["binance"])
    for k in keys:
        coin, _, K, cp = k.split("-")
        K = float(K)
        S = idx.get(("deribit", coin))
        d = next((q[v][k]["delta"] for v in ("bybit", "binance") if k in q[v] and q[v][k].get("delta")), 0.5)
        for A in q:
            for B in q:
                if A == B or k not in q[A] or k not in q[B]:
                    continue
                a, b = q[A][k].get("ask"), q[B][k].get("bid")
                if not a or not b:
                    continue
                gap = gap_worst(K, cp, p[A], p[B])
                gross = b * p[B] - a * p[A] + gap
                if gross <= 0:
                    continue
                cost = (fee(A, S, a * p[A]) + fee(B, S, b * p[B]) + EXERCISE * S * abs(d)
                        + SETTLE_BP * 1e-4 * S * abs(d))
                rows.append(dict(key=k, buy=A, sell=B, ask=a, bid=b, gross=gross, cost=cost, net=gross - cost,
                                 delta=d, gap=gap, p=pb))
    return rows, p


def book(v, sym, S):
    if v == "deribit":
        r = get("https://www.deribit.com/api/v2/public/get_order_book", instrument_name=sym, depth=3)["result"]
        u = r["index_price"]
        return [(x[0] * u, x[1]) for x in r["bids"]], [(x[0] * u, x[1]) for x in r["asks"]]
    if v == "binance":
        r = get("https://eapi.binance.com/eapi/v1/depth", symbol=sym, limit=10)
        return [(float(a), float(b)) for a, b in r["bids"][:3]], [(float(a), float(b)) for a, b in r["asks"][:3]]
    r = get("https://api.bybit.com/v5/market/orderbook", category="option", symbol=sym, limit=25)["result"]
    return [(float(a), float(b)) for a, b in r["b"][:3]], [(float(a), float(b)) for a, b in r["a"][:3]]


def confirm(c, q, idx, p):
    k, A, B = c["key"], c["buy"], c["sell"]
    coin = k[:3]
    S = idx[("deribit", coin)]
    fa = POOL.submit(book, A, q[A][k]["sym"], S)
    fb = POOL.submit(book, B, q[B][k]["sym"], S)
    try:
        (_, asks), (bids, _) = fa.result(), fb.result()
    except Exception as e:
        return dict(c, confirmed=False, err=repr(e)[:80])
    if not asks or not bids:
        return dict(c, confirmed=False, why="empty")
    a, asz = asks[0]
    b, bsz = bids[0]
    K, cp = float(k.split("-")[2]), k.split("-")[3]
    gross = b * p[B] - a * p[A] + gap_worst(K, cp, p[A], p[B])
    cost = (fee(A, S, a * p[A]) + fee(B, S, b * p[B]) + EXERCISE * S * abs(c["delta"])
            + SETTLE_BP * 1e-4 * S * abs(c["delta"]))
    qty = min(asz, bsz)
    return dict(c, confirmed=gross - cost > 0, c_ask=a, c_bid=b, c_qty=qty, c_net=gross - cost,
                c_net_total=(gross - cost) * qty, premium=a * p[A])


def main(minutes, interval):
    run = ROOT / "results" / "wing_scan" / time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    run.mkdir(parents=True)
    fh = open(run / "candidates.jsonl", "a", buffering=1)
    sh = open(run / "snapshots.jsonl", "a", buffering=1)
    end = time.time() + minutes * 60
    i = 0
    while time.time() < end:
        t0 = time.time()
        q, idx, err = snapshot()
        n = {v: len(x) for v, x in q.items()}
        try:
            rows, p = edges(q, idx)
        except KeyError as e:
            print("missing index", e, err, flush=True)
            time.sleep(interval)
            continue
        cands = [r for r in rows if r["net"] > 0]
        # long-dated deep-ITM "crosses" are interest-rate carry (~2-3 %/yr on locked capital, < T-bills;
        # see results/wing_scan first run) -> only confirm expiries within MAX_DTE days, where carry ~ 0
        now = dt.datetime.utcnow()
        for r in cands:
            r["dte"] = (dt.datetime.strptime(r["key"].split("-")[1], "%Y%m%d") + dt.timedelta(hours=8) - now).total_seconds() / 86400
        n_long = sum(r["dte"] > MAX_DTE for r in cands)
        cands = [r for r in cands if r["dte"] <= MAX_DTE]
        conf = [confirm(c, q, idx, p) for c in sorted(cands, key=lambda r: -r["net"])[:10]]
        for c in conf:
            fh.write(json.dumps(dict(c, t=t0), default=float) + "\n")
        best = max(rows, key=lambda r: r["net"]) if rows else None
        sh.write(json.dumps({"t": t0, "n": n, "err": err, "p": p["bybit"], "n_gross_cross": len(rows),
                             "n_net_pos_short": len(cands), "n_net_pos_long": n_long, "n_confirmed": sum(c["confirmed"] for c in conf),
                             "best": best}, default=float) + "\n")
        i += 1
        print(f"[{i:3d}] inst={n} crossed(gross)={len(rows)} net>0: long-dated={n_long} <=30d={len(cands)} confirmed={sum(c['confirmed'] for c in conf)} "
              f"best={(best['key'], best['buy'] + '->' + best['sell'], round(best['gross'], 2), round(best['net'], 2)) if best else None} {err or ''}",
              flush=True)
        for c in conf:
            if c["confirmed"]:
                print("   CONFIRMED", c["key"], c["buy"], "->", c["sell"], "ask", c["c_ask"], "bid", c["c_bid"],
                      "qty", c["c_qty"], "net/contract", round(c["c_net"], 2), "total", round(c["c_net_total"], 2), flush=True)
        time.sleep(max(0, interval - (time.time() - t0)))


if __name__ == "__main__":
    main(float(sys.argv[1]), float(sys.argv[2]) if len(sys.argv) > 2 else 20)
