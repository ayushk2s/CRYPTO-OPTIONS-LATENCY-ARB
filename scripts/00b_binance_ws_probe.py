"""Gate 0b: is Binance fstream silent on this machine in general, or just for our stream names?"""
import asyncio, json, time, pathlib, websockets
ROOT = pathlib.Path(__file__).resolve().parents[1]
SYM = "BTC-261025-79000-P"
cases = [
 ("futures control raw", "wss://fstream.binance.com/ws/btcusdt@bookTicker", None),
 ("futures control public", "wss://fstream.binance.com/public/ws/btcusdt@bookTicker", None),
 ("futures control market", "wss://fstream.binance.com/market/ws/btcusdt@markPrice", None),
 ("spot control", "wss://stream.binance.com:9443/ws/btcusdt@bookTicker", None),
 ("opt SUBSCRIBE public", "wss://fstream.binance.com/public/ws", {"method":"SUBSCRIBE","params":[f"{SYM}@optionTicker", f"{SYM}@bookTicker"],"id":7}),
 ("opt SUBSCRIBE market", "wss://fstream.binance.com/market/ws", {"method":"SUBSCRIBE","params":["btcusdt@optionMarkPrice", f"{SYM}@optionTicker"],"id":8}),
 ("opt LIST_SUBSCRIPTIONS public", "wss://fstream.binance.com/public/ws", {"method":"LIST_SUBSCRIPTIONS","id":9}),
 ("opt bookTicker public", f"wss://fstream.binance.com/public/ws/{SYM}@bookTicker", None),
 ("opt depth public", f"wss://fstream.binance.com/public/ws/{SYM}@depth10@100ms", None),
 ("opt markprice market", "wss://fstream.binance.com/market/ws/btcusdt@optionMarkPrice", None),
]
async def probe(name, url, sub):
    out = {"name": name, "url": url, "msgs": []}
    try:
        async with websockets.connect(url, open_timeout=15) as ws:
            if sub: await ws.send(json.dumps(sub))
            end = time.time() + 15
            while time.time() < end and len(out["msgs"]) < 3:
                try: out["msgs"].append(str(await asyncio.wait_for(ws.recv(), end-time.time()))[:250])
                except asyncio.TimeoutError: break
    except Exception as e: out["error"] = repr(e)[:200]
    print(f"{name:32s} msgs={len(out['msgs'])} {out.get('error','')} {out['msgs'][:1]}")
    return out
async def main():
    r = await asyncio.gather(*[probe(*c) for c in cases])
    (ROOT/"results/gate0_binance_ws_probe.json").write_text(json.dumps(r, indent=1))
asyncio.run(main())
