"""Live paper trade of S1 (Deribit-led latency follow) and S2 (cross-venue crossed-market arb).

usage: python scripts/05_paper_trade.py <hours> [n_strikes=7] [n_expiries=2] [--dash]
--dash: live terminal dashboard (rich) instead of the per-minute log lines
No orders are sent anywhere; see src/paper/engine.py for the fill/cost model.
Binance is used only when this machine's DNS resolves it (ISP sinkholes it intermittently; no bypass).
Output: results/paper/<run>/{status.jsonl, summary.json, meta.json}
        data_cache/paper/<run>/{trades.jsonl, s2_crosses.jsonl}
"""
import asyncio, json, sys, time, pathlib, datetime as dt, socket
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.clients import feeds  # noqa: E402
from src.paper.engine import Engine  # noqa: E402

S = requests.Session()
# measured on this machine (results/gate2_latency.json, gate1 apparent feed delays p95)
RTT = {"bybit": 138.0, "binance": 211.0, "deribit": 255.0}
FEED_P95 = {"bybit": 150.0, "binance": 220.0, "deribit": 1300.0}


def binance_up():
    try:
        ip = socket.gethostbyname("eapi.binance.com")
        if ip.startswith("49.44."):  # Jio sinkhole seen 2026-10-23
            return False
        return S.get("https://eapi.binance.com/eapi/v1/time", timeout=5).ok
    except Exception:
        return False


def pick(n_strikes, n_exp, use_binance):
    now_ms = time.time() * 1000
    inst = {}
    der = {c: S.get("https://www.deribit.com/api/v2/public/get_instruments",
                    params={"currency": c, "kind": "option", "expired": "false"}, timeout=15).json()["result"]
           for c in ("BTC", "ETH")}
    byb = {c: S.get("https://api.bybit.com/v5/market/instruments-info",
                    params={"category": "option", "baseCoin": c, "limit": 1000}, timeout=15).json()["result"]["list"]
           for c in ("BTC", "ETH")}
    if use_binance:
        binfo = S.get("https://eapi.binance.com/eapi/v1/exchangeInfo", timeout=15).json()["optionSymbols"]
    else:  # unreachable now -> symbol/tick metadata from today's Gate 0 cache; feed connects if DNS recovers
        binfo = json.loads((ROOT / "data_cache/binance/gate0_exchangeInfo.json").read_text())["optionSymbols"]
    meta = {}
    for c in ("BTC", "ETH"):
        spot = S.get("https://www.deribit.com/api/v2/public/get_index_price",
                     params={"index_name": f"{c.lower()}_usd"}, timeout=10).json()["result"]["index_price"]
        d = {(x["expiration_timestamp"], x["strike"], x["option_type"][0].upper()): x
             for x in der[c] if x["expiration_timestamp"] > now_ms + 3 * 3600e3}
        y = {}
        for x in byb[c]:
            if x["settleCoin"] == "USDT" and x["status"] == "Trading":
                p = x["symbol"].split("-")
                y[(int(x["deliveryTime"]), float(p[2]), p[3][0])] = x
        b = {(x["expiryDate"], float(x["strikePrice"]), x["side"][0]): x
             for x in binfo if x["underlying"] == f"{c}USDT" and x["status"] == "TRADING"}
        common = set(d) & set(y)  # Binance optional: S2 needs >=2 venues per key
        exps = sorted({e for e, _, _ in common})[:n_exp]
        meta[c] = {"spot": spot, "expiries": []}
        for e in exps:
            ks = sorted({k for ee, k, _ in common if ee == e}, key=lambda k: abs(k - spot))[:n_strikes]
            meta[c]["expiries"].append({"expiry": str(dt.datetime.utcfromtimestamp(e / 1000)), "strikes": sorted(ks)})
            for k in ks:
                for cp in ("C", "P"):
                    if (e, k, cp) not in common:
                        continue
                    key = f"{c}-{dt.datetime.utcfromtimestamp(e / 1000):%Y%m%d}-{int(k)}-{cp}"
                    dx, yx = d[(e, k, cp)], y[(e, k, cp)]
                    rec = {"coin": c, "expiry_ms": e,
                           "deribit": {"sym": dx["instrument_name"], "tick": dx["tick_size"],
                                       "tick_steps": dx.get("tick_size_steps", [])},
                           "bybit": {"sym": yx["symbol"], "tick": float(yx["priceFilter"]["tickSize"])}}
                    if (e, k, cp) in b:
                        bx = b[(e, k, cp)]
                        pf = [f for f in bx["filters"] if f["filterType"] == "PRICE_FILTER"][0]
                        rec["binance"] = {"sym": bx["symbol"], "tick": float(pf["tickSize"])}
                    inst[key] = rec
    return inst, meta


def offset_sample():
    offs = []
    for url, f in (("https://www.deribit.com/api/v2/public/get_time", lambda j: j["result"]),
                   ("https://api.bybit.com/v5/market/time", lambda j: int(j["result"]["timeNano"]) / 1e6)):
        try:
            t0 = time.time(); j = S.get(url, timeout=5).json(); t1 = time.time()
            offs.append(f(j) - (t0 + t1) / 2 * 1000)
        except Exception:
            pass
    return sum(offs) / len(offs) if offs else None


async def main(hours, n_strikes, n_exp, dash=False):
    use_bin = binance_up()
    inst, meta = pick(n_strikes, n_exp, use_bin)
    name = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    run = ROOT / "results" / "paper" / name  # summaries
    raw = ROOT / "data_cache" / "paper" / name  # per-trade logs (large)
    run.mkdir(parents=True); raw.mkdir(parents=True)
    (run / "meta.json").write_text(json.dumps({"binance_at_start": use_bin, "meta": meta, "rtt_ms": RTT,
                                               "feed_p95_ms": FEED_P95, "instruments": inst}, indent=1))
    print("binance reachable at start:", use_bin, "| instruments:", len(inst), flush=True)
    print(json.dumps(meta), flush=True)
    eng = Engine(inst, RTT, FEED_P95, raw)
    eng.offset = offset_sample() or 0.0
    print(f"clock offset {eng.offset:.0f} ms", flush=True)
    keys = list(inst)
    bk = [k for k in keys if "binance" in inst[k]]
    ctx = None
    if dash:
        from src.paper import dash as dsh
        sys.stdout.reconfigure(encoding="utf-8")  # box-drawing glyphs; Windows default is cp1252
        ctx = dsh.Ctx(name, hours, len(inst), bk, snap_dir=run)
        ctx.say(f"run {name} started · {len(inst)} options · {len(bk)} on Binance", "bold cyan")
        ctx.say("Binance reachable at start" if use_bin else "Binance DNS-blocked at start (ISP); retrying",
                "green" if use_bin else "red")

    def emit(r):
        try:
            eng.on_record(r)
        except Exception as e:  # never let one bad message kill the run
            eng.stats[f"err:{type(e).__name__}"] += 1
        if ctx:
            ctx.on_record(eng, r)
        if r.get("kind") in ("conn", "disconnect") and eng.stats[f"{r['kind']}:{r['v'].split(':')[0]}"] <= 3:
            if ctx:
                v = r["v"].split(":")[0].replace("_books", "")
                ctx.say(f"{v} websocket {'connected' if r['kind'] == 'conn' else 'dropped, reconnecting'}",
                        "green" if r["kind"] == "conn" else "red")
            else:
                print({k: r[k] for k in ("kind", "v", "tr") if k in r}, r.get("err", ""), flush=True)

    tasks = [feeds.deribit([inst[k]["deribit"]["sym"] for k in keys], keys,
                           ["deribit_price_index.btc_usd", "deribit_price_index.eth_usd"], emit),
             feeds.bybit([inst[k]["bybit"]["sym"] for k in keys], keys, emit)]
    for i in range(0, len(bk), 20):  # Binance tried regardless; it delivers only while DNS lets it through
        ch = bk[i:i + 20]
        tasks.append(feeds.binance_books([inst[k]["binance"]["sym"] for k in ch], ch, emit))

    async def pump():
        while True:
            await asyncio.sleep(0.01)
            eng.process_pending()

    async def clock():
        while True:
            await asyncio.sleep(60)
            o = await asyncio.to_thread(offset_sample)
            if o is not None:
                eng.offset = 0.8 * eng.offset + 0.2 * o

    async def snaps():
        while True:
            await asyncio.sleep(10)
            eng.snapshot_crosses()

    t_start = time.time()

    async def status():
        prev_bin = None
        with open(run / "status.jsonl", "a") as fh:
            while True:
                await asyncio.sleep(60)
                s = eng.summary()
                s["min"] = round((time.time() - t_start) / 60, 1)
                s["binance_valid"] = sum(eng.valid[("binance", k)] for k in bk)
                fh.write(json.dumps(s, default=float) + "\n"); fh.flush()
                s1 = {k: round(v, 1) for k, v in s["pnl"].items() if "real|signal" in k or k.startswith("S2")}
                if ctx:
                    if prev_bin is not None and (prev_bin > 0) != (s["binance_valid"] > 0):
                        ctx.say("Binance back online" if s["binance_valid"] else "Binance lost (ISP DNS sinkhole), masked",
                                "green" if s["binance_valid"] else "red")
                    prev_bin = s["binance_valid"]
                    if int(s["min"]) % 10 == 0:
                        ctx.say(f"checkpoint {s['min']:.0f} min · {sum(s['n'].values()):,} paper trades · status.jsonl saved",
                                "grey62")
                    continue
                print(f"[{s['min']:6.1f} min] binance_live={s['binance_valid']}/{len(bk)} "
                      f"q={ {k: v for k, v in s['stats'].items() if k.startswith('q:')} } pnl={s1}", flush=True)

    extra = [dsh.run(eng, ctx)] if ctx else []
    jobs = [asyncio.ensure_future(t) for t in tasks + [pump(), clock(), snaps(), status()] + extra]
    await asyncio.sleep(hours * 3600)
    for j in jobs:
        j.cancel()
    # force-close any open S1 positions at last quotes
    await asyncio.sleep(0)
    for t, fn, args in sorted(eng.pending, key=lambda p: p[0]):
        if fn.__name__ == "s1_exit":
            fn(*args)
    eng.pending = []
    s = eng.summary()
    s["hours"] = hours
    (run / "summary.json").write_text(json.dumps(s, indent=1, default=float))
    print("done", run, flush=True)


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    asyncio.run(main(float(a[0]), int(a[1]) if len(a) > 1 else 7, int(a[2]) if len(a) > 2 else 2,
                     dash="--dash" in sys.argv))
