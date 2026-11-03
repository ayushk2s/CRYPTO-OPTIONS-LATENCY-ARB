"""Report for a 05_paper_trade.py run.

usage: python scripts/06_paper_report.py [run_name]   (default: latest results/paper/*)
Writes results/paper/<run>/report.json and report.md
Stats:
  - per variant: n, gross / fees / slippage / net (USD), mean net per trade with a 1-minute
    cluster bootstrap 95% CI (trades overlap in time, so i.i.d. CIs would be too narrow), win rate
  - S1 signal vs random-direction null (same timing): permutation test on mean gross per trade
  - S2: how often quotes crossed at all, after basis, after costs; fills; best edges seen
"""
import sys, json, glob, pathlib, collections
import numpy as np, pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
rng = np.random.default_rng(3)


def cluster_boot(x, cl, n=2000):
    """bootstrap mean of x resampling whole clusters."""
    df = pd.DataFrame({"x": x, "c": cl})
    g = df.groupby("c").x.agg(["sum", "count"])
    s, c = g["sum"].values, g["count"].values
    if len(s) < 5:
        return [np.nan, np.nan]
    idx = rng.integers(0, len(s), (n, len(s)))
    means = s[idx].sum(1) / c[idx].sum(1)
    return [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))]


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(str(ROOT / "results/paper/*")))[-1].split("\\")[-1].split("/")[-1]
    res, raw = ROOT / "results/paper" / name, ROOT / "data_cache/paper" / name
    meta = json.loads((res / "meta.json").read_text())
    status = [json.loads(l) for l in open(res / "status.jsonl")]
    summ = json.loads((res / "summary.json").read_text()) if (res / "summary.json").exists() else status[-1]
    hours = summ.get("hours") or status[-1]["min"] / 60
    nbk = sum(1 for v in meta["instruments"].values() if "binance" in v)
    bin_cov = float(np.mean([s["binance_valid"] > 0 for s in status])) if nbk else 0.0

    cols = ["strat", "var", "coin", "key", "venue", "qty", "gross", "fees", "slip", "net", "t_in", "t_dec", "filled",
            "leg_loss", "settle", "gap", "gross_sig", "cost_sig", "buy", "sell"]
    rows = []
    with open(raw / "trades.jsonl") as fh:
        for l in fh:
            d = json.loads(l)
            rows.append({c: d.get(c) for c in cols})
    t = pd.DataFrame(rows)
    t["t"] = t.t_in.fillna(t.t_dec)
    t["minute"] = (t.t // 60000).astype("int64")
    out = {"run": name, "hours": hours, "binance_coverage": bin_cov, "p_usdt_end": summ.get("p_usdt"),
           "instruments": len(meta["instruments"]), "variants": {}, "s1_signal_vs_null": {}, "s2": {}}

    for var, g in t.groupby("var"):
        if g.strat.iloc[0] == "S2" and not g.filled.fillna(False).any() and g.leg_loss.fillna(0).sum() == 0:
            out["variants"][var] = {"n_signals": len(g), "n_filled": 0, "net": 0.0}
            continue
        n = len(g)
        r = {"n": n, "gross": g.gross.sum(), "fees": g.fees.sum(), "slip": g.slip.sum(), "net": g.net.sum(),
             "mean_gross": g.gross.mean(), "mean_fees": g.fees.mean(), "mean_net": g.net.mean(),
             "ci95_mean_net": cluster_boot(g.net.values, g.minute.values),
             "win_rate": float((g.net > 0).mean()), "net_per_day": g.net.sum() / hours * 24,
             "by_coin": {c: {"n": len(x), "mean_gross": x.gross.mean(), "mean_net": x.net.mean()} for c, x in g.groupby("coin")}}
        if g.strat.iloc[0] == "S2":
            r["n_filled"] = int(g.filled.fillna(False).sum())
            r["leg_loss"] = g.leg_loss.fillna(0).sum()
        out["variants"][var] = r

    # S1 signal vs null: same events, random direction
    for var in [v for v in out["variants"] if v.startswith("S1") and v.endswith("|signal")]:
        nv = var.replace("|signal", "|null")
        if nv not in out["variants"]:
            continue
        a = t[t["var"] == var].gross.values
        b = t[t["var"] == nv].gross.values
        obs = a.mean() - b.mean()
        pool = np.concatenate([a, b])
        perm = []
        for _ in range(1000):
            rng.shuffle(pool)
            perm.append(pool[:len(a)].mean() - pool[len(a):].mean())
        out["s1_signal_vs_null"][var] = {"mean_gross_signal": a.mean(), "mean_gross_null": b.mean(),
                                         "diff": obs, "perm_p_one_sided": float((np.array(perm) >= obs).mean())}

    # S2 crosses
    st = summ["stats"]
    for pair in ("deribit->bybit", "bybit->deribit", "deribit->binance", "binance->deribit", "binance->bybit", "bybit->binance"):
        out["s2"][pair] = {"quote_updates_crossed_after_basis": st.get(f"s2_crossed:{pair}", 0),
                           "crossed_but_killed_by_costs": st.get(f"s2_crossed_but_costs:{pair}", 0),
                           "signals": st.get(f"s2_signal:{pair}", 0), "nofill": st.get(f"s2_nofill:{pair}", 0)}
    cx = pd.read_json(raw / "s2_crosses.jsonl", lines=True)
    if len(cx):
        cx["pair"] = cx.buy + "->" + cx.sell
        q = cx.groupby(["coin", "pair"]).best_gross
        out["s2"]["best_gross_per_10s_window"] = {
            f"{c}|{p}": {"windows": int(len(x)), "median": float(x.median()), "p99": float(x.quantile(.99)),
                         "max": float(x.max()), "share_windows_crossed": float((x > 0).mean())}
            for (c, p), x in q}
    out["stats"] = st
    (res / "report.json").write_text(json.dumps(out, indent=1, default=float))

    # markdown
    L = [f"# Paper trade report — run {name}", "",
         f"- duration {hours:.2f} h, {out['instruments']} instruments, Binance live {bin_cov:.0%} of minutes, "
         f"USDT/USD p = {out['p_usdt_end']}", "", "## Variants (USD)", "",
         "| variant | n | gross | fees | slippage | net | mean net/trade [95% CI] | win% | net/day |",
         "|---|---|---|---|---|---|---|---|---|"]
    for var, r in sorted(out["variants"].items()):
        if "mean_net" not in r:
            L.append(f"| {var} | {r.get('n_signals', 0)} signals, 0 filled | | | | 0 | | | |")
            continue
        ci = r["ci95_mean_net"]
        L.append(f"| {var} | {r['n']} | {r['gross']:,.0f} | {r['fees']:,.0f} | {r['slip']:,.0f} | {r['net']:,.0f} | "
                 f"{r['mean_net']:,.2f} [{ci[0]:,.2f}, {ci[1]:,.2f}] | {r['win_rate']:.0%} | {r['net_per_day']:,.0f} |")
    L += ["", "## S1: does Deribit's direction carry information? (gross per trade, signal vs random)", "",
          "| variant | signal | null | diff | perm p |", "|---|---|---|---|---|"]
    for var, r in sorted(out["s1_signal_vs_null"].items()):
        L.append(f"| {var} | {r['mean_gross_signal']:.2f} | {r['mean_gross_null']:.2f} | {r['diff']:.2f} | {r['perm_p_one_sided']:.3f} |")
    L += ["", "## S2: crossed markets", "", "| pair | updates crossed (after USDT basis + strike gap) | killed by costs | signals | no-fill |",
          "|---|---|---|---|---|"]
    for p_, r in out["s2"].items():
        if "quote_updates_crossed_after_basis" in r:
            L.append(f"| {p_} | {r['quote_updates_crossed_after_basis']} | {r['crossed_but_killed_by_costs']} | {r['signals']} | {r['nofill']} |")
    if "best_gross_per_10s_window" in out["s2"]:
        L += ["", "Best gross cross per 10 s window (USD/contract; negative = never crossed):", "",
              "| coin/pair | windows | median | p99 | max | % windows crossed |", "|---|---|---|---|---|---|"]
        for k, r in out["s2"]["best_gross_per_10s_window"].items():
            L.append(f"| {k} | {r['windows']} | {r['median']:.2f} | {r['p99']:.2f} | {r['max']:.2f} | {r['share_windows_crossed']:.1%} |")
    (res / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
