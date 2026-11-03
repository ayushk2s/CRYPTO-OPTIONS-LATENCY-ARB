"""Gate 1 recorder: synchronized BBO + mark/IV for nearest-expiry ~ATM options on all three venues.

usage: python scripts/02_record.py <hours> [n_strikes=3]
Output: data_cache/rec_<start>/ticks.jsonl  (one JSON record per update; see src/clients/feeds.py)
        data_cache/rec_<start>/clock.jsonl  (per-venue clock offset + REST RTT every 30s)
        data_cache/rec_<start>/meta.json
"""
import asyncio, json, sys, time, pathlib, datetime as dt
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.clients import feeds  # noqa: E402

MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
S = requests.Session()


def pick(n_strikes):
    """Nearest expiry listed on all 3 venues; n strikes common to all 3 closest to spot; C+P."""
    sel = {"deribit": [], "binance": [], "bybit": [], "keys": [], "meta": {}}
    der = {c: S.get("https://www.deribit.com/api/v2/public/get_instruments",
                    params={"currency": c, "kind": "option", "expired": "false"}, timeout=15).json()["result"]
           for c in ("BTC", "ETH")}
    bin_ = S.get("https://eapi.binance.com/eapi/v1/exchangeInfo", timeout=15).json()["optionSymbols"]
    byb = {c: S.get("https://api.bybit.com/v5/market/instruments-info",
                    params={"category": "option", "baseCoin": c, "limit": 1000}, timeout=15).json()["result"]["list"]
           for c in ("BTC", "ETH")}
    now_ms = time.time() * 1000
    for c in ("BTC", "ETH"):
        spot = S.get("https://www.deribit.com/api/v2/public/get_index_price",
                     params={"index_name": f"{c.lower()}_usd"}, timeout=10).json()["result"]["index_price"]
        dset = {(x["expiration_timestamp"], x["strike"], x["option_type"][0].upper()): x["instrument_name"]
                for x in der[c] if x["expiration_timestamp"] > now_ms + 2 * 3600e3}
        bset = {(x["expiryDate"], float(x["strikePrice"]), x["side"][0]): x["symbol"]
                for x in bin_ if x["underlying"] == f"{c}USDT" and x["status"] == "TRADING"}
        yset = {}
        for x in byb[c]:
            if x["settleCoin"] != "USDT" or x["status"] != "Trading":
                continue
            p = x["symbol"].split("-")
            yset[(int(x["deliveryTime"]), float(p[2]), p[3][0])] = x["symbol"]
        common = set(dset) & set(bset) & set(yset)
        exp = min(e for e, _, _ in common)
        ks = sorted({k for e, k, _ in common if e == exp}, key=lambda k: abs(k - spot))[:n_strikes]
        sel["meta"][c] = {"expiry_ms": exp, "expiry": str(dt.datetime.utcfromtimestamp(exp / 1000)), "spot": spot,
                          "strikes": sorted(ks)}
        for k in sorted(ks):
            for cp in ("C", "P"):
                if (exp, k, cp) not in common:
                    continue
                key = f"{c}-{dt.datetime.utcfromtimestamp(exp / 1000):%Y%m%d}-{int(k)}-{cp}"
                sel["deribit"].append(dset[(exp, k, cp)])
                sel["binance"].append(bset[(exp, k, cp)])
                sel["bybit"].append(yset[(exp, k, cp)])
                sel["keys"].append(key)
    return sel


def clock_sample():
    """NTP-style offset (server - local midpoint) and RTT for each venue's public time endpoint."""
    out = {"tr": time.time_ns()}
    for v, url, f in (("deribit", "https://www.deribit.com/api/v2/public/get_time", lambda j: j["result"]),
                      ("binance", "https://eapi.binance.com/eapi/v1/time", lambda j: j["serverTime"]),
                      ("bybit", "https://api.bybit.com/v5/market/time", lambda j: int(j["result"]["timeNano"]) / 1e6)):
        try:
            t0 = time.time()
            j = S.get(url, timeout=5).json()
            t1 = time.time()
            out[v] = {"off_ms": f(j) - (t0 + t1) / 2 * 1000, "rtt_ms": (t1 - t0) * 1000}
        except Exception as e:
            out[v] = {"err": repr(e)[:100]}
    return out


async def main(hours, n_strikes):
    sel = pick(n_strikes)
    run = ROOT / "data_cache" / f"rec_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}"
    run.mkdir(parents=True)
    (run / "meta.json").write_text(json.dumps(sel, indent=1))
    print(json.dumps(sel["meta"], indent=1), "\n", sel["keys"], flush=True)

    fh = open(run / "ticks.jsonl", "a", buffering=1 << 20)
    counts = {}

    def emit(r):
        counts[r["v"]] = counts.get(r["v"], 0) + 1
        fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        if r["kind"] in ("conn", "disconnect"):
            print(r, flush=True)

    tasks = [feeds.deribit(sel["deribit"], sel["keys"],
                           ["ticker.BTC-PERPETUAL.100ms", "ticker.ETH-PERPETUAL.100ms",
                            "deribit_price_index.btc_usd", "deribit_price_index.eth_usd"], emit),
             feeds.bybit(sel["bybit"], sel["keys"], emit),
             feeds.binance_mark("BTCUSDT", dict(zip(sel["binance"], sel["keys"])), emit),
             feeds.binance_mark("ETHUSDT", dict(zip(sel["binance"], sel["keys"])), emit),
             feeds.binance_perp("BTCUSDT", emit), feeds.binance_perp("ETHUSDT", emit)]
    tasks += [feeds.binance_book(s, k, emit) for s, k in zip(sel["binance"], sel["keys"])]

    async def clock_loop():
        with open(run / "clock.jsonl", "a") as ch:
            while True:
                c = await asyncio.to_thread(clock_sample)
                ch.write(json.dumps(c) + "\n"); ch.flush()
                await asyncio.sleep(30)

    async def status():
        t0 = time.time()
        while True:
            await asyncio.sleep(60)
            fh.flush()
            print(f"[{(time.time() - t0) / 60:6.1f} min] {counts}", flush=True)

    end = time.time() + hours * 3600
    jobs = [asyncio.ensure_future(t) for t in tasks + [clock_loop(), status()]]
    await asyncio.sleep(max(0, end - time.time()))
    for j in jobs:
        j.cancel()
    fh.close()
    print("done", run, counts, flush=True)


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 3))
