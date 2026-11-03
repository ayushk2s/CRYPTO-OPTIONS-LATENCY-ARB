"""Figures, tables and statistics for the working paper, computed from the raw research data.

usage: python paper/analysis.py [stage ...]   stages: gate0 rec gate1 paper s2 wing pm  (default: all)
Writes paper/build/figs/*.png, paper/build/tables/*.html and merges numbers into paper/build/stats.json.
Every number quoted in paper/paper.html comes from these outputs or from results/*.
"""
import sys, json, glob, pathlib, importlib.util, collections, datetime as dt, re
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

ROOT = pathlib.Path(__file__).resolve().parents[1]
RES = ROOT / "results"
OUT = ROOT / "paper" / "build"
FIG, TAB = OUT / "figs", OUT / "tables"
FIG.mkdir(parents=True, exist_ok=True); TAB.mkdir(parents=True, exist_ok=True)
REC = ROOT / "data_cache" / "rec_20261023_125702"
RUNS = {"run1": "20261023_150900", "run2": "20261102_180156"}

spec = importlib.util.spec_from_file_location("ll", ROOT / "scripts" / "03_leadlag.py")
ll = importlib.util.module_from_spec(spec); spec.loader.exec_module(ll)
sys.path.insert(0, str(ROOT))
from src.paper.engine import TAKER, EXERCISE, SETTLE_BP, CAP  # noqa: E402

# validated palette (light surface): one colour per entity, fixed everywhere
C = {"deribit": "#2563eb", "bybit": "#d97706", "binance": "#7c3aed", "perp": "#0f9d76", "cost": "#dc2626",
     "ink": "#1f2937", "muted": "#6b7280", "grid": "#e5e7eb", "null": "#9ca3af"}
NAME = {"deribit": "Deribit", "bybit": "Bybit", "binance": "Binance", "perp": "Perpetual",
        "binance_perp": "Binance perp", "deribit_perp": "Deribit perp", "deribit_index": "Deribit index"}
plt.rcParams.update({"font.family": "DejaVu Serif", "font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9,
                     "axes.edgecolor": C["muted"], "axes.linewidth": 0.6, "axes.grid": True, "grid.color": C["grid"],
                     "grid.linewidth": 0.5, "axes.spines.top": False, "axes.spines.right": False,
                     "xtick.color": C["ink"], "ytick.color": C["ink"], "legend.frameon": False,
                     "lines.linewidth": 1.6, "figure.dpi": 100, "savefig.dpi": 220, "savefig.bbox": "tight"})

STATS_F = OUT / "stats.json"
STATS = json.loads(STATS_F.read_text()) if STATS_F.exists() else {}


def put(**kw):
    STATS.update(kw)
    STATS_F.write_text(json.dumps(STATS, indent=1, default=float))


def save(fig, name):
    fig.savefig(FIG / f"{name}.png")
    plt.close(fig)
    print("fig", name)


def table(name, df, fmt=None, index=False, cls=""):
    """write an HTML table fragment; fmt: {col: format-string or callable}."""
    fmt = fmt or {}
    d = df.copy() if index is False else df.reset_index()
    rows = []
    head = "".join(f"<th>{c}</th>" for c in d.columns)
    for _, r in d.iterrows():
        cells = []
        for c in d.columns:
            v = r[c]
            f = fmt.get(c)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                s = "—"
            elif callable(f):
                s = f(v)
            elif f:
                s = f.format(v)
            else:
                s = str(v)
            num = isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, bool)
            cells.append(f'<td class="{"n" if num else ""}">{s}</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
    (TAB / f"{name}.html").write_text(f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>'
                                      + "".join(rows) + "</tbody></table>", encoding="utf-8")
    print("table", name)


def usd(x, d=2):
    return ("−" if x < 0 else "") + f"${abs(x):,.{d}f}"


def ts(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)


# ---------------------------------------------------------------- Gate 0
def gate0():
    census = json.loads((RES / "gate0_chain_census.json").read_text())["census"]
    rows = []
    for k, v in census.items():
        ven, coin = k.split(".")
        rows.append({"Exchange": NAME[ven], "Coin": coin, "Instruments": v["instruments"], "Expiries": v["expiries"],
                     "Unique strikes": v["strikes_unique"], "Two-sided books": v["two_sided"],
                     "24h volume (contracts)": v["vol24_coins"], "Nearest expiry": v["expiry_list"][0],
                     "Longest expiry": v["expiry_list"][-1]})
    cdf = pd.DataFrame(rows).sort_values(["Coin", "Exchange"])
    table("t_census", cdf, {"24h volume (contracts)": "{:,.0f}", "Instruments": "{:,}"})
    put(census={k: {"n": v["instruments"], "vol": v["vol24_coins"], "exp": v["expiries"]} for k, v in census.items()})

    sp = pd.read_csv(RES / "gate0_atm_spreads.csv")
    sp["Exchange"] = sp.venue.map(NAME)
    t = sp[["Exchange", "sym", "dte_days", "K", "cp", "bid", "ask", "spread_usd", "spread_pct_mid", "mark_iv"]].copy()
    t.columns = ["Exchange", "Instrument", "DTE (days)", "Strike", "C/P", "Bid (USD)", "Ask (USD)", "Spread (USD)",
                 "Spread (% of mid)", "Mark IV"]
    table("t_atm_spreads", t, {"DTE (days)": "{:.2f}", "Strike": "{:,.0f}", "Bid (USD)": "{:,.2f}", "Ask (USD)": "{:,.2f}",
                               "Spread (USD)": "{:,.2f}", "Spread (% of mid)": "{:.1f}%", "Mark IV": "{:.1%}"})
    # touch depth
    dep = []
    for _, r in sp[sp.book_bids.notna()].iterrows():
        b, a = json.loads(r.book_bids), json.loads(r.book_asks)
        dep.append({"Exchange": NAME[r.venue], "Instrument": r.sym, "Best bid": b[0][0], "Bid size": b[0][1],
                    "Best ask": a[0][0], "Ask size": a[0][1], "Size within 5 levels (bid)": sum(x[1] for x in b[:5]),
                    "Size within 5 levels (ask)": sum(x[1] for x in a[:5])})
    table("t_depth", pd.DataFrame(dep), {"Best bid": "{:,.2f}", "Best ask": "{:,.2f}", "Bid size": "{:,.2f}",
                                         "Ask size": "{:,.2f}", "Size within 5 levels (bid)": "{:,.1f}",
                                         "Size within 5 levels (ask)": "{:,.1f}"})
    # spread figure
    fig, axs = plt.subplots(1, 2, figsize=(7.2, 2.6))
    for ax, coin in zip(axs, ("BTC", "ETH")):
        s = sp[sp.coin == coin]
        for i, ven in enumerate(("binance", "bybit", "deribit")):
            v = s[s.venue == ven]
            ax.scatter(np.full(len(v), i) + np.linspace(-0.15, 0.15, len(v)), v.spread_pct_mid, color=C[ven], s=26,
                       zorder=3, edgecolor="white", linewidth=0.8)
        ax.set_xticks(range(3), [NAME[v] for v in ("binance", "bybit", "deribit")])
        ax.set_ylabel("bid-ask spread, % of mid"); ax.set_title(f"{coin}: near-expiry ATM options")
        ax.set_xlim(-0.6, 2.6); ax.grid(axis="x", visible=False)
    fig.tight_layout(); save(fig, "f_atm_spreads")

    con = json.loads((RES / "gate0_connectivity.json").read_text())
    rows = [{"Endpoint": k, "URL": v["url"].split("?")[0].replace("https://", ""), "HTTP": v.get("status"),
             "Latency (ms)": v.get("ms"), "Bytes": v.get("bytes")} for k, v in con["rest"].items()]
    table("t_rest", pd.DataFrame(rows), {"Latency (ms)": "{:,.0f}", "Bytes": "{:,}"})
    ws = con.get("ws", {})
    if ws:
        wrows = []
        for k, v in ws.items():
            wrows.append({"Stream": k, "URL": str(v.get("url", ""))[:70], "Messages": v.get("n", v.get("msgs")),
                          "Note": str(v.get("err") or v.get("note") or "")[:60]})
        table("t_ws", pd.DataFrame(wrows))
    probe = json.loads((RES / "gate0_binance_opt_streams.json").read_text())
    prows = [{"Stream URL": p["url"].replace("wss://", ""), "Messages received": p["n"],
              "Median gap (ms)": float(np.median(np.diff(p["dts"]))) if len(p.get("dts") or []) > 2 else None}
             for p in probe]
    table("t_binance_streams", pd.DataFrame(prows), {"Median gap (ms)": "{:,.0f}"})
    probe2 = json.loads((RES / "gate0_binance_ws_probe.json").read_text())
    table("t_binance_probe", pd.DataFrame([{"Probe": p["name"], "URL": p["url"].replace("wss://", ""),
                                            "Messages": len(p.get("msgs") or [])} for p in probe2]))
    lat = json.loads((RES / "gate2_latency.json").read_text())["rtt_ms"]
    table("t_latency", pd.DataFrame([{"Endpoint": k, **v} for k, v in lat.items()]),
          {c: "{:,.1f}" for c in ("min", "p5", "p50", "p95", "max")})
    put(latency=lat)


# ---------------------------------------------------------------- recording (data + Gate 1)
def load_rec():
    df, off = ll.load(REC)
    conn = [json.loads(l) for l in open(REC / "ticks.jsonl") if '"kind":"conn"' in l or '"kind":"disconnect"' in l]
    return df, off, conn


def outages(df, gap_ms=60000):
    """Binance-perp silences > 60 s (the perp only emits on price change, so short quiet gaps are normal)."""
    p = np.sort(df[df.v == "binance_perp"].t_exch.values)
    d = np.diff(p)
    i = np.where(d > gap_ms)[0]
    return [(p[j], p[j + 1]) for j in i]


def rec():
    df, off, conn = load_rec()
    t0, t1 = df.t_exch.min(), df.t_exch.max()
    hours = (t1 - t0) / 3.6e6
    outs = outages(df)
    out_min = sum(b - a for a, b in outs) / 6e4
    put(rec={"hours": hours, "start": str(ts(t0)), "end": str(ts(t1)), "n_records": len(df), "clock_offset_ms": off,
             "outages": [(str(ts(a)), (b - a) / 6e4) for a, b in outs], "outage_min": out_min,
             "n_conn_events": len(conn)})
    # data summary
    g = df.groupby(["v", "kind"]).agg(n=("te", "size"), instruments=("key", "nunique"))
    g["per_sec"] = g.n / ((t1 - t0) / 1e3)
    g = g.reset_index()
    g["v"] = g.v.map(lambda x: NAME.get(x, x))
    g.columns = ["Source", "Record kind", "Records", "Instruments / series", "Records per second"]
    table("t_rec_summary", g, {"Records": "{:,}", "Records per second": "{:,.2f}"})
    cev = collections.Counter((c["v"].split(":")[0], c["kind"]) for c in conn)
    table("t_conn", pd.DataFrame([{"Socket": k[0], "Event": k[1], "Count": n} for k, n in sorted(cev.items())]))
    if outs:
        table("t_outages", pd.DataFrame([{"Start (UTC)": f"{ts(a):%H:%M:%S}", "End (UTC)": f"{ts(b):%H:%M:%S}",
                                          "Minutes": (b - a) / 6e4} for a, b in outs]), {"Minutes": "{:.1f}"})

    # clock offsets
    clk = [json.loads(l) for l in open(REC / "clock.jsonl")]
    fig, axs = plt.subplots(1, 2, figsize=(7.2, 2.5))
    for v in ("deribit", "binance", "bybit"):
        tt = [ts(c["tr"] / 1e6) for c in clk if "off_ms" in c.get(v, {})]
        ov = pd.Series([c[v]["off_ms"] for c in clk if "off_ms" in c.get(v, {})])
        axs[0].plot(tt, ov, color=C[v], lw=0.4, alpha=0.35)
        axs[0].plot(tt, ov.rolling(9, center=True, min_periods=3).median(), color=C[v], label=NAME[v] + " (rolling median)", lw=1.3)
        axs[1].plot(tt, [c[v]["rtt_ms"] for c in clk if "rtt_ms" in c.get(v, {})], color=C[v], label=NAME[v], lw=0.8)
    axs[0].set_ylabel("exchange − local clock (ms)"); axs[0].set_title("Local clock offset"); axs[0].set_ylim(-200, 1500)
    axs[1].set_ylabel("REST round trip (ms)"); axs[1].set_title("Round trip during recording"); axs[1].set_ylim(0, 800)
    for ax in axs:
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M"))
    axs[0].legend(fontsize=8)
    fig.tight_layout(); save(fig, "f_clock")
    offs = {v: [c[v]["off_ms"] for c in clk if "off_ms" in c.get(v, {})] for v in ("deribit", "binance", "bybit")}
    put(clock={v: {"p5": float(np.percentile(x, 5)), "p95": float(np.percentile(x, 95)), "median": float(np.median(x))}
               for v, x in offs.items()})

    # apparent feed delay
    rows, fig = [], plt.figure(figsize=(7.2, 2.6))
    ax = fig.add_subplot(111)
    for v in ("deribit", "binance", "bybit", "binance_perp", "deribit_perp", "deribit_index"):
        x = (df[df.v == v].t_local - df[df.v == v].t_exch).dropna().values
        if len(x) < 100:
            continue
        rows.append({"Feed": NAME[v], "Records": len(x), "p5": np.percentile(x, 5), "p25": np.percentile(x, 25),
                     "p50": np.percentile(x, 50), "p75": np.percentile(x, 75), "Share > 2 s": (x > 2000).mean()})
        if v in ("deribit", "binance", "bybit"):
            ax.hist(x, bins=150, range=(-800, 2200), histtype="step", color=C[v], label=NAME[v], density=True, lw=1.2)
    fd = pd.DataFrame(rows)
    bb = fd.set_index("Feed").p50
    fd["p50 minus Bybit p50"] = fd.p50 - bb["Bybit"]
    table("t_feed_delay", fd, {c: "{:,.0f}" for c in ("p5", "p25", "p50", "p75", "p50 minus Bybit p50")}
          | {"Records": "{:,}", "Share > 2 s": "{:.1%}"})
    ax.set_xlabel("local arrival − exchange timestamp (ms, after median clock correction)"); ax.set_ylabel("density")
    ax.set_title("Apparent one-way feed delay, option quotes"); ax.legend()
    save(fig, "f_feed_delay")
    put(feed_delay={r["Feed"]: {"p50": r["p50"], "p75": r["p75"], "gt2s": r["Share > 2 s"]} for r in rows},
        deribit_minus_bybit_ms=float(bb["Deribit"] - bb["Bybit"]), deribit_minus_binance_ms=float(bb["Deribit"] - bb["Binance"]))

    # spreads across the recording
    q = df[(df.kind == "q") & df.v.isin(["deribit", "binance", "bybit"]) & df.bid.notna() & df.ask.notna()].copy()
    q["coin"] = q.key.str[:3]
    q["spread"] = q.ask - q.bid
    q["spct"] = q.spread / q.mid * 100
    srows = []
    for (v, c), x in q.groupby(["v", "coin"]):
        srows.append({"Exchange": NAME[v], "Coin": c, "Quotes": len(x), "Median spread (USD)": x.spread.median(),
                      "p10": x.spread.quantile(.1), "p90": x.spread.quantile(.9),
                      "Median spread (% of mid)": x.spct.median(), "Median mid (USD)": x.mid.median()})
    table("t_rec_spreads", pd.DataFrame(srows), {"Quotes": "{:,}", "Median spread (USD)": "{:,.2f}", "p10": "{:,.2f}",
                                                 "p90": "{:,.2f}", "Median spread (% of mid)": "{:.2f}%",
                                                 "Median mid (USD)": "{:,.2f}"})
    put(rec_spreads=srows)
    fig, axs = plt.subplots(1, 2, figsize=(7.2, 2.7))
    for ax, coin in zip(axs, ("BTC", "ETH")):
        data = [q[(q.v == v) & (q.coin == coin)].spct.clip(upper=40).values for v in ("binance", "bybit", "deribit")]
        bp = ax.boxplot(data, widths=0.5, showfliers=False, patch_artist=True, medianprops={"color": "white", "lw": 1.5})
        for patch, v in zip(bp["boxes"], ("binance", "bybit", "deribit")):
            patch.set_facecolor(C[v]); patch.set_edgecolor(C[v])
        ax.set_xticks([1, 2, 3], ["Binance", "Bybit", "Deribit"]); ax.set_ylabel("spread, % of mid")
        ax.set_title(f"{coin}: all quotes over 4 h (12 options)"); ax.grid(axis="x", visible=False)
    fig.tight_layout(); save(fig, "f_rec_spreads")

    # update intensity per minute with outage shading
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    for v in ("bybit", "binance", "deribit", "binance_perp"):
        x = df[(df.v == v) & (df.kind == "q")].t_exch
        m = (x // 6e4).value_counts().sort_index()
        ax.plot([ts(i * 6e4) for i in m.index], m.values, color=C.get(v, C["perp"]), lw=1,
                label=NAME[v] + (" (price changes)" if v == "binance_perp" else ""))
    for a, b in outs:
        ax.axvspan(ts(a), ts(b), color=C["cost"], alpha=0.08, lw=0)
    ax.set_yscale("log"); ax.set_ylabel("quote updates per minute"); ax.legend(fontsize=7, ncol=4, loc="lower left")
    ax.set_title("Message intensity during the Gate 1 recording (shaded: Binance DNS outages, masked)")
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M"))
    save(fig, "f_intensity")

    # per-instrument quote counts
    pi = q.groupby(["key", "v"]).size().unstack(fill_value=0)
    pi.columns = [NAME[c] for c in pi.columns]
    table("t_per_instrument", pi, {c: "{:,}" for c in pi.columns}, index=True)

    # ---- lead-lag curves on the clean pre-outage window (exchange clock and local clock)
    w0 = t0 + 60e3
    w1 = outs[0][0] if outs else t1 - 5e3
    put(clean_window_min=(w1 - w0) / 6e4)
    keys = sorted(df[df.v == "deribit"].key.unique())
    sub = df[(df.t_exch >= w0 - 5e3) & (df.t_exch <= w1 + 5e3)]
    curves = {}
    for clock in ("exch", "local"):
        nb = int((w1 - w0) // ll.BIN)
        for coin in ("BTC", "ETH"):
            acc = collections.defaultdict(list)
            for k in [k for k in keys if k.startswith(coin)]:
                g = {}
                for v in ("deribit", "binance", "bybit"):
                    s = ll.series(sub, v, k, "mid", clock, "q")
                    if len(s) > 20:
                        g[v] = np.diff(ll.grid(s, w0, nb), prepend=np.nan)
                g["perp"] = np.diff(ll.grid(ll.series(sub, "binance_perp", f"{coin}-PERP", "mid", clock), w0, nb), prepend=np.nan)
                for a, b in (("perp", "deribit"), ("perp", "binance"), ("perp", "bybit"),
                             ("deribit", "binance"), ("deribit", "bybit"), ("binance", "bybit")):
                    if a in g and b in g:
                        m = ~(np.isnan(g[a]) | np.isnan(g[b]))
                        if m.sum() > 1000 and np.count_nonzero(g[a][m]) > 30 and np.count_nonzero(g[b][m]) > 30:
                            c = ll.xcorr(g[a][m], g[b][m], ll.LAGS)
                            if a == "perp" and c[np.abs(c).argmax()] < 0:
                                c = -c
                            acc[(a, b)].append(c)
            for p, cs in acc.items():
                curves[(clock, coin) + p] = np.mean(cs, axis=0)
    lags_ms = ll.LAGS * ll.BIN
    sel = np.abs(lags_ms) <= 2000
    for clock in ("exch", "local"):
        fig, axs = plt.subplots(2, 2, figsize=(7.2, 5.0), sharex=True)
        for j, coin in enumerate(("BTC", "ETH")):
            for v in ("deribit", "binance", "bybit"):
                c = curves.get((clock, coin, "perp", v))
                if c is not None:
                    axs[0, j].plot(lags_ms[sel], c[sel], color=C[v], label=f"perp → {NAME[v]}")
            for (a, b), col in ((("deribit", "binance"), C["binance"]), (("deribit", "bybit"), C["bybit"]),
                                (("binance", "bybit"), C["muted"])):
                c = curves.get((clock, coin, a, b))
                if c is not None:
                    axs[1, j].plot(lags_ms[sel], c[sel], color=col, label=f"{NAME[a]} → {NAME[b]}",
                                   ls="--" if a == "binance" else "-")
            for i in (0, 1):
                axs[i, j].axvline(0, color=C["ink"], lw=0.6)
                axs[i, j].set_title(f"{coin}: " + ("perpetual leading options" if i == 0 else "option exchange pairs"))
            axs[1, j].set_xlabel("lag L (ms): corr(Δleader[t], Δfollower[t+L])")
        axs[0, 0].set_ylabel("mean cross-correlation"); axs[1, 0].set_ylabel("mean cross-correlation")
        axs[0, 0].legend(fontsize=7); axs[1, 0].legend(fontsize=7)
        fig.suptitle(("Exchange" if clock == "exch" else "Local-arrival") + " clock, 50 ms bins, clean pre-outage window",
                     fontsize=9, y=1.0)
        fig.tight_layout(); save(fig, f"f_xcorr_{clock}")
    xrows = []
    for (clock, coin, a, b), c in sorted(curves.items()):
        pos, neg = (c[ll.LAGS > 0] ** 2).sum(), (c[ll.LAGS < 0] ** 2).sum()
        xrows.append({"Clock": clock, "Coin": coin, "Leader → follower": f"{NAME[a]} → {NAME[b]}",
                      "Peak lag (ms)": int(lags_ms[np.argmax(c)]), "Peak corr": c.max(),
                      "Corr at 0": c[ll.LAGS == 0][0], "LLR (leader leads)": pos / (neg + 1e-12)})
    table("t_xcorr", pd.DataFrame(xrows), {"Peak corr": "{:.3f}", "Corr at 0": "{:.3f}", "LLR (leader leads)": "{:.2f}"})
    put(xcorr_recomputed=xrows)


def gate1():
    ev = pd.read_csv(RES / "gate1_events.csv")
    hz = [c for c in ev.columns if c.endswith("ms")]
    hv = [int(c[:-2]) for c in hz]
    fig, axs = plt.subplots(1, 2, figsize=(7.2, 2.8), sharey=True)
    for ax, clock in zip(axs, ("exch", "local")):
        for _, r in ev[(ev.clock == clock) & (ev.lead == "deribit")].iterrows():
            ax.plot(range(len(hv)), r[hz].values.astype(float), marker="o", ms=3, color=C[r.follow], label=f"Deribit → {NAME[r.follow]}")
        for _, r in ev[(ev.clock == clock) & (ev.follow == "deribit")].iterrows():
            ax.plot(range(len(hv)), r[hz].values.astype(float), marker="s", ms=3, ls="--", color=C[r.lead], alpha=0.8,
                    label=f"{NAME[r.lead]} → Deribit")
        ax.axvline(hv.index(0), color=C["ink"], lw=0.6)
        ax.set_xticks(range(len(hv)), [f"{h / 1000:g}" for h in hv], fontsize=7, rotation=0)
        ax.set_title(("Exchange" if clock == "exch" else "Local-arrival") + " clock")
        ax.set_xlabel("seconds after leader move (non-linear axis)")
    axs[0].set_ylabel("share of eventual signed move realised"); axs[0].legend(fontsize=7)
    fig.tight_layout(); save(fig, "f_event_study")
    ev2 = ev.copy()
    ev2["lead"], ev2["follow"] = ev2.lead.map(NAME), ev2.follow.map(NAME)
    ev2["Done at 0 / done at +10 s"] = ev2["0ms"] / ev2["10000ms"]
    table("t_events", ev2[["clock", "lead", "follow", "n_events", "-1000ms", "0ms", "500ms", "1000ms", "5000ms", "10000ms",
                           "Done at 0 / done at +10 s"]].rename(columns={"clock": "Clock", "lead": "Leader", "follow": "Follower",
                                                                         "n_events": "Events"}),
          {c: "{:.3f}" for c in ("-1000ms", "0ms", "500ms", "1000ms", "5000ms", "10000ms")} | {"Done at 0 / done at +10 s": "{:.0%}"})
    loc = ev[(ev.clock == "local") & (ev.lead == "deribit")]
    put(event_done_at_arrival={r.follow: float(r["0ms"] / r["10000ms"]) for _, r in loc.iterrows()})

    inc = pd.read_csv(RES / "gate1_incremental.csv")
    inc["Target"] = inc.target.map(lambda s: "Δ follower over (t+{}ms, t+{}+{}s]".format(*[s.split("_")[1]] * 2, int(s.split("_")[2]) // 1000))
    t = inc[["follower", "Target", "n", "b_der", "t_der", "b_der_alone", "t_der_alone", "b_perp", "t_perp", "b_own", "t_own", "R2"]]
    t = t.rename(columns={"follower": "Follower", "n": "N", "b_der": "β Deribit", "t_der": "t", "b_der_alone": "β Deribit (alone)",
                          "t_der_alone": "t (alone)", "b_perp": "β perp", "t_perp": "t perp", "b_own": "β own", "t_own": "t own"})
    t["Follower"] = t.Follower.map(NAME)
    table("t_regression", t, {"N": "{:,}", "R2": "{:.4f}"} | {c: "{:.4f}" for c in ("β Deribit", "β Deribit (alone)", "β perp", "β own")}
          | {c: "{:.2f}" for c in ("t", "t (alone)", "t perp", "t own")})
    j = json.loads((RES / "gate1_incremental.json").read_text())
    put(regression={"t_min": float(inc.t_der.min()), "t_max": float(inc.t_der.max()), "n": int(inc.n.max()), "hours": j["hours"],
                    "null": {f: j[f"{f}_shift_null"] for f in ("binance", "bybit")}})
    mg = pd.DataFrame(j["pure_deribit_move_magnitude"])
    mg["Follower"] = mg.follower.map(NAME)
    mg["Horizon"] = mg.target.map(lambda s: f"{int(s.split('_')[2]) // 1000} s")
    mg["95% CI"] = mg.ci95.map(lambda c: f"[{c[0]:.2f}, {c[1]:.2f}]")
    mg["Round-trip fees"] = mg.taker_fee_per_side_usd * 2 * mg.coin.map({"BTC": 1, "ETH": 1})
    mg["Move / (fees + spread)"] = mg.mean_signed_move_usd / (mg["Round-trip fees"] + mg.median_spread_usd)
    table("t_magnitude", mg[["Follower", "coin", "Horizon", "n_events", "mean_signed_move_usd", "95% CI", "median_spread_usd",
                             "Round-trip fees", "Move / (fees + spread)"]].rename(
        columns={"coin": "Coin", "n_events": "Events", "mean_signed_move_usd": "Mean move (USD)", "median_spread_usd": "Median spread (USD)"}),
        {"Mean move (USD)": "{:.3f}", "Median spread (USD)": "{:.2f}", "Round-trip fees": "{:.2f}", "Move / (fees + spread)": "{:.1%}"})
    # null distribution figure (normal approximation from 200 circular shifts: mean, sd)
    from scipy.stats import norm
    fig, axs = plt.subplots(1, 2, figsize=(7.2, 2.4))
    for ax, f in zip(axs, ("binance", "bybit")):
        nl = j[f"{f}_shift_null"]
        x = np.linspace(-0.04, max(0.06, nl["obs_b_der"] * 1.2), 400)
        ax.fill_between(x, norm.pdf(x, nl["null_mean"], nl["null_sd"]), color=C["null"], alpha=0.5, lw=0,
                        label="circular-shift null (200 draws, normal fit)")
        ax.axvline(nl["obs_b_der"], color=C[f], lw=2, label=f"observed β = {nl['obs_b_der']:.4f}")
        z = (nl["obs_b_der"] - nl["null_mean"]) / nl["null_sd"]
        ax.set_title(f"{NAME[f]}: z = {z:.1f}, 0/200 shifts as extreme"); ax.set_xlabel("standardised Deribit coefficient")
        ax.legend(fontsize=7, loc="upper left"); ax.set_yticks([])
    fig.tight_layout(); save(fig, "f_null")

    # magnitude vs cost figure
    fig, ax = plt.subplots(figsize=(7.2, 2.3))
    b = mg[mg.coin == "BTC"]
    labels, vals, cols = [], [], []
    for _, r in b.iterrows():
        labels.append(f"{r.Follower} drift, {r.Horizon}"); vals.append(r.mean_signed_move_usd); cols.append(C["perp"])
    labels += ["Spread (one crossing)", "Taker fees, round trip", "Total cost"]
    fee = float(b["Round-trip fees"].iloc[0]); sp = float(b.median_spread_usd.iloc[0])
    vals += [sp, fee, fee + sp]; cols += ["#f09a8d", C["cost"], C["cost"]]
    y = np.arange(len(vals))[::-1]
    ax.barh(y, vals, color=cols, height=0.62)
    for yy, v in zip(y, vals):
        ax.text(v + 0.6, yy, f"${v:,.2f}", va="center", fontsize=8)
    ax.set_yticks(y, labels); ax.set_xlabel("USD per BTC contract"); ax.grid(axis="y", visible=False)
    ax.set_title("What the Deribit lag is worth vs what it costs to trade (BTC)")
    save(fig, "f_value_vs_cost")


# ---------------------------------------------------------------- Gate 3 paper runs
COLS = ["strat", "var", "key", "venue", "side", "qty", "px_in", "px_out", "t_in", "t_out", "gross", "slip", "fees", "net", "S",
        "coin", "t_local"]


def load_trades(run):
    p = ROOT / "data_cache" / "paper" / RUNS[run] / "trades.jsonl"
    cache = OUT / f"trades_{run}.pkl"
    if cache.exists() and cache.stat().st_mtime > p.stat().st_mtime:
        return pd.read_pickle(cache)
    rows = []
    with open(p) as fh:
        for l in fh:
            if '"S1"' in l:
                t = json.loads(l)
                rows.append([t.get(c) for c in COLS])
    d = pd.DataFrame(rows, columns=COLS)
    _, d["f"], d["hold"], d["lat"], d["mode"] = zip(*d["var"].str.split("|"))
    d["hold_s"] = d.hold.str[1:].astype(int) // 1000
    d["K"] = d.key.str.split("-").str[2].astype(float)
    d["cp"] = d.key.str[-1]
    d["exp"] = d.key.str.split("-").str[1]
    d.to_pickle(cache)
    return d


def paper():
    rep = {r: json.loads((RES / "paper" / RUNS[r] / "report.json").read_text()) for r in RUNS}
    meta = {r: json.loads((RES / "paper" / RUNS[r] / "meta.json").read_text()) for r in RUNS}
    put(runs={r: {"hours": rep[r]["hours"], "binance_cov": rep[r]["binance_coverage"], "p": rep[r]["p_usdt_end"],
                  "spot": {c: m["spot"] for c, m in meta[r]["meta"].items()},
                  "expiries": {c: [e["expiry"] for e in m["expiries"]] for c, m in meta[r]["meta"].items()},
                  "strikes": {c: m["expiries"][0]["strikes"] for c, m in meta[r]["meta"].items()}} for r in RUNS})
    for r in RUNS:
        rows = []
        for v, s in sorted(rep[r]["variants"].items()):
            if not v.startswith("S1"):
                continue
            _, f, h, lat, mode = v.split("|")
            rows.append({"Exchange": NAME[f], "Hold": f"{int(h[1:]) // 1000} s", "Latency": lat, "Direction": mode, "Trades": s["n"],
                         "Gross/trade": s["mean_gross"], "Fees/trade": s["mean_fees"], "Slip/trade": s["slip"] / s["n"],
                         "Net/trade": s["mean_net"], "95% CI (cluster bootstrap)": f"[{s['ci95_mean_net'][0]:.2f}, {s['ci95_mean_net'][1]:.2f}]",
                         "Win rate": s["win_rate"], "Total net": s["net"]})
        table(f"t_variants_{r}", pd.DataFrame(rows), {"Trades": "{:,}", "Gross/trade": usd, "Fees/trade": usd, "Slip/trade": usd,
                                                      "Net/trade": usd, "Win rate": "{:.3%}", "Total net": lambda x: usd(x, 0)}, cls="small")
        sv = rep[r]["s1_signal_vs_null"]
        srows = []
        for v, s in sorted(sv.items()):
            _, f, h, lat, _ = v.split("|")
            srows.append({"Exchange": NAME[f], "Hold": f"{int(h[1:]) // 1000} s", "Latency": lat, "Signal gross/trade": s["mean_gross_signal"],
                          "Random gross/trade": s["mean_gross_null"], "Difference": s["diff"],
                          "Permutation p (one-sided)": s["perm_p_one_sided"]})
        table(f"t_signal_null_{r}", pd.DataFrame(srows), {"Signal gross/trade": usd, "Random gross/trade": usd,
                                                          "Difference": usd, "Permutation p (one-sided)": "{:.3f}"})
        put(**{f"signal_null_{r}": srows})
        s2r = [{"Buy → sell": k.replace("->", " → ").title(), "Crossed after USDT basis": v["quote_updates_crossed_after_basis"],
                "Killed by costs": v["crossed_but_killed_by_costs"], "Signals": v["signals"], "No-fill": v["nofill"]}
               for k, v in rep[r]["s2"].items() if isinstance(v, dict) and "signals" in v]
        table(f"t_s2_counts_{r}", pd.DataFrame(s2r), {c: "{:,}" for c in ("Crossed after USDT basis", "Killed by costs")})
        put(**{f"s2_counts_{r}": s2r})

    D = {r: load_trades(r) for r in RUNS}
    # per-coin decomposition, real latency, signal
    rows = []
    for r, d in D.items():
        x = d[(d.lat == "real") & (d["mode"] == "signal")]
        for (f, h, coin), g in x.groupby(["f", "hold_s", "coin"]):
            q = g.qty.values
            prem = g.px_in.values
            fee_full = TAKER * g.S.values * q * 2
            rows.append({"Run": r[-1], "Exchange": NAME[f], "Hold": f"{h} s", "Coin": coin, "Trades": len(g),
                         "Avg qty": q.mean(), "Avg premium (USD)": prem.mean(), "Gross/trade": g.gross.mean(),
                         "Fees/trade": g.fees.mean(), "Fees if uncapped": fee_full.mean(), "Slip/trade": g.slip.mean(),
                         "Net/trade": g.net.mean(), "Wins": int((g.net > 0).sum())})
    pc = pd.DataFrame(rows)
    table("t_per_coin", pc, {"Trades": "{:,}", "Avg qty": "{:.0f}", "Avg premium (USD)": "{:,.2f}", "Gross/trade": usd,
                             "Fees/trade": usd, "Fees if uncapped": usd, "Slip/trade": usd, "Net/trade": usd}, cls="small")
    put(per_coin=rows)
    tot = {r: d[(d.lat == "real") & (d["mode"] == "signal")] for r, d in D.items()}
    put(total_trades={r: int(len(x)) for r, x in tot.items()}, total_wins={r: int((x.net > 0).sum()) for r, x in tot.items()},
        max_net={r: float(x.net.max()) for r, x in tot.items()})

    # gross P&L distribution, signal vs null, BTC 1 s Bybit (run 1)
    fig, axs = plt.subplots(1, 2, figsize=(7.2, 2.6))
    for ax, f in zip(axs, ("bybit", "binance")):
        d = D["run1"]
        for mode, col in (("signal", C[f]), ("null", C["null"])):
            g = d[(d.f == f) & (d.hold_s == 1) & (d.lat == "real") & (d["mode"] == mode) & (d.coin == "BTC")]
            ax.hist(g.gross.clip(-60, 40), bins=np.arange(-60, 41, 2.5), histtype="step" if mode == "null" else "stepfilled",
                    color=col, alpha=0.55 if mode == "signal" else 1, lw=1.3, label=f"{mode} (n={len(g):,})", density=True)
        ax.axvline(0, color=C["ink"], lw=0.6)
        ax.set_title(f"{NAME[f]}, BTC, 1 s hold, run 1"); ax.set_xlabel("gross P&L per trade before fees (USD)")
        ax.legend(fontsize=7)
    axs[0].set_ylabel("density")
    fig.tight_layout(); save(fig, "f_gross_dist")

    # cost decomposition per variant (real, signal), both runs
    fig, axs = plt.subplots(1, 2, figsize=(7.2, 3.0), sharey=True)
    for ax, r in zip(axs, RUNS):
        x = tot[r].groupby(["f", "hold_s"]).agg(gross=("gross", "mean"), fees=("fees", "mean"), slip=("slip", "mean"),
                                                 net=("net", "mean")).reset_index()
        lab = [f"{NAME[a]} {h}s" for a, h in zip(x.f, x.hold_s)]
        y = np.arange(len(x))
        ax.barh(y, x.gross, color=C["muted"], height=0.6, label="gross (spread + price move)")
        ax.barh(y, -x.slip, left=x.gross, color="#f09a8d", height=0.6, label="slippage")
        ax.barh(y, -x.fees, left=x.gross - x.slip, color=C["cost"], height=0.6, label="taker fees")
        for yy, n in zip(y, x.net):
            ax.text(n - 1, yy, usd(n), va="center", ha="right", fontsize=7.5)
        ax.set_yticks(y, lab); ax.set_xlim(min(x.net) * 1.35, 2); ax.axvline(0, color=C["ink"], lw=0.6)
        ax.set_title(f"Run {r[-1]}: mean P&L per trade (USD)"); ax.grid(axis="y", visible=False)
    h, l = axs[0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=3, fontsize=7.5, bbox_to_anchor=(0.5, -0.06))
    fig.tight_layout(); save(fig, "f_cost_decomp")

    # cumulative net over time (run 1) + binance coverage
    st = [json.loads(l) for l in open(RES / "paper" / RUNS["run1"] / "status.jsonl")]
    fig, axs = plt.subplots(2, 1, figsize=(7.2, 3.6), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    x = tot["run1"].sort_values("t_local")
    for f in ("bybit", "binance"):
        for h, ls in ((1, "-"), (5, "--"), (30, ":")):
            g = x[(x.f == f) & (x.hold_s == h)]
            axs[0].plot((g.t_local - x.t_local.min()) / 6e4, g.net.cumsum() / 1e6, color=C[f], ls=ls, lw=1.2, label=f"{NAME[f]} {h} s")
    axs[0].set_ylabel("cumulative net (USD m)"); axs[0].legend(fontsize=7, ncol=2)
    axs[0].set_title("Run 1: cumulative simulated net P&L, real latency, 1 BTC / 10 ETH contracts per signal")
    axs[1].fill_between([s["min"] for s in st], [s["binance_valid"] for s in st], step="mid", color=C["binance"], alpha=0.4, lw=0)
    axs[1].set_ylabel("Binance\nlive options"); axs[1].set_xlabel("minutes into run")
    fig.tight_layout(); save(fig, "f_cum_run1")

    # fee vs premium (cap regimes), BTC, both runs, Bybit
    fig, axs = plt.subplots(1, 2, figsize=(7.2, 2.7), sharey=True)
    for ax, f in zip(axs, ("bybit", "binance")):
        for r, mk in (("run1", "o"), ("run2", "^")):
            g = tot[r][(tot[r].f == f) & (tot[r].coin == "BTC") & (tot[r].hold_s == 1)]
            g = g.sample(min(3000, len(g)), random_state=1)
            ax.scatter(g.px_in, g.fees / 2, s=4, alpha=0.35, marker=mk, color=C[f] if r == "run1" else C["ink"],
                       label=f"run {r[-1]}", lw=0)
        pr = np.linspace(0, 1500, 200)
        S = 79000
        ax.plot(pr, np.minimum(TAKER * S, CAP[f] * pr), color=C["cost"], lw=1.2, label=f"min(0.03%·S, {CAP[f]:.0%}·premium), S=$79k")
        ax.set_xlabel("option premium (USD)"); ax.set_title(f"{NAME[f]}: fee per side vs premium, BTC")
        ax.legend(fontsize=7, loc="lower right", markerscale=3)
    axs[0].set_ylabel("fee per contract per side (USD)")
    fig.tight_layout(); save(fig, "f_fee_cap")

    # net per trade by moneyness and by call/put (run 1, Bybit, 1 s)
    rows = []
    for r in RUNS:
        g = tot[r][(tot[r].hold_s == 1)].copy()
        g["mny"] = (g.K / g.S - 1) * 100
        g["bucket"] = pd.cut(g.mny, [-10, -1.5, -0.5, 0.5, 1.5, 10], labels=["< −1.5%", "−1.5…−0.5%", "±0.5%", "0.5…1.5%", "> 1.5%"])
        for (f, coin, b), x in g.groupby(["f", "coin", "bucket"], observed=True):
            rows.append({"Run": r[-1], "Exchange": NAME[f], "Coin": coin, "Strike vs spot": str(b), "Trades": len(x),
                         "Avg premium": x.px_in.mean(), "Gross/trade": x.gross.mean(), "Fees/trade": x.fees.mean(), "Net/trade": x.net.mean()})
    table("t_moneyness", pd.DataFrame(rows), {"Trades": "{:,}", "Avg premium": usd, "Gross/trade": usd, "Fees/trade": usd,
                                              "Net/trade": usd}, cls="small")
    # trades per minute
    fig, ax = plt.subplots(figsize=(7.2, 2.3))
    for r, ls in (("run1", "-"), ("run2", "--")):
        x = tot[r]
        m = ((x.t_local - x.t_local.min()) // 6e4).value_counts().sort_index()
        ax.plot(m.index, m.values, ls=ls, color=C["ink"] if r == "run1" else C["bybit"], lw=1, label=f"run {r[-1]}")
    ax.set_xlabel("minutes into run"); ax.set_ylabel("S1 trades / min"); ax.legend()
    ax.set_title("Simulated trade rate, all six real-latency S1 variants combined")
    save(fig, "f_trade_rate")
    put(trade_rate={r: float(len(x) / ((x.t_local.max() - x.t_local.min()) / 6e4)) for r, x in tot.items()})

    # engine counters
    rows = []
    for r in RUNS:
        s = rep[r]["stats"]
        for k in ("s1_deribit_moves", "s1_signal:bybit", "s1_signal:binance", "s1_not_stale:bybit", "s1_not_stale:binance",
                  "s1_wide_book:bybit", "s1_wide_book:binance", "q:deribit", "q:bybit", "q:binance"):
            rows.append({"Run": r[-1], "Counter": k, "Value": s.get(k, 0)})
        for k, v in s.items():
            if "real|signal" in k and ("nofill" in k or "forced" in k or "nodata" in k):
                rows.append({"Run": r[-1], "Counter": k.replace("S1|", ""), "Value": v})
    table("t_counters", pd.DataFrame(rows), {"Value": "{:,}"}, cls="small")


def s2():
    rows, hurd = [], {}
    fig, axs = plt.subplots(2, 2, figsize=(7.2, 4.6))
    for j, r in enumerate(RUNS):
        cx = pd.read_json(ROOT / "data_cache" / "paper" / RUNS[r] / "s2_crosses.jsonl", lines=True)
        cx["pair"] = cx["buy"].map(NAME) + " → " + cx["sell"].map(NAME)
        for (coin, pair), g in cx.groupby(["coin", "pair"]):
            rows.append({"Run": r[-1], "Coin": coin, "Buy → sell": pair, "10 s windows": len(g), "Median best cross": g.best_gross.median(),
                         "p99": g.best_gross.quantile(.99), "Max": g.best_gross.max(), "% windows crossed": (g.best_gross > 0).mean()})
        for i, coin in enumerate(("BTC", "ETH")):
            ax = axs[i, j]
            for pair, col in (("Binance → Bybit", C["binance"]), ("Bybit → Binance", C["bybit"]), ("Deribit → Bybit", C["deribit"])):
                g = cx[(cx.coin == coin) & (cx.pair == pair)].sort_values("t")
                ax.plot((g.t - cx.t.min()) / 6e4, g.best_gross, color=col, lw=0.7, label=pair)
            S = cx[cx.coin == coin].S.median()
            h = S * (2 * TAKER + EXERCISE + SETTLE_BP * 1e-4)
            hurd[f"{r}_{coin}"] = float(h)
            ax.axhline(h, color=C["cost"], lw=1.4, ls="--", label=f"cost hurdle ≈ {usd(h)}")
            ax.axhline(0, color=C["ink"], lw=0.5)
            ax.set_title(f"Run {r[-1]}, {coin}: best cross per 10 s window"); ax.set_ylabel("USD per contract")
            if i == 1:
                ax.set_xlabel("minutes into run")
    h, l = axs[0, 0].get_legend_handles_labels()
    fig.legend(h[:3] + [plt.Line2D([], [], color=C["cost"], ls="--")], l[:3] + ["cost hurdle (fees + exercise + settlement, before slippage)"],
               loc="lower center", ncol=4, fontsize=7, bbox_to_anchor=(0.5, -0.04))
    fig.tight_layout(); save(fig, "f_s2_crosses")
    table("t_s2", pd.DataFrame(rows), {"Median best cross": usd, "p99": usd, "Max": usd, "% windows crossed": "{:.1%}"}, cls="small")
    put(s2_hurdle_ex_slip=hurd, s2_rows=rows)


def wing():
    snaps, cands = [], []
    for d in sorted(glob.glob(str(RES / "wing_scan" / "*")))[-1:]:  # the 10-min run; the first was a 5-snapshot trial
        snaps += [json.loads(l) for l in open(pathlib.Path(d) / "snapshots.jsonl")]
        cands += [json.loads(l) for l in open(pathlib.Path(d) / "candidates.jsonl")]
    s = pd.DataFrame([{"t": x["t"], "deribit": x["n"]["deribit"], "bybit": x["n"]["bybit"], "binance": x["n"]["binance"],
                       "gross_cross": x["n_gross_cross"], "net_pos_short": x["n_net_pos_short"], "net_pos_long": x["n_net_pos_long"],
                       "confirmed": x["n_confirmed"], "best_key": x["best"]["key"] if x.get("best") else None,
                       "best_net": x["best"]["net"] if x.get("best") else None, "best_dte": x["best"]["dte"] if x.get("best") else None}
                      for x in snaps])
    put(wing={"snapshots": len(s), "instruments_med": float((s.deribit + s.bybit + s.binance).median()),
              "gross_cross_med": float(s.gross_cross.median()), "net_pos_long_med": float(s.net_pos_long.median()),
              "net_pos_short_total": int(s.net_pos_short.sum()), "confirmed_total": int(s.confirmed.sum())})
    st = s.describe().T[["mean", "min", "50%", "max"]].loc[["deribit", "bybit", "binance", "gross_cross", "net_pos_short", "net_pos_long", "confirmed"]]
    st.index = ["Deribit instruments", "Bybit instruments", "Binance instruments", "Gross crosses", "Net-positive, ≤ 30 DTE",
                "Net-positive, > 30 DTE", "Confirmed on order books"]
    table("t_wing_snap", st, {c: "{:,.1f}" for c in st.columns}, index=True)
    c = pd.DataFrame(cands)
    if len(c):
        c["annualised_on_premium"] = c.net / c.premium.clip(lower=1e-9) * 365 / c.dte.clip(lower=1e-6) if "premium" in c else np.nan
        cc = c.drop_duplicates("key")[["key", "buy", "sell", "dte", "ask", "bid", "gross", "cost", "net", "confirmed", "c_net_total"]].copy()
        cc.columns = ["Instrument", "Buy", "Sell", "DTE", "Ask (USD)", "Bid (USD)", "Gross", "Cost", "Net", "Confirmed", "Confirmed net total"]
        table("t_wing_cands", cc, {"DTE": "{:.1f}", "Ask (USD)": "{:,.2f}", "Bid (USD)": "{:,.2f}", "Gross": usd, "Cost": usd, "Net": usd,
                                   "Confirmed net total": usd}, cls="small")
    # best long-dated: carry yield
    b = s.dropna(subset=["best_net"])
    best = pd.DataFrame([x["best"] for x in snaps if x.get("best")])
    best["carry_pct_yr"] = best.net / best.ask * 365 / best.dte * 100
    put(wing_carry={"min": float(best.carry_pct_yr.min()), "max": float(best.carry_pct_yr.max()), "median": float(best.carry_pct_yr.median()),
                    "dte_med": float(best.dte.median())})
    fig, ax = plt.subplots(figsize=(7.2, 2.4))
    ax.plot((s.t - s.t.min()) / 60, s.gross_cross, color=C["muted"], label="gross crosses (all expiries)")
    ax.plot((s.t - s.t.min()) / 60, s.net_pos_long, color=C["deribit"], label="net-positive after costs, > 30 DTE")
    ax.plot((s.t - s.t.min()) / 60, s.net_pos_short, color=C["cost"], label="net-positive after costs, ≤ 30 DTE")
    ax.set_xlabel("minutes"); ax.set_ylabel("instruments per snapshot"); ax.set_ylim(-5, 300)
    ax.legend(fontsize=7, loc="upper center", ncol=3)
    ax.set_title("Full-chain crossed-market scan, ~4,100 options per 20 s snapshot")
    save(fig, "f_wing")


def pm():
    snaps = [json.loads(l) for l in open(next(iter(sorted(glob.glob(str(RES / "pm_vs_deribit" / "*" / "snapshots.jsonl"))))))]
    rows = []
    for s in snaps:
        for r in s["rows"]:
            if r["bid"] and r["ask"]:
                mid = (r["bid"] + r["ask"]) / 2
                rows.append({"event": s["event"], "hours_left": s["hours_left"], "K": r["K"], "spot": s["spot_usd"], "mid": mid,
                             "fair": r["fair"], "dev": mid - r["fair"], "iv": r["iv"], "bsz": r["bsz"], "asz": r["asz"],
                             "spread": r["ask"] - r["bid"], "buy_edge": r["buy_edge"], "sell_edge": r["sell_edge"]})
    d = pd.DataFrame(rows)
    live = d[(d.mid > 0.02) & (d.mid < 0.98)]
    g = live.groupby("event").agg(snapshots=("hours_left", "nunique"), strikes=("K", "nunique"), mean_abs_dev=("dev", lambda x: x.abs().mean()),
                                  max_abs_dev=("dev", lambda x: x.abs().max()), mean_dev=("dev", "mean"),
                                  best_buy_edge=("buy_edge", "max"), best_sell_edge=("sell_edge", "max")).reset_index()
    g.columns = ["Event", "Snapshots", "Live strikes", "Mean |mid − fair|", "Max |mid − fair|", "Mean mid − fair", "Best buy edge", "Best sell edge"]
    pct = lambda x: f"{x * 100:.2f}¢"
    table("t_pm", g, {c: pct for c in g.columns[3:]})
    mono = sum(len(s["mono_arbs"]) for s in snaps)
    put(pm={"snapshots": len(snaps), "events": int(d.event.nunique()), "mean_abs_dev_c": float(live.dev.abs().mean() * 100),
            "max_abs_dev_c": float(live.dev.abs().max() * 100), "mono_arbs": mono, "obs": len(live)})
    fig, axs = plt.subplots(1, 2, figsize=(7.2, 2.8))
    for ev, x in d.groupby("event"):
        axs[0].scatter(x.fair, x.mid, s=6, alpha=0.5, lw=0)
        axs[1].scatter(x.K / x.spot - 1, x.dev * 100, s=6, alpha=0.5, lw=0, label=ev.replace("Bitcoin above ___ on ", "")[:18])
    axs[0].plot([0, 1], [0, 1], color=C["ink"], lw=0.7); axs[0].set_xlabel("Deribit-implied P(S_T > K)"); axs[0].set_ylabel("Polymarket mid")
    axs[0].set_title("Polymarket vs Deribit-implied probability")
    axs[1].axhline(0, color=C["ink"], lw=0.6); axs[1].set_xlabel("strike / spot − 1"); axs[1].set_ylabel("mid − fair (cents)")
    axs[1].set_title("Deviation by moneyness"); axs[1].legend(fontsize=6, markerscale=2)
    fig.tight_layout(); save(fig, "f_pm")


STAGES = {"gate0": gate0, "rec": rec, "gate1": gate1, "paper": paper, "s2": s2, "wing": wing, "pm": pm}
if __name__ == "__main__":
    for st in (sys.argv[1:] or list(STAGES)):
        print("== stage", st)
        STAGES[st]()
