"""Live terminal dashboard for 05_paper_trade.py --dash (rich). Read-only view of the Engine.

Rendered on the asyncio loop (not rich's refresh thread) so engine dicts are never read mid-update.
"""
import asyncio, collections, io, statistics, time, datetime as dt
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from .engine import TAKER, EXERCISE, SETTLE_BP

VEN_NAME = {"deribit": "Deribit", "bybit": "Bybit", "binance": "Binance"}
HOLDS = (1000, 5000, 30000)
BARS = "▁▂▃▄▅▆▇█"
ACC = "bright_cyan"


class Ctx:
    """State the dashboard needs beyond the Engine (filled by the feed callback in 05)."""

    def __init__(self, run, hours, n_inst, bk, snap_dir=None):
        self.run, self.hours, self.n_inst, self.bk = run, hours, n_inst, bk
        self.snap_dir = snap_dir  # dashboard.svg written here every 5 min (crisp image for sharing)
        self.t0 = time.time()
        self.last_seen = {}
        self.delay = {v: collections.deque(maxlen=400) for v in VEN_NAME}
        self.log = collections.deque(maxlen=9)
        self.max_cross = {}  # (coin, A, B) -> best gross cross over the whole run
        self.curve = {"bybit": [], "binance": []}  # cumulative net, sampled every 30 s
        self.rate_prev = (time.time(), {})
        self.rate = {}

    def say(self, msg, style="white"):
        self.log.append((time.strftime("%H:%M:%S", time.gmtime()), msg, style))

    def on_record(self, eng, r):
        v = r.get("v")
        if v in VEN_NAME and r.get("kind") in ("q", "m"):
            self.last_seen[v] = time.time()
            if r.get("te") and r.get("tr"):
                self.delay[v].append(r["tr"] / 1e6 - (float(r["te"]) - eng.offset))


def usd(x, signed=True):
    if x is None:
        return "—"
    s = f"{abs(x):,.2f}"
    return ("−$" if x < 0 else ("+$" if signed else "$")) + s


def money_style(x):
    return "bold red" if x < 0 else "bold green"


def spark(vals, width):
    if len(vals) < 2:
        return "·" * width
    if len(vals) > width:
        vals = [vals[int(i * (len(vals) - 1) / (width - 1))] for i in range(width)]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return BARS[3] * len(vals)
    return "".join(BARS[int((v - lo) / (hi - lo) * (len(BARS) - 1))] for v in vals)


def header(ctx):
    el = time.time() - ctx.t0
    tot = ctx.hours * 3600
    frac = min(el / tot, 1.0)
    w = 34
    bar = "█" * int(frac * w) + "░" * (w - int(frac * w))
    h, m, s = int(el // 3600), int(el % 3600 // 60), int(el % 60)
    t = Text()
    t.append(" ◆ CROSS-VENUE OPTIONS LATENCY ARB ", style="bold white on #0e7490")
    t.append("  LIVE PAPER TRADE  ", style="bold white")
    t.append("● REC", style="bold red blink")
    t.append(f"     run {ctx.run}", style="grey62")
    t.append(f"     {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n", style="white")
    t.append(f" elapsed {h:02d}:{m:02d}:{s:02d} / {int(ctx.hours):02d}:{int(ctx.hours % 1 * 60):02d}:00  ", style="white")
    t.append(bar, style=ACC)
    t.append(f" {frac * 100:5.1f}%", style="bold white")
    t.append("     Deribit ⇄ Bybit ⇄ Binance · BTC/ETH options · public data only · no live orders", style="grey62")
    return Panel(t, box=box.HEAVY, border_style=ACC, padding=(0, 1))


def feeds(eng, ctx):
    now = time.time()
    dtt = now - ctx.rate_prev[0]
    if dtt >= 2:
        cur = {v: eng.stats.get(f"q:{v}", 0) for v in VEN_NAME}
        ctx.rate = {v: (cur[v] - ctx.rate_prev[1].get(v, cur[v])) / dtt for v in VEN_NAME}
        ctx.rate_prev = (now, cur)
    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=f"bold {ACC}", pad_edge=False)
    for c, j in (("VENUE", "left"), ("STATUS", "left"), ("QUOTES", "right"), ("MSG/S", "right"),
                 ("FEED DELAY p50", "right"), ("ORDER RTT", "right")):
        t.add_column(c, justify=j)
    for v, name in VEN_NAME.items():
        age = now - ctx.last_seen.get(v, 0)
        if v == "binance":
            live_n = sum(eng.valid[("binance", k)] for k in ctx.bk)
            st = (Text(f"● LIVE {live_n}/{len(ctx.bk)}", style="bold green") if age < 15
                  else Text("● DNS BLOCKED", style="bold red"))
        else:
            st = Text("● LIVE", style="bold green") if age < 15 else Text("● STALE", style="bold yellow")
        d = ctx.delay[v]
        dl = f"{statistics.median(d):,.0f} ms" if len(d) > 20 else "—"
        t.add_row(Text(name, style="bold"), st, f"{eng.stats.get(f'q:{v}', 0):,}", f"{ctx.rate.get(v, 0):,.0f}",
                  dl, f"{eng.rtt[v]:.0f} ms")
    return Panel(t, title="[bold]FEEDS[/]", title_align="left", border_style="grey42", box=box.ROUNDED)


def market(eng, ctx):
    t = Table.grid(padding=(0, 2), expand=True)
    t.add_column(style="grey62"); t.add_column(justify="right", style="bold white")
    for c in ("BTC", "ETH"):
        S = eng.index.get(c)
        t.add_row(f"{c} index (Deribit)", f"${S:,.2f}" if S else "—")
    p = eng.p.get("bybit")
    t.add_row("USDT / USD", f"{p:.5f}" if p else "—")
    t.add_row("Options tracked", f"{ctx.n_inst}")
    t.add_row("Deribit moves seen", f"{eng.stats.get('s1_deribit_moves', 0):,}")
    sig = sum(eng.stats.get(f"s1_signal:{f}", 0) for f in ("bybit", "binance"))
    t.add_row("Stale-quote signals", f"{sig:,}")
    t.add_row("Clock offset (exch−local)", f"{eng.offset:+,.0f} ms")
    return Panel(t, title="[bold]MARKET[/]", title_align="left", border_style="grey42", box=box.ROUNDED)


def s1(eng):
    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=f"bold {ACC}", pad_edge=False)
    for c in ("VENUE", "HOLD", "TRADES", "GROSS/TRADE", "FEES+SLIP/TRADE", "NET/TRADE", "TOTAL NET", "WINS",
              "SIGNAL − RANDOM"):
        t.add_column(c, justify="left" if c in ("VENUE", "HOLD") else "right")
    for f in ("bybit", "binance"):
        for h in HOLDS:
            var, nul = f"S1|{f}|h{h}|real|signal", f"S1|{f}|h{h}|real|null"
            n = eng.ntr.get(var, 0)
            if n == 0:
                t.add_row(VEN_NAME[f], f"{h // 1000} s", "0", "—", "—", "—", "—", "—", "—")
                continue
            g, c, net = eng.gross[var] / n, eng.costs[var] / n, eng.pnl[var] / n
            nn = eng.ntr.get(nul, 0)
            edge = g - eng.gross[nul] / nn if nn else None
            t.add_row(Text(VEN_NAME[f], style="bold"), f"{h // 1000} s", f"{n:,}",
                      Text(usd(g), style="red" if g < 0 else "green"), Text(usd(-c), style="red"),
                      Text(usd(net), style=money_style(net)), Text(usd(eng.pnl[var]), style=money_style(eng.pnl[var])),
                      f"{eng.wins[var]:,} ({eng.wins[var] / n * 100:.1f}%)",
                      Text(usd(edge) if edge is not None else "—", style="yellow"))
    return Panel(t, title="[bold]S1 · LATENCY-FOLLOW[/]  [grey62]hit the stale Binance/Bybit quote after a Deribit move · "
                          "real latency · IOC fills on the live book[/]", title_align="left", border_style="grey42",
                 box=box.ROUNDED)


def s2(eng, ctx):
    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=f"bold {ACC}", pad_edge=False)
    for c in ("BUY → SELL", "QUOTES CROSSED", "KILLED BY COSTS", "TRADES", "BEST CROSS BTC", "BEST CROSS ETH"):
        t.add_column(c, justify="left" if c == "BUY → SELL" else "right")
    for A in VEN_NAME:
        for B in VEN_NAME:
            if A == B:
                continue
            cr = eng.stats.get(f"s2_crossed:{A}->{B}", 0)
            kl = eng.stats.get(f"s2_crossed_but_costs:{A}->{B}", 0)
            n = eng.ntr.get(f"S2|{A}->{B}", 0)
            cells = []
            for coin in ("BTC", "ETH"):
                g = ctx.max_cross.get((coin, A, B))
                cells.append(Text(usd(g) if g is not None else "—", style="green" if (g or -1) > 0 else "grey50"))
            t.add_row(Text(f"{VEN_NAME[A]} → {VEN_NAME[B]}", style="bold"), f"{cr:,}",
                      Text(f"{kl:,}", style="red" if kl else "grey50"), Text(f"{n:,}", style="bold"), *cells)
    hurdle = []
    for coin in ("BTC", "ETH"):
        S = eng.index.get(coin)
        if S:
            hurdle.append(f"{coin} ≈ ${S * (2 * TAKER + EXERCISE + SETTLE_BP * 1e-4):,.2f}")
    return Panel(t, title="[bold]S2 · CROSSED-MARKET ARB[/]  [grey62]buy one venue's ask, sell another's bid, "
                          "after USDT basis · cost hurdle per contract: " + " · ".join(hurdle) + " + slippage[/]",
                 title_align="left", border_style="grey42", box=box.ROUNDED)


def curve(eng, ctx, width):
    t = Text()
    for f in ("bybit", "binance"):
        var = f"S1|{f}|h1000|real|signal"
        vals = ctx.curve[f]
        t.append(f" {VEN_NAME[f]:<8} 1 s  ", style="bold white")
        t.append(spark(vals, width), style="red")
        t.append(f"  {usd(eng.pnl.get(var, 0.0))}\n", style=money_style(eng.pnl.get(var, 0.0)))
    t.append(" cumulative net P&L of the paper strategy (1 BTC / 10 ETH contracts per signal)", style="grey50")
    return Panel(t, title="[bold]EQUITY CURVE[/]", title_align="left", border_style="grey42", box=box.ROUNDED)


def events(ctx):
    t = Text()
    for ts, msg, style in ctx.log:
        t.append(f" {ts} ", style="grey50")
        t.append(msg + "\n", style=style)
    return Panel(t, title="[bold]EVENTS (UTC)[/]", title_align="left", border_style="grey42", box=box.ROUNDED)


def verdict(eng):
    nets = {v: eng.pnl[v] / eng.ntr[v] for v in eng.ntr if v.startswith("S1") and "real|signal" in v and eng.ntr[v]}
    n_s2 = sum(n for v, n in eng.ntr.items() if v.startswith("S2"))
    crossed = sum(n for k, n in eng.stats.items() if k.startswith("s2_crossed:"))
    t = Text()
    t.append(" VERDICT SO FAR ", style="bold black on yellow")
    if nets:
        best = max(nets, key=nets.get)
        _, f, h, *_ = best.split("|")
        t.append(f"  best S1 variant {VEN_NAME[f]} {int(h[1:]) // 1000} s: ", style="white")
        t.append(f"{usd(nets[best])} per trade", style=money_style(nets[best]))
        t.append(f"   ·   S2: {crossed:,} quote crosses → {n_s2} trades after costs", style="white")
        t.append("   ·   fees ≫ lag", style="bold yellow")
    else:
        t.append("  warming up…", style="white")
    return Panel(t, box=box.HEAVY, border_style="yellow", padding=(0, 1))


def render(eng, ctx, console):
    w = console.size.width
    top = Table.grid(expand=True)
    top.add_column(ratio=3); top.add_column(ratio=2)
    top.add_row(feeds(eng, ctx), market(eng, ctx))
    bot = Table.grid(expand=True)
    bot.add_column(ratio=1); bot.add_column(ratio=1)
    bot.add_row(curve(eng, ctx, max(20, w // 2 - 34)), events(ctx))
    return Group(header(ctx), top, s1(eng), s2(eng, ctx), bot, verdict(eng))


async def run(eng, ctx):
    try:
        await _run(eng, ctx)
    except asyncio.CancelledError:
        raise
    except Exception:  # the dashboard must never take the paper trade down; leave a trace and stop drawing
        import traceback
        if ctx.snap_dir:
            (ctx.snap_dir / "dash_error.log").write_text(traceback.format_exc())


async def _run(eng, ctx):
    console = Console()
    last_curve = last_snap = 0.0
    with Live(render(eng, ctx, console), console=console, auto_refresh=False, screen=True) as live:
        while True:
            await asyncio.sleep(0.5)
            now = time.time()
            for (coin, A, B), bc in list(eng.best_cross.items()):
                g = bc[0]
                old = ctx.max_cross.get((coin, A, B))
                if old is None or g > old:
                    ctx.max_cross[(coin, A, B)] = g
                    if old is not None and g > 0 and now - ctx.t0 > 60:
                        ctx.say(f"S2 new best cross {VEN_NAME[A]}→{VEN_NAME[B]} {coin} {usd(g)}/contract "
                                f"→ below cost hurdle, no trade", "yellow")
            if now - last_curve >= 30:
                last_curve = now
                for f in ("bybit", "binance"):
                    ctx.curve[f].append(eng.pnl.get(f"S1|{f}|h1000|real|signal", 0.0))
            try:
                live.update(render(eng, ctx, console), refresh=True)
            except Exception as e:  # one bad frame (e.g. a dict resized mid-render) must not freeze the view
                ctx.say(f"render skipped: {type(e).__name__}", "grey50")
            if ctx.snap_dir and (now - last_snap >= 300 if last_snap else now - ctx.t0 > 90):
                last_snap = now
                rec = Console(record=True, width=190, file=io.StringIO(), force_terminal=True, color_system="truecolor")
                rec.print(render(eng, ctx, rec))
                rec.save_svg(str(ctx.snap_dir / "dashboard.svg"), title="paper_trade --dash")
