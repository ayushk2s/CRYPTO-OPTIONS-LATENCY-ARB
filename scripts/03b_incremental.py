"""Gate 1 (C): does Deribit's option quote add predictive info for Binance/Bybit quotes
beyond the underlying perp and the follower's own recent move?  Local-arrival clock (actionable).

At each 1 s sample time t (per instrument, per follower f):
  x_der  = Deribit option mid change over (t-1s, t]          (as received here)
  x_perp = Binance perp mid change over (t-1s, t] * (+1 call / -1 put)
  x_own  = follower mid change over (t-1s, t]
  y      = follower mid change over (t+d, t+d+h]   d in {0,150,250} ms, h in {1,5} s
Samples where the Binance perp is >5 s stale (ISP DNS-block outages) are dropped.
Variables standardised per series, pooled; OLS with HAC SEs, plus a circular-shift null for the
x_der coefficient (Deribit series shifted by random 60-600 s offsets -> 200 null betas).
Also: raw USD magnitude of the follower's move after 'pure' Deribit moves (perp quiet).
Output: results/gate1_incremental.csv, results/gate1_incremental.json
"""
import sys, json, glob, pathlib, importlib.util
import numpy as np, pandas as pd, statsmodels.api as sm

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ll", ROOT / "scripts" / "03_leadlag.py")
ll = importlib.util.module_from_spec(spec); spec.loader.exec_module(ll)
G = 50  # ms grid
rng = np.random.default_rng(7)


def at(s, ts):
    """value of step series s (index ms) at times ts (last obs <= t)."""
    i = np.searchsorted(s.index.values, ts, side="right") - 1
    v = np.where(i >= 0, s.values[np.clip(i, 0, None)], np.nan)
    return v


def main():
    rec = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(sorted(glob.glob(str(ROOT / "data_cache/rec_*")))[-1])
    df, off = ll.load(rec)
    clock = "local"
    keys = sorted(df[df.v == "deribit"].key.unique())
    t0 = df.t_local.min() + 60e3
    t1 = df.t_local.max() - 15e3
    ts = np.arange(t0, t1, 1000.0)
    rows, mag, out = [], [], {"rec": rec.name, "hours": (t1 - t0) / 3.6e6}
    data = {}
    for f in ("binance", "bybit"):
        parts = []
        for k in keys:
            D = ll.series(df, "deribit", k, "mid", clock, "q")
            F = ll.series(df, f, k, "mid", clock, "q")
            P = ll.series(df, "binance_perp", f"{k[:3]}-PERP", "mid", clock)
            if min(len(D), len(F), len(P)) < 50:
                continue
            sg = 1.0 if k.endswith("-C") else -1.0
            d = pd.DataFrame({"t": ts, "key": k,
                              "x_der": at(D, ts) - at(D, ts - 1000),
                              "x_perp": sg * (at(P, ts) - at(P, ts - 1000)),
                              "x_own": at(F, ts) - at(F, ts - 1000)})
            for dl in (0, 150, 250):
                for h in (1000, 5000):
                    d[f"y_{dl}_{h}"] = at(F, ts + dl + h) - at(F, ts + dl)
            # outage mask: Binance perp (same DNS-blocked host as Binance options) must be < 5 s stale
            pi = np.searchsorted(P.index.values, ts, side="right") - 1
            d["perp_age"] = np.where(pi >= 0, ts - P.index.values[np.clip(pi, 0, None)], np.inf)
            d["spread"] = at(ll.series(df, f, k, "ask", clock, "q"), ts) - at(ll.series(df, f, k, "bid", clock, "q"), ts)
            d["S"] = at(P, ts)
            d = d[d.perp_age < 5000].dropna()
            parts.append((d, D, ts))
        data[f] = parts

    for f, parts in data.items():
        def pooled(parts_, shift=None):
            zs = []
            for d, D, ts_ in parts_:
                d = d.copy()
                if shift is not None:
                    tt = d.t.values
                    s = shift % (t1 - t0 - 2000)
                    tt2 = t0 + 1000 + ((tt - t0 - 1000 + s) % (t1 - t0 - 2000))
                    d["x_der"] = at(D, tt2) - at(D, tt2 - 1000)
                cols = ["x_der", "x_perp", "x_own"] + [c for c in d.columns if c.startswith("y_")]
                sd = d[cols].std().replace(0, np.nan)
                z = d[cols] / sd
                z["t"] = d.t.values
                zs.append(z.dropna())
            return pd.concat(zs).sort_values("t")

        Z = pooled(parts)
        for yc in [c for c in Z.columns if c.startswith("y_")]:
            X = sm.add_constant(Z[["x_der", "x_perp", "x_own"]])
            m = sm.OLS(Z[yc], X).fit(cov_type="HAC", cov_kwds={"maxlags": 5})
            # univariate (Deribit only) for comparison
            mu = sm.OLS(Z[yc], sm.add_constant(Z[["x_der"]])).fit(cov_type="HAC", cov_kwds={"maxlags": 5})
            r = {"follower": f, "target": yc, "n": int(m.nobs),
                 "b_der_alone": round(mu.params["x_der"], 4), "t_der_alone": round(mu.tvalues["x_der"], 2),
                 "b_der": round(m.params["x_der"], 4), "t_der": round(m.tvalues["x_der"], 2),
                 "b_perp": round(m.params["x_perp"], 4), "t_perp": round(m.tvalues["x_perp"], 2),
                 "b_own": round(m.params["x_own"], 4), "t_own": round(m.tvalues["x_own"], 2),
                 "R2": round(m.rsquared, 4)}
            rows.append(r)
        # circular-shift null for the key target (d=150ms, h=5s) partial coefficient
        key_y = "y_150_5000"
        null = []
        for _ in range(200):
            Zs = pooled(parts, shift=rng.uniform(60e3, 600e3))
            mm = sm.OLS(Zs[key_y], sm.add_constant(Zs[["x_der", "x_perp", "x_own"]])).fit()
            null.append(mm.params["x_der"])
        obs = [r for r in rows if r["follower"] == f and r["target"] == key_y][0]["b_der"]
        null = np.array(null)
        out[f"{f}_shift_null"] = {"target": key_y, "obs_b_der": obs, "null_mean": float(null.mean()),
                                  "null_sd": float(null.std()), "p_two_sided": float((np.abs(null) >= abs(obs)).mean())}

        # USD magnitude: 'pure' Deribit moves (|x_der| >= 1 Deribit tick-ish, perp quiet) -> follower move
        allp = pd.concat([d for d, _, _ in parts])
        # Deribit ticks: 0.0001 BTC (~$8) / 0.0005 ETH (~$1.2); one side ticking moves mid by half that
        for coin, dtick, pq in (("BTC", 4.0, 0.5e-4), ("ETH", 0.6, 0.5e-4)):
            a = allp[allp.key.str.startswith(coin)]
            ev = a[(a.x_der.abs() >= dtick) & (a.x_perp.abs() <= pq * a.S)]
            for yc in ("y_150_1000", "y_150_5000"):
                sgn = np.sign(ev.x_der)
                v = (ev[yc] * sgn).values
                if len(v) >= 5:
                    bs = [rng.choice(v, len(v)).mean() for _ in range(2000)]
                    mag.append({"follower": f, "coin": coin, "target": yc, "n_events": len(v),
                                "mean_signed_move_usd": round(float(v.mean()), 3),
                                "ci95": [round(float(np.percentile(bs, 2.5)), 3), round(float(np.percentile(bs, 97.5)), 3)],
                                "median_spread_usd": round(float(ev.spread.median()), 3),
                                "taker_fee_per_side_usd": round(float(3e-4 * ev.S.median()), 3)})

    res = pd.DataFrame(rows)
    res.to_csv(ROOT / "results" / "gate1_incremental.csv", index=False)
    out["regressions"] = rows
    out["pure_deribit_move_magnitude"] = mag
    (ROOT / "results" / "gate1_incremental.json").write_text(json.dumps(out, indent=1, default=float))
    pd.set_option("display.width", 250)
    print(res.to_string())
    print(json.dumps({k: v for k, v in out.items() if "null" in k}, indent=1))
    print(pd.DataFrame(mag).to_string())


if __name__ == "__main__":
    main()
