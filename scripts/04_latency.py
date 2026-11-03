"""Gate 2: round-trip latency from this machine to the options order gateways (no orders, no keys).

Times warm keep-alive public GETs on the same hosts that serve the options order endpoints
(eapi.binance.com -> POST /eapi/v1/order; api.bybit.com -> POST /v5/order/create). This is a LOWER
bound on order RTT: real orders add signing, risk checks and matching-engine time.
Also times Deribit (signal source) for reference.
Output: results/gate2_latency.json
"""
import json, time, pathlib, statistics
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
N = 200
targets = {
    "binance_eapi_ping": "https://eapi.binance.com/eapi/v1/ping",
    "binance_eapi_time": "https://eapi.binance.com/eapi/v1/time",
    "bybit_v5_time": "https://api.bybit.com/v5/market/time",
    "deribit_time": "https://www.deribit.com/api/v2/public/get_time",
}
out = {}
for name, url in targets.items():
    s = requests.Session()
    s.get(url, timeout=10)  # warm TCP+TLS
    xs = []
    for _ in range(N):
        t0 = time.perf_counter()
        try:
            s.get(url, timeout=10)
            xs.append((time.perf_counter() - t0) * 1000)
        except Exception:
            pass
        time.sleep(0.25)  # well inside public rate limits
    xs.sort()
    out[name] = {"n": len(xs), "min": round(xs[0], 1), "p5": round(xs[len(xs) // 20], 1),
                 "p50": round(statistics.median(xs), 1), "p95": round(xs[int(len(xs) * .95)], 1),
                 "max": round(xs[-1], 1)}
    print(name, out[name], flush=True)
(ROOT / "results" / "gate2_latency.json").write_text(json.dumps({"ts": time.time(), "rtt_ms": out}, indent=1))
