"""Gate 0c: find a sub-second Binance options stream for an ATM symbol."""
import asyncio, json, time, sys, pathlib, websockets
ROOT = pathlib.Path(__file__).resolve().parents[1]
SYM = sys.argv[1]
streams = [f"{SYM}@bookTicker", f"{SYM}@depth10@100ms", f"{SYM}@depth10", f"{SYM}@depth20@100ms", f"{SYM}@trade",
           f"{SYM}@optionTicker", f"{SYM}@ticker", "btcusdt@optionMarkPrice", f"{SYM.lower()}@bookTicker"]
async def probe(base, st):
    url = f"{base}/{st}"; out = {"url": url, "n": 0, "dts": [], "first": None}
    try:
        async with websockets.connect(url, open_timeout=15) as ws:
            end = time.time() + 20; last = None
            while time.time() < end:
                try: m = await asyncio.wait_for(ws.recv(), end - time.time())
                except asyncio.TimeoutError: break
                now = time.time(); out["n"] += 1
                if last: out["dts"].append(round((now-last)*1000))
                last = now; out["first"] = out["first"] or str(m)[:200]
    except Exception as e: out["err"] = repr(e)[:120]
    print(f"{url:85s} n={out['n']:4d} med_dt={sorted(out['dts'])[len(out['dts'])//2] if out['dts'] else None} {out.get('err','')} {out['first'] and out['first'][:110]}")
    return out
async def main():
    bases = ["wss://fstream.binance.com/public/ws", "wss://fstream.binance.com/market/ws"]
    r = await asyncio.gather(*[probe(b, s) for b in bases for s in streams])
    (ROOT/"results/gate0_binance_opt_streams.json").write_text(json.dumps(r, indent=1))
asyncio.run(main())
