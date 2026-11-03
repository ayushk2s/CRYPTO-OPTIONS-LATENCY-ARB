"""Gate 1: does Binance/Bybit lag Deribit? Lead-lag analysis on a recording from 02_record.py.

usage: python scripts/03_leadlag.py [rec_dir]  (default: latest data_cache/rec_*)

Two clocks:
  'exch'  exchange event timestamp te (exchanges agree with each other to ~20 ms; see clock.jsonl)
  'local' local receive time shifted onto exchange clock by the median measured offset
          -> what this machine actually sees (includes each venue's network path)
Two methods:
  (A) cross-correlation of 50 ms-binned changes, lags -10s..+10s, pooled over instruments
  (B) event study: on a leader-venue mid move, cumulative follower-mid move from -5s..+10s,
      normalised by the follower's total move over [-5s, +10s]
Outputs: results/gate1_xcorr.csv, results/gate1_events.csv, results/gate1_summary.json
"""
import sys, json, glob, pathlib
import numpy as np, pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
RES = ROOT / "results"
BIN = 50  # ms
LAGS = np.arange(-200, 201)  # bins -> -10s..+10s
VEN = ["deribit", "binance", "bybit"]


def load(rec):
    df = pd.read_json(rec / "ticks.jsonl", lines=True, dtype=False)
    df = df[df.kind.isin(["q", "m"])].copy()
    clk = [json.loads(l) for l in open(rec / "clock.jsonl")]
    off = np.median([c[v]["off_ms"] for c in clk for v in VEN if "off_ms" in c.get(v, {})])
    df["t_exch"] = df.te.astype(float)
    df["t_local"] = df.tr / 1e6 + off
    df["mid"] = np.where(df.bid.notna() & df.ask.notna(), (df.bid + df.ask) / 2, np.nan)
    return df, off


def series(df, v, key, field, clock, kind=None):
    s = df[(df.v == v) & (df.key == key)]
    if kind:
        s = s[s.kind == kind]
    s = s[[f"t_{clock}", field]].dropna().sort_values(f"t_{clock}")
    return pd.Series(s[field].values, index=s[f"t_{clock}"].values)


def grid(s, t0, nb):
    """last value in each BIN-ms bin, forward-filled; returns array length nb."""
    idx = ((s.index.values - t0) // BIN).astype(int)
    ok = (idx >= 0) & (idx < nb)
    out = np.full(nb, np.nan)
    out[idx[ok]] = s.values[ok]  # later writes win -> last value in bin
    return pd.Series(out).ffill().values


def xcorr(a, b, lags):
    """corr(da[t], db[t+L]) for each L in lags; da, db are change arrays (nan-free zeros ok)."""
    a = (a - a.mean()) / (a.std() + 1e-12)
    b = (b - b.mean()) / (b.std() + 1e-12)
    n = len(a)
    out = []
    for L in lags:
        if L >= 0:
            out.append(np.dot(a[:n - L], b[L:]) / (n - L))
        else:
            out.append(np.dot(a[-L:], b[:n + L]) / (n + L))
    return np.array(out)


def main():
    rec = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(sorted(glob.glob(str(ROOT / "data_cache/rec_*")))[-1])
    df, off = load(rec)
    keys = sorted(df[df.v == "deribit"].key.unique())
    t0 = df.t_exch.min() + 60e3  # skip 1st minute (snapshots)
    t1 = df.t_exch.max() - 5e3
    nb = int((t1 - t0) // BIN)
    hours = (t1 - t0) / 3.6e6
    print(f"rec={rec.name} hours={hours:.2f} clock_offset={off:.0f}ms keys={len(keys)}")

    # per-venue one-way latency as seen here (local arrival on exch clock - exchange ts)
    lat = {}
    for v in VEN + ["binance_perp", "deribit_perp", "deribit_index"]:
        x = (df[df.v == v].t_local - df[df.v == v].t_exch).dropna()
        lat[v] = {p: round(float(np.percentile(x, q)), 1) for p, q in (("p5", 5), ("p50", 50), ("p95", 95))}
    print("apparent one-way delay (local arrival - exchange ts), ms:", lat)

    # ---- (A) cross-correlation ----
    pairs = [("deribit", "binance"), ("deribit", "bybit"), ("binance", "bybit")]
    rows = []
    summ = {"rec": rec.name, "hours": hours, "clock_offset_ms": off, "latency_ms": lat, "xcorr": {}, "events": {}}
    for clock in ("exch", "local"):
        for field, desc in (("mid", "BBO mid"), ("mark", "mark price")):
            for coin in ("BTC", "ETH"):
                ck = [k for k in keys if k.startswith(coin)]
                # leader-perp control: perp mid changes vs each venue's option mid changes (signed by delta via corr sign)
                acc = {p: [] for p in pairs + [("perp", v) for v in VEN]}
                for k in ck:
                    g = {}
                    for v in VEN:
                        kind = "q" if field == "mid" else ("m" if v != "deribit" else "q")
                        s = series(df, v, k, field, clock, kind)
                        if len(s) > 20:
                            g[v] = np.diff(grid(s, t0, nb), prepend=np.nan)
                    perp = series(df, "binance_perp", f"{coin}-PERP", "mid", clock)
                    g["perp"] = np.diff(grid(perp, t0, nb), prepend=np.nan)
                    for a, b in acc:
                        if a in g and b in g:
                            m = ~(np.isnan(g[a]) | np.isnan(g[b]))
                            if m.sum() > 1000 and np.count_nonzero(g[a][m]) > 30 and np.count_nonzero(g[b][m]) > 30:
                                c = xcorr(g[a][m], g[b][m], LAGS)
                                if a == "perp" and c[np.abs(c).argmax()] < 0:  # puts: flip sign
                                    c = -c
                                acc[(a, b)].append(c)
                for (a, b), cs in acc.items():
                    if not cs:
                        continue
                    c = np.mean(cs, axis=0)
                    pos = (c[LAGS > 0] ** 2).sum()
                    neg = (c[LAGS < 0] ** 2).sum()
                    pk = int(LAGS[np.argmax(c)]) * BIN
                    nobs = nb
                    tag = f"{clock}|{field}|{coin}|{a}->{b}"
                    r = {"tag": tag, "clock": clock, "field": field, "coin": coin, "lead": a, "follow": b,
                         "n_instr": len(cs), "peak_lag_ms": pk, "peak_corr": round(float(c.max()), 4),
                         "corr_lag0": round(float(c[LAGS == 0][0]), 4),
                         "LLR(a leads b)": round(float(pos / (neg + 1e-12)), 3),
                         "se_approx": round(1 / np.sqrt(nobs), 4)}
                    for L in (-1000, -500, -100, 100, 250, 500, 1000, 2000, 5000):
                        r[f"c@{L}ms"] = round(float(c[LAGS == L // BIN][0]), 4)
                    rows.append(r)
                    summ["xcorr"][tag] = r
    xc = pd.DataFrame(rows)
    xc.to_csv(RES / "gate1_xcorr.csv", index=False)
    pd.set_option("display.width", 300); pd.set_option("display.max_columns", 40); pd.set_option("display.max_rows", 200)
    print(xc[["tag", "n_instr", "peak_lag_ms", "peak_corr", "corr_lag0", "LLR(a leads b)", "c@-500ms", "c@100ms",
              "c@500ms", "c@1000ms"]].to_string())

    # ---- (B) event study ----
    HOR = [-5000, -1000, -500, -250, -100, 0, 100, 250, 500, 1000, 2000, 5000, 10000]
    erows = []
    for clock in ("exch", "local"):
        for lead in VEN + ["perp"]:
            for fol in VEN:
                if fol == lead:
                    continue
                resp = {h: [] for h in HOR}
                n_ev = 0
                for k in keys:
                    coin = k[:3]
                    L = (series(df, "binance_perp", f"{coin}-PERP", "mid", clock) if lead == "perp"
                         else series(df, lead, k, "mid", clock, "q"))
                    F = series(df, fol, k, "mid", clock, "q")
                    if len(L) < 50 or len(F) < 50:
                        continue
                    L = L[(L.index > t0) & (L.index < t1 - 10e3)]
                    dL = np.diff(L.values, prepend=L.values[0])
                    # leader 'move' = mid change >= 1 follower tick-equivalent; for perp: >= 3bp
                    thr = (L.values * 3e-4) if lead == "perp" else np.full(len(L), 2.5 if coin == "BTC" else 0.1)
                    ev = np.where(np.abs(dL) >= thr)[0]
                    # de-cluster: keep events >= 2s apart
                    keep, last = [], -1e18
                    for i in ev:
                        if L.index[i] - last >= 2000:
                            keep.append(i); last = L.index[i]
                    Ft, Fv = F.index.values, F.values
                    sgn_k = 1.0
                    if lead == "perp" and k.endswith("-P"):
                        sgn_k = -1.0
                    for i in keep:
                        te_ = L.index[i]
                        base_i = np.searchsorted(Ft, te_ - 5000, side="right") - 1
                        if base_i < 0:
                            continue
                        base = Fv[base_i]
                        tot_i = np.searchsorted(Ft, te_ + 10000, side="right") - 1
                        tot = (Fv[tot_i] - base) * np.sign(dL[i]) * sgn_k
                        if tot == 0:
                            continue
                        n_ev += 1
                        for h in HOR:
                            j = np.searchsorted(Ft, te_ + h, side="right") - 1
                            resp[h].append(((Fv[j] - base) * np.sign(dL[i]) * sgn_k, tot))
                if n_ev < 20:
                    continue
                r = {"clock": clock, "lead": lead, "follow": fol, "n_events": n_ev}
                for h in HOR:
                    a = np.array(resp[h])
                    # fraction of the eventual (10s) signed move already realised by horizon h (ratio of sums)
                    r[f"{h}ms"] = round(float(a[:, 0].sum() / np.abs(a[:, 1]).sum()), 3)
                erows.append(r)
                summ["events"][f"{clock}|{lead}->{fol}"] = r
    ev = pd.DataFrame(erows)
    ev.to_csv(RES / "gate1_events.csv", index=False)
    print("\nEvent study: signed follower move realised by horizon / total |move| over [-5s,+10s] (0 = none, 1 = all)")
    print(ev.to_string())
    (RES / "gate1_summary.json").write_text(json.dumps(summ, indent=1, default=float))


if __name__ == "__main__":
    main()
