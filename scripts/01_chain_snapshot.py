"""Gate 0 part 2: live options-chain census + ATM spread/depth for BTC & ETH on each venue.

Writes results/gate0_chain_census.json, results/gate0_atm_spreads.csv.
"""
import json, time, pathlib, re, datetime as dt
import requests, pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
RES = ROOT / "results"
S = requests.Session()
MON = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}


def parse(sym):
    """-> (coin, expiry_date, strike, 'C'/'P') for any of the three naming schemes."""
    p = sym.split("-")
    coin = p[0]
    e = p[1]
    if re.fullmatch(r"\d{6}", e):  # Binance YYMMDD
        exp = dt.date(2000 + int(e[:2]), int(e[2:4]), int(e[4:]))
    else:  # Deribit/Bybit DMMMYY
        m = re.fullmatch(r"(\d{1,2})([A-Z]{3})(\d{2})", e)
        exp = dt.date(2000 + int(m[3]), MON[m[2]], int(m[1]))
    return coin, exp, float(p[2]), p[3][0]


def load_deribit(coin):
    r = S.get("https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
              params={"currency": coin, "kind": "option"}, timeout=15).json()["result"]
    rows = []
    for x in r:
        c, exp, k, cp = parse(x["instrument_name"])
        u = x["underlying_price"]
        bid = x["bid_price"] * u if x["bid_price"] else None
        ask = x["ask_price"] * u if x["ask_price"] else None
        rows.append(dict(venue="deribit", sym=x["instrument_name"], coin=c, exp=exp, K=k, cp=cp, S=u,
                         bid=bid, ask=ask, mark=x["mark_price"] * u, mark_iv=x["mark_iv"] / 100,
                         oi=x["open_interest"], vol24=x["volume"]))
    return rows


def load_binance():
    t = {x["symbol"]: x for x in S.get("https://eapi.binance.com/eapi/v1/ticker", timeout=15).json()}
    m = {x["symbol"]: x for x in S.get("https://eapi.binance.com/eapi/v1/mark", timeout=15).json()}
    rows = []
    for s, x in t.items():
        c, exp, k, cp = parse(s)
        if c not in ("BTC", "ETH"):
            continue
        mk = m.get(s, {})
        f = lambda v: float(v) if v not in (None, "", "0", "0.000") and float(v) > 0 else None
        rows.append(dict(venue="binance", sym=s, coin=c, exp=exp, K=k, cp=cp, S=float(x["exercisePrice"]),
                         bid=f(x["bidPrice"]), ask=f(x["askPrice"]), mark=f(mk.get("markPrice")),
                         mark_iv=f(mk.get("markIV")), bid_iv=f(mk.get("bidIV")), ask_iv=f(mk.get("askIV")),
                         vol24=float(x["volume"])))
    return rows


def load_bybit(coin):
    r = S.get("https://api.bybit.com/v5/market/tickers", params={"category": "option", "baseCoin": coin},
              timeout=15).json()["result"]["list"]
    rows = []
    f = lambda v: float(v) if v not in (None, "") and float(v) > 0 else None
    for x in r:
        c, exp, k, cp = parse(x["symbol"])
        rows.append(dict(venue="bybit", sym=x["symbol"], coin=c, exp=exp, K=k, cp=cp,
                         S=float(x["underlyingPrice"]), bid=f(x["bid1Price"]), ask=f(x["ask1Price"]),
                         bid_sz=f(x["bid1Size"]), ask_sz=f(x["ask1Size"]), mark=f(x["markPrice"]),
                         mark_iv=f(x["markIv"]), bid_iv=f(x["bid1Iv"]), ask_iv=f(x["ask1Iv"]),
                         oi=float(x["openInterest"]), vol24=float(x["volume24h"])))
    return rows


def depth(venue, sym, S_):
    """Top-5 book in USD price + size (contracts = 1 coin each on all three venues)."""
    try:
        if venue == "deribit":
            r = S.get("https://www.deribit.com/api/v2/public/get_order_book",
                      params={"instrument_name": sym, "depth": 5}, timeout=10).json()["result"]
            u = r["underlying_price"]
            return [[p * u, q] for p, q in r["bids"]], [[p * u, q] for p, q in r["asks"]]
        if venue == "binance":
            r = S.get("https://eapi.binance.com/eapi/v1/depth", params={"symbol": sym, "limit": 10}, timeout=10).json()
            return [[float(p), float(q)] for p, q in r["bids"][:5]], [[float(p), float(q)] for p, q in r["asks"][:5]]
        if venue == "bybit":
            r = S.get("https://api.bybit.com/v5/market/orderbook",
                      params={"category": "option", "symbol": sym, "limit": 25}, timeout=10).json()["result"]
            return [[float(p), float(q)] for p, q in r["b"][:5]], [[float(p), float(q)] for p, q in r["a"][:5]]
    except Exception as e:
        return repr(e), None


def main():
    ts = time.time()
    rows = load_deribit("BTC") + load_deribit("ETH") + load_binance() + load_bybit("BTC") + load_bybit("ETH")
    df = pd.DataFrame(rows)
    df["two_sided"] = df.bid.notna() & df.ask.notna()
    census = {}
    for (v, c), g in df.groupby(["venue", "coin"]):
        census[f"{v}.{c}"] = dict(instruments=len(g), expiries=g.exp.nunique(),
                                  strikes_unique=g.K.nunique(),
                                  two_sided=int(g.two_sided.sum()),
                                  expiry_list=sorted(str(e) for e in g.exp.unique()),
                                  vol24_coins=round(g.vol24.sum(), 1))
        print(f"{v:8s} {c}: {len(g):4d} instr, {g.exp.nunique():2d} expiries, {g.K.nunique():3d} strikes, "
              f"two-sided {int(g.two_sided.sum())}, 24h vol {g.vol24.sum():.0f} contracts")

    # ATM rows: for each venue/coin, the 3 nearest expiries + 2 common monthly-ish expiries, closest-K call & put
    out = []
    for (v, c), g in df.groupby(["venue", "coin"]):
        exps = sorted(g.exp.unique())[:3]
        for exp in exps:
            ge = g[g.exp == exp]
            Su = ge.S.median()
            katm = ge.K.iloc[(ge.K - Su).abs().argsort().iloc[0]]
            for _, r in ge[ge.K == katm].iterrows():
                rec = r.to_dict()
                rec["dte_days"] = (dt.datetime.combine(exp, dt.time(8)) - dt.datetime.utcfromtimestamp(ts)).total_seconds() / 86400
                if r.two_sided:
                    mid = (r.bid + r.ask) / 2
                    rec["spread_usd"] = r.ask - r.bid
                    rec["spread_pct_mid"] = (r.ask - r.bid) / mid * 100
                    rec["spread_bps_underlying"] = (r.ask - r.bid) / Su * 1e4
                if exp == exps[0]:
                    b, a = depth(v, r.sym, Su)
                    rec["book_bids"], rec["book_asks"] = b, a
                out.append(rec)
    atm = pd.DataFrame(out)
    atm.to_csv(RES / "gate0_atm_spreads.csv", index=False)
    cols = ["venue", "coin", "sym", "dte_days", "S", "bid", "ask", "mark", "mark_iv", "spread_usd", "spread_pct_mid",
            "spread_bps_underlying", "bid_sz", "ask_sz"]
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print(atm[cols].round(3).to_string())
    print("\nTop-of-book depth (nearest expiry ATM):")
    for _, r in atm.dropna(subset=["book_bids"]).iterrows():
        print(f"  {r.venue:8s} {r.sym:28s} bids={r.book_bids[:3]}  asks={r.book_asks[:3] if r.book_asks else None}")
    (RES / "gate0_chain_census.json").write_text(json.dumps({"ts": ts, "census": census}, indent=1, default=str))
    df.to_parquet(ROOT / "data_cache" / f"chain_snapshot_{int(ts)}.parquet") if False else df.to_csv(
        ROOT / "data_cache" / f"chain_snapshot_{int(ts)}.csv", index=False)


if __name__ == "__main__":
    main()
