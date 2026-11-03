"""Gate 0: real reachability probes for Deribit / Binance Options / Bybit Options.

Public market-data endpoints only. Saves raw responses to data_cache/<venue>/gate0_*
and a summary to results/gate0_connectivity.json.
"""
import asyncio, json, time, pathlib, traceback
import requests, websockets

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / "data_cache"
RES = ROOT / "results"
for v in ("deribit", "binance", "bybit"):
    (CACHE / v).mkdir(parents=True, exist_ok=True)
RES.mkdir(exist_ok=True)

S = requests.Session()
S.headers["User-Agent"] = "options-research/0.1"
summary = {"ts": time.time(), "rest": {}, "ws": {}}


def rest(venue, name, url, params=None):
    t0 = time.perf_counter()
    try:
        r = S.get(url, params=params, timeout=15)
        dt = (time.perf_counter() - t0) * 1000
        body = r.text
        (CACHE / venue / f"gate0_{name}.json").write_text(body, encoding="utf-8")
        rec = {"url": r.url, "status": r.status_code, "ms": round(dt, 1), "bytes": len(body),
               "head": body[:300]}
    except Exception as e:
        rec = {"url": url, "error": repr(e), "ms": round((time.perf_counter() - t0) * 1000, 1)}
    summary["rest"][f"{venue}.{name}"] = rec
    print(f"[REST] {venue}.{name}: {rec.get('status', rec.get('error'))} {rec['ms']}ms {rec.get('bytes', '')}B")
    return rec


async def ws_probe(venue, url, sub_msg, n_msgs=5, timeout=20):
    msgs, t0 = [], time.perf_counter()
    rec = {"url": url}
    try:
        async with websockets.connect(url, open_timeout=15, ping_interval=20, max_size=2**24) as ws:
            rec["connect_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            if sub_msg is not None:
                await ws.send(json.dumps(sub_msg))
            end = time.time() + timeout
            while len(msgs) < n_msgs and time.time() < end:
                try:
                    m = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
                    msgs.append(m if isinstance(m, str) else m.decode())
                except asyncio.TimeoutError:
                    break
    except Exception as e:
        rec["error"] = repr(e)
    rec["n_msgs"] = len(msgs)
    rec["samples"] = [m[:400] for m in msgs[:5]]
    summary["ws"][f"{venue}:{url}"] = rec
    (CACHE / venue / f"gate0_ws_{len(summary['ws'])}.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
    print(f"[WS] {venue} {url}: connect={rec.get('connect_ms')} msgs={len(msgs)} err={rec.get('error')}")
    return rec


def main():
    # ---------- REST ----------
    D = "https://www.deribit.com/api/v2/public"
    rest("deribit", "time", f"{D}/get_time")
    for c in ("BTC", "ETH"):
        rest("deribit", f"index_{c}", f"{D}/get_index_price", {"index_name": f"{c.lower()}_usd"})
        rest("deribit", f"summary_{c}", f"{D}/get_book_summary_by_currency", {"currency": c, "kind": "option"})

    B = "https://eapi.binance.com/eapi/v1"
    rest("binance", "time", f"{B}/time")
    rest("binance", "exchangeInfo", f"{B}/exchangeInfo")
    rest("binance", "ticker", f"{B}/ticker")
    rest("binance", "mark", f"{B}/mark")
    for c in ("BTC", "ETH"):
        rest("binance", f"index_{c}", f"{B}/index", {"underlying": f"{c}USDT"})

    Y = "https://api.bybit.com/v5"
    rest("bybit", "time", f"{Y}/market/time")
    for c in ("BTC", "ETH"):
        rest("bybit", f"tickers_{c}", f"{Y}/market/tickers", {"category": "option", "baseCoin": c})
        rest("bybit", f"instruments_{c}", f"{Y}/market/instruments-info",
             {"category": "option", "baseCoin": c, "limit": 1000})

    # ---------- WS ----------
    async def ws_all():
        # pick one live symbol per venue from REST results for subscription
        try:
            d = json.loads((CACHE / "deribit/gate0_summary_BTC.json").read_text())["result"]
            dsym = max(d, key=lambda x: x.get("volume") or 0)["instrument_name"]
        except Exception:
            dsym = None
        try:
            b = json.loads((CACHE / "binance/gate0_ticker.json").read_text())
            bsym = max([x for x in b if x["symbol"].startswith("BTC")], key=lambda x: float(x.get("volume") or 0))["symbol"]
        except Exception:
            bsym = None
        try:
            y = json.loads((CACHE / "bybit/gate0_tickers_BTC.json").read_text())["result"]["list"]
            ysym = max(y, key=lambda x: float(x.get("volume24h") or 0))["symbol"]
        except Exception:
            ysym = None
        summary["ws_symbols"] = {"deribit": dsym, "binance": bsym, "bybit": ysym}
        print("WS symbols:", summary["ws_symbols"])
        tasks = []
        if dsym:
            tasks.append(ws_probe("deribit", "wss://www.deribit.com/ws/api/v2",
                                  {"jsonrpc": "2.0", "id": 1, "method": "public/subscribe",
                                   "params": {"channels": [f"ticker.{dsym}.100ms", f"book.{dsym}.100ms"]}}, 10))
        if bsym:
            # Binance options WS: try known hosts / path styles
            for url in (f"wss://nbstream.binance.com/eoptions/ws/{bsym}@ticker",
                        f"wss://nbstream.binance.com/eoptions/stream?streams={bsym}@ticker",
                        f"wss://fstream.binance.com/public/ws/{bsym}@optionTicker",
                        f"wss://fstream.binance.com/market/ws/{bsym}@optionTicker"):
                tasks.append(ws_probe("binance", url, None, 5, 20))
            tasks.append(ws_probe("binance", "wss://nbstream.binance.com/eoptions/ws",
                                  {"method": "SUBSCRIBE", "params": [f"{bsym}@depth10@100ms"], "id": 1}, 5, 20))
        if ysym:
            tasks.append(ws_probe("bybit", "wss://stream.bybit.com/v5/public/option",
                                  {"op": "subscribe", "args": [f"tickers.{ysym}", f"orderbook.25.{ysym}"]}, 10, 20))
        await asyncio.gather(*tasks)

    try:
        asyncio.run(ws_all())
    except Exception:
        traceback.print_exc()
    (RES / "gate0_connectivity.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
