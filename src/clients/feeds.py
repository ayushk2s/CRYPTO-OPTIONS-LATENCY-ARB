"""Public market-data WS feeds for Deribit / Binance Options / Bybit Options (no auth, no orders).

Each feed coroutine calls emit(record_dict) for every parsed update. Every record carries
  v      venue ('deribit'|'binance'|'bybit'|'binance_perp')
  sym    venue symbol
  key    normalized option key "BTC-20261024-81500-C" (or underlying name for perps)
  tr     local receive time, ns since epoch (time.time_ns())
  te     exchange event timestamp, ms since epoch
  bid/ask/bsz/asz  best bid/ask in USD and sizes (coins)
  mark/iv          where the venue provides them
Reconnects forever with backoff; each (re)connect is logged as kind='conn'.
"""
import asyncio, json, random, time
import websockets


def _f(x):
    try:
        x = float(x)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


async def _run(name, url, on_open, on_msg, emit, ping_interval=20, extra=None):
    """extra: dict merged into conn/disconnect events (e.g. which keys this socket carries)."""
    extra = extra or {}
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, open_timeout=15, ping_interval=ping_interval, ping_timeout=60,
                                          max_size=2**24) as ws:
                emit({"kind": "conn", "v": name, "url": url, "tr": time.time_ns(), **extra})
                backoff = 1
                await on_open(ws)
                async for m in ws:
                    on_msg(m, time.time_ns())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            emit({"kind": "disconnect", "v": name, "url": url, "tr": time.time_ns(), "err": repr(e)[:200], **extra})
        await asyncio.sleep(backoff * (0.75 + 0.5 * random.random()))
        backoff = min(backoff * 2, 60)


def deribit(instruments, keys, extra_channels, emit):
    """instruments: deribit option names; keys: same-order normalized keys."""
    kmap = dict(zip(instruments, keys))
    cur = {}
    chans = [f"ticker.{i}.100ms" for i in instruments] + extra_channels

    async def on_open(ws):
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "public/subscribe",
                                  "params": {"channels": chans}}))
        # Deribit drops idle WS without heartbeat; 'public/set_heartbeat' + respond to test_request
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "public/set_heartbeat",
                                  "params": {"interval": 30}}))
        cur['ws'] = ws

    def on_msg(m, tr):
        d = json.loads(m)
        if d.get("method") == "heartbeat" and d["params"].get("type") == "test_request":
            asyncio.ensure_future(cur['ws'].send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "public/test"})))
            return
        if d.get("method") != "subscription":
            return
        ch, x = d["params"]["channel"], d["params"]["data"]
        if ch.startswith("ticker."):
            inst = x["instrument_name"]
            if inst.endswith("PERPETUAL"):
                emit({"kind": "q", "v": "deribit_perp", "sym": inst, "key": inst.split("-")[0] + "-PERP", "tr": tr,
                      "te": x["timestamp"], "bid": x.get("best_bid_price"), "ask": x.get("best_ask_price"),
                      "mark": x.get("mark_price"), "idx": x.get("index_price")})
                return
            u = x["underlying_price"]
            bb, ba = x.get("best_bid_price") or 0, x.get("best_ask_price") or 0
            emit({"kind": "q", "v": "deribit", "sym": inst, "key": kmap[inst], "tr": tr, "te": x["timestamp"],
                  "bid": bb * u or None, "ask": ba * u or None,
                  "bsz": x.get("best_bid_amount"), "asz": x.get("best_ask_amount"),
                  "mark": x["mark_price"] * u, "iv": x["mark_iv"] / 100,
                  "biv": (x.get("bid_iv") or 0) / 100 or None, "aiv": (x.get("ask_iv") or 0) / 100 or None,
                  "S": u, "idx": x.get("index_price"), "bid_c": bb or None, "ask_c": ba or None})
        elif ch.startswith("deribit_price_index"):
            emit({"kind": "q", "v": "deribit_index", "sym": x["index_name"], "key": x["index_name"].split("_")[0].upper() + "-IDX",
                  "tr": tr, "te": x["timestamp"], "mark": x["price"]})

    return _run("deribit", "wss://www.deribit.com/ws/api/v2", on_open, on_msg, emit, extra={"keys": list(keys)})


def binance_book(sym, key, emit):
    """Per-symbol event-driven BBO. NOTE: only the lowercase symbol on /public delivers (verified Gate 0c)."""
    async def on_open(ws):
        pass

    def on_msg(m, tr):
        x = json.loads(m)
        if x.get("e") != "bookTicker":
            return
        emit({"kind": "q", "v": "binance", "sym": sym, "key": key, "tr": tr, "te": x.get("E") or x.get("T"),
              "bid": _f(x["b"]), "ask": _f(x["a"]), "bsz": _f(x["B"]), "asz": _f(x["A"]), "u": x.get("u")})

    return _run(f"binance:{sym}", f"wss://fstream.binance.com/public/ws/{sym.lower()}@bookTicker", on_open, on_msg, emit)


def binance_mark(underlying, symmap, emit):
    """1-second whole-chain mark/IV push; symmap: binance symbol -> key (filtered)."""
    async def on_open(ws):
        pass

    def on_msg(m, tr):
        arr = json.loads(m)
        if not isinstance(arr, list):
            return
        for x in arr:
            k = symmap.get(x.get("s"))
            if k:
                emit({"kind": "m", "v": "binance", "sym": x["s"], "key": k, "tr": tr, "te": x["E"],
                      "mark": _f(x["mp"]), "S": _f(x["i"]), "bid": _f(x.get("bo")), "ask": _f(x.get("ao")),
                      "bsz": _f(x.get("bq")), "asz": _f(x.get("aq")), "biv": _f(x.get("b")), "aiv": _f(x.get("a")),
                      "iv": _f(x.get("vo"))})

    return _run(f"binance_mark:{underlying}", f"wss://fstream.binance.com/market/ws/{underlying.lower()}@optionMarkPrice",
                on_open, on_msg, emit)


def binance_perp(sym, emit):
    """Perp BBO as underlying control; only emitted when bid or ask *price* changes (raw feed ~500 msg/s)."""
    last = [None]

    async def on_open(ws):
        pass

    def on_msg(m, tr):
        x = json.loads(m)
        if x.get("e") == "bookTicker" and (x["b"], x["a"]) != last[0]:
            last[0] = (x["b"], x["a"])
            emit({"kind": "q", "v": "binance_perp", "sym": sym, "key": sym[:3] + "-PERP", "tr": tr, "te": x["E"],
                  "bid": _f(x["b"]), "ask": _f(x["a"]), "bsz": _f(x["B"]), "asz": _f(x["A"])})

    return _run(f"binance_perp:{sym}", f"wss://fstream.binance.com/public/ws/{sym.lower()}@bookTicker", on_open, on_msg, emit)


def bybit(symbols, keys, emit):
    """tickers (mark/iv/bbo) + orderbook.25 (maintained locally -> BBO + top-5 depth)."""
    kmap = dict(zip(symbols, keys))
    books = {}

    async def on_open(ws):
        books.clear()
        args = [f"tickers.{s}" for s in symbols] + [f"orderbook.25.{s}" for s in symbols]
        for i in range(0, len(args), 10):
            await ws.send(json.dumps({"op": "subscribe", "args": args[i:i + 10]}))

        async def ping():
            try:
                while True:
                    await asyncio.sleep(20)
                    await ws.send(json.dumps({"op": "ping"}))
            except websockets.ConnectionClosed:
                pass
        asyncio.ensure_future(ping())

    def on_msg(m, tr):
        d = json.loads(m)
        t = d.get("topic", "")
        if t.startswith("tickers."):
            x = d["data"]
            s = x["symbol"]
            emit({"kind": "m", "v": "bybit", "sym": s, "key": kmap[s], "tr": tr, "te": d["ts"],
                  "mark": _f(x.get("markPrice")), "iv": _f(x.get("markIv")), "S": _f(x.get("underlyingPrice")),
                  "idx": _f(x.get("indexPrice")), "biv": _f(x.get("bidIv")), "aiv": _f(x.get("askIv")),
                  "tbid": _f(x.get("bidPrice")), "task": _f(x.get("askPrice"))})
        elif t.startswith("orderbook."):
            x = d["data"]
            s = x["s"]
            if d["type"] == "snapshot":
                books[s] = ({float(p): float(q) for p, q in x["b"]}, {float(p): float(q) for p, q in x["a"]})
            elif s in books:
                for side, lv in ((0, x["b"]), (1, x["a"])):
                    bk = books[s][side]
                    for p, q in lv:
                        p, q = float(p), float(q)
                        if q == 0:
                            bk.pop(p, None)
                        else:
                            bk[p] = q
            else:
                return
            b, a = books[s]
            bl = sorted(b.items(), reverse=True)[:5]
            al = sorted(a.items())[:5]
            emit({"kind": "q", "v": "bybit", "sym": s, "key": kmap[s], "tr": tr, "te": d.get("cts") or d["ts"],
                  "bid": bl[0][0] if bl else None, "ask": al[0][0] if al else None,
                  "bsz": bl[0][1] if bl else None, "asz": al[0][1] if al else None,
                  "bl": bl, "al": al})

    # Bybit uses app-level {"op":"ping"}; protocol pings time out under load (seen in smoke test)
    return _run("bybit", "wss://stream.bybit.com/v5/public/option", on_open, on_msg, emit, ping_interval=None,
                extra={"keys": list(keys)})


def binance_books(syms, keys, emit):
    """Combined-stream BBO for many symbols on one socket (lowercase names, /public host)."""
    kmap = {s.lower(): (s, k) for s, k in zip(syms, keys)}
    url = "wss://fstream.binance.com/public/stream?streams=" + "/".join(f"{s.lower()}@bookTicker" for s in syms)

    async def on_open(ws):
        pass

    def on_msg(m, tr):
        d = json.loads(m)
        x = d.get("data", d)
        if x.get("e") != "bookTicker":
            return
        sym, key = kmap.get(x["s"].lower(), (None, None))
        if key is None:
            return
        emit({"kind": "q", "v": "binance", "sym": sym, "key": key, "tr": tr, "te": x.get("E") or x.get("T"),
              "bid": _f(x["b"]), "ask": _f(x["a"]), "bsz": _f(x["B"]), "asz": _f(x["A"]), "u": x.get("u")})

    return _run(f"binance_books:{syms[0]}", url, on_open, on_msg, emit, extra={"keys": list(keys)})
