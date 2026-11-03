"""Paper-trading engine for cross-venue BTC/ETH option strategies on live public quotes.

Nothing is ever sent to an exchange. Orders are simulated against the recorded book of the
target venue *as it stood on the exchange clock when the order would have arrived*:
    t_arrive_exch = t_decide_exch + RTT_v / 2
and a fill is only evaluated once local data up to that exchange time has been received
(eval at local t_decide + RTT_v/2 + feed_delay_p95_v), so there is no lookahead.

Strategies
  S1 latency-follow: on a Deribit option mid move, if the follower (binance/bybit) mid is stale,
     IOC-buy (sell) the follower at its stale ask (bid); exit at market after `hold` ms.
     Variants: hold in HOLDS x latency {real, zero} x direction {signal, random-null}.
  S2 crossed-market arb: same option on two venues, ask_A + costs < bid_B -> IOC both legs at
     signal prices; hold to expiry (payoffs cancel). Unmatched fills are unwound at market.

Costs (per contract, USD; contracts are 1 coin on all three venues)
  taker fee   min(TAKER * S_index, CAP_v * premium)
  exercise    EXERCISE * S_index charged once per S2 pair (conservative: assumes ITM)
  slippage    walk real depth where available (Bybit 5 levels); beyond touch on Deribit/Binance
              each additional touch-size of quantity costs one more tick; plus SLIP_TICKS on every fill
  S2 settlement-index mismatch haircut SETTLE_BP * S per pair

USD vs USDT: Deribit options are USD-index settled; Binance/Bybit are USDT-quoted and USDT-index
settled. USDT traded at ~0.9987 USD on 2026-10-23 (Coinbase, Deribit usdt_usd and index ratios agree).
p = USD per USDT is tracked live as deribit_index / bybit_index. USDT prices are converted with p, and a
USDT option of strike K pays like a USD option of strike p*K, so a Deribit<->USDT-venue pair carries a
strike gap; S2 books its worst-case payoff with p allowed to drift by +-P_DRIFT before expiry.
"""
import collections, json, math, random, time

TAKER = 3e-4
CAP = {"deribit": 0.125, "bybit": 0.07, "binance": 0.10}  # binance cap: assumption (not verified)
EXERCISE = 1.5e-4
SLIP_TICKS = 0.5
SETTLE_BP = 2.0
VENUES = ("deribit", "binance", "bybit")
HIST_MS = 15000
P_DRIFT = 5e-4
S1_MAX_SPREAD_TICKS = 3


class Book:
    __slots__ = ("hist",)

    def __init__(self):
        self.hist = collections.deque()  # (te, bid, ask, bsz, asz, bl, al, S)

    def add(self, te, q):
        h = self.hist
        if h and te < h[-1][0]:
            te = h[-1][0]  # keep monotone (exchange ts can tie / jitter)
        h.append((te,) + q)
        while h and h[0][0] < te - HIST_MS:
            h.popleft()

    def last(self):
        return self.hist[-1] if self.hist else None

    def at(self, te):
        """state with largest timestamp <= te, or None."""
        h = self.hist
        if not h or h[0][0] > te:
            return None
        lo, hi = 0, len(h) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if h[mid][0] <= te:
                lo = mid
            else:
                hi = mid - 1
        return h[lo]


def gap_worst(K, cp, pa, pb):
    """worst-case payoff per contract of long option (eff. strike K*pa) + short option (eff. strike K*pb)."""
    w = 0.0
    for da in (-P_DRIFT, 0, P_DRIFT) if pa != 1.0 else (0,):
        for db in (-P_DRIFT, 0, P_DRIFT) if pb != 1.0 else (0,):
            if pa != 1.0 and pb != 1.0:
                db = da  # both USDT venues: same basis drift
            a, b = K * pa * (1 + da), K * pb * (1 + db)
            w = min(w, (b - a) if cp == "C" else (a - b))
    return w


def fee(v, S, prem, qty):
    return qty * min(TAKER * S, CAP[v] * max(prem, 0.0))


def walk(state, side, qty, tick, limit=None):
    """Fill `qty` against book `state` on `side` ('buy' hits asks). Returns (filled_qty, avg_px).
    With limit, only levels at or better than limit are taken (IOC). Slippage haircut not included."""
    _, bid, ask, bsz, asz, bl, al, _ = state
    levels = (al if side == "buy" else bl) or None
    if not levels:
        touch, tsz = (ask, asz) if side == "buy" else (bid, bsz)
        if touch is None or not tsz:
            return 0.0, None
        sgn = 1 if side == "buy" else -1
        levels = [(touch + sgn * tick * i, tsz) for i in range(0, 50)]  # synthetic: +1 tick per touch-size
    got, cost = 0.0, 0.0
    for p, s in levels:
        if limit is not None and ((side == "buy" and p > limit + 1e-9) or (side == "sell" and p < limit - 1e-9)):
            break
        take = min(s, qty - got)
        got += take
        cost += take * p
        if got >= qty - 1e-12:
            break
    return (got, cost / got) if got > 0 else (0.0, None)


class Engine:
    def __init__(self, inst, rtt_ms, feed_delay_ms, out_dir, holds=(1000, 5000, 30000),
                 s1_qty={"BTC": 1.0, "ETH": 10.0}, s2_max_qty={"BTC": 5.0, "ETH": 50.0}, min_qty={"BTC": 0.1, "ETH": 1.0}):
        self.inst = inst  # key -> {venue: {"sym", "tick", "tick_steps"(deribit)}, "coin", "expiry_ms"}
        self.rtt, self.fdelay = rtt_ms, feed_delay_ms
        self.books = {(v, k): Book() for k in inst for v in VENUES}
        self.valid = {(v, k): False for k in inst for v in VENUES}
        self.index = {}  # coin -> latest spot index (Deribit index; Bybit index as fallback)
        self.vidx = collections.defaultdict(dict)  # coin -> venue -> index, for settlement mismatch stats
        self.idx_diffs = collections.defaultdict(list)
        self.offset = 0.0  # exch_ms - local_ms
        self.pending = []  # (eval_local_ms, fn, args)
        self.holds, self.s1_qty, self.s2_max, self.min_qty = holds, s1_qty, s2_max_qty, min_qty
        self.rng = random.Random(11)
        self.open_s1 = set()  # (variant, follower, key)
        self.s2_block = set()  # (venue, key, side) consumed until next update
        self.s2_cool = {}
        self.out = open(out_dir / "trades.jsonl", "a", buffering=1)
        self.nm = open(out_dir / "s2_crosses.jsonl", "a", buffering=1)
        self.stats = collections.Counter()
        self.pnl = collections.defaultdict(float)
        self.ntr = collections.Counter()
        self.gross = collections.defaultdict(float)
        self.costs = collections.defaultdict(float)
        self.wins = collections.Counter()
        self.capital = collections.defaultdict(float)
        self.peak_capital = collections.defaultdict(float)
        self.der_prev = {}  # key -> (deribit mid, te at that mid, follower mids at that time)
        self.best_cross = {}  # (coin, A, B) -> best gross cross seen in current 10 s window
        self.p = {"deribit": 1.0, "bybit": None, "binance": None}  # USD per quote-currency unit

    # ---------- utils ----------
    def now_exch(self):
        return time.time() * 1000 + self.offset

    def tick(self, v, k, px_usd):
        i = self.inst[k][v]
        if v != "deribit":
            return i["tick"]
        S = self.index.get(self.inst[k]["coin"]) or 0
        pc = px_usd / S if S else 0
        t = i["tick"]
        for st in i.get("tick_steps", []):
            if pc >= st["above_price"]:
                t = st["tick_size"]
        return t * S

    def log(self, rec):
        self.out.write(json.dumps(rec, separators=(",", ":"), default=float) + "\n")

    # ---------- feed input ----------
    def on_record(self, r):
        kind = r.get("kind")
        if kind in ("conn", "disconnect"):
            v = r["v"].split(":")[0].replace("binance_books", "binance")
            if v in VENUES:
                for k in r.get("keys", []):
                    if k in self.inst:
                        self.valid[(v, k)] = kind == "conn"
                        if kind == "disconnect":
                            self.books[(v, k)] = Book()
                self.stats[f"{kind}:{v}"] += 1
            return
        v, k = r["v"], r.get("key")
        if v == "deribit_index":
            self.index[k[:3]] = r["mark"]
            self.vidx[k[:3]]["deribit"] = r["mark"]
            return
        if kind == "m":
            if v == "bybit" and r.get("idx"):
                self.vidx[k[:3]]["bybit"] = r["idx"]
                self.index.setdefault(k[:3], r["idx"])
                d = self.vidx[k[:3]].get("deribit")
                if d and k[:3] == "BTC":
                    ratio = d / r["idx"]
                    cur = self.p["bybit"]
                    self.p["bybit"] = self.p["binance"] = ratio if cur is None else 0.99 * cur + 0.01 * ratio
            return
        if kind != "q" or k not in self.inst or v not in VENUES:
            return
        coin = self.inst[k]["coin"]
        if v == "deribit":
            S = r.get("idx") or self.index.get(coin)
            if not S:
                return
            bid = r["bid_c"] * S if r.get("bid_c") else None
            ask = r["ask_c"] * S if r.get("ask_c") else None
            q = (bid, ask, r.get("bsz"), r.get("asz"), None, None, S)
        else:
            S = self.index.get(coin)
            q = (r.get("bid"), r.get("ask"), r.get("bsz"), r.get("asz"), r.get("bl"), r.get("al"), S)
        self.valid[(v, k)] = True
        self.books[(v, k)].add(float(r["te"]), q)
        self.s2_block.discard((v, k, "buy")); self.s2_block.discard((v, k, "sell"))
        self.stats[f"q:{v}"] += 1
        if v == "deribit":
            self.on_deribit(k, float(r["te"]))
        self.check_s2(k, float(r["te"]))

    # ---------- S1 ----------
    def on_deribit(self, k, te):
        st = self.books[("deribit", k)].last()
        if st[1] is None or st[2] is None:
            return
        mid = (st[1] + st[2]) / 2
        fol_mids = {}
        for f in ("binance", "bybit"):
            s = self.books[(f, k)].last()
            if self.valid[(f, k)] and s and s[1] and s[2]:
                fol_mids[f] = (s[1] + s[2]) / 2
        prev = self.der_prev.get(k)
        self.der_prev[k] = (mid, te, fol_mids)
        if prev is None or mid == prev[0]:
            return
        d = 1 if mid > prev[0] else -1
        self.stats["s1_deribit_moves"] += 1
        for f, fm in fol_mids.items():
            pm = prev[2].get(f)
            if pm is None or (fm - pm) * d > 0:  # follower already moved our way -> not stale
                self.stats[f"s1_not_stale:{f}"] += 1
                continue
            fst = self.books[(f, k)].last()
            if fst[2] - fst[1] > S1_MAX_SPREAD_TICKS * self.inst[k][f]["tick"] + 1e-9:
                self.stats[f"s1_wide_book:{f}"] += 1
                continue
            self.stats[f"s1_signal:{f}"] += 1
            for hold in self.holds:
                for lat in ("real", "zero"):
                    for mode in ("signal", "null"):
                        dd = d if mode == "signal" else self.rng.choice((-1, 1))
                        var = f"S1|{f}|h{hold}|{lat}|{mode}"
                        if (var, f, k) in self.open_s1:
                            continue
                        self.open_s1.add((var, f, k))
                        side = "buy" if dd > 0 else "sell"
                        # real: IOC at the stale quote we saw; zero-latency: take the book as it was at te
                        limit = (fst[2] if side == "buy" else fst[1]) if lat == "real" else None
                        t_dec = te if lat == "zero" else self.now_exch()
                        t_arr = t_dec if lat == "zero" else t_dec + self.rtt[f] / 2
                        self.schedule(t_arr, self.s1_entry, (var, f, k, side, limit, t_arr, hold, lat))

    def schedule(self, t_arr_exch, fn, args, venue_delay=None):
        # evaluate once data with exchange ts up to t_arr has had time to arrive locally
        dl = max(self.fdelay.values()) if venue_delay is None else venue_delay
        self.pending.append((t_arr_exch + dl, fn, args))

    def s1_entry(self, var, f, k, side, limit, t_arr, hold, lat):
        coin = self.inst[k]["coin"]
        st = self.books[(f, k)].at(t_arr)
        if st is None or not self.valid[(f, k)]:
            self.stats[f"{var}:nodata"] += 1; self.open_s1.discard((var, f, k)); return
        tk = self.inst[k][f]["tick"]
        q, px = walk(st, side, self.s1_qty[coin], tk, limit=limit)
        q = math.floor(q / self.min_qty[coin] + 1e-9) * self.min_qty[coin]
        if q <= 0:
            self.stats[f"{var}:nofill"] += 1; self.open_s1.discard((var, f, k)); return
        S = st[7] or self.index.get(coin)
        px_eff = px + (1 if side == "buy" else -1) * SLIP_TICKS * tk
        pos = dict(var=var, f=f, k=k, side=side, q=q, px=px, px_eff=px_eff, fee_in=fee(f, S, px, q), t_in=t_arr, S=S)
        t_exit = t_arr + hold if lat == "zero" else t_arr + hold + self.rtt[f] / 2
        self.schedule(t_exit, self.s1_exit, (pos, t_exit))

    def s1_exit(self, pos, t_exit):
        f, k, var = pos["f"], pos["k"], pos["var"]
        coin = self.inst[k]["coin"]
        tk = self.inst[k][f]["tick"]
        st = self.books[(f, k)].at(t_exit)
        xside = "sell" if pos["side"] == "buy" else "buy"
        if st is None:
            st = self.books[(f, k)].last()
        q, px = walk(st, xside, pos["q"], tk) if st else (0, None)
        if not q or q < pos["q"] - 1e-9:
            # can't exit fully on this book (outage/empty side): mark at 2x worse touch, flagged
            touch = (st[1] if xside == "sell" else st[2]) if st else None
            px = (touch if touch else pos["px"]) + (-2 if xside == "sell" else 2) * tk
            self.stats[f"{var}:forced_exit"] += 1
        px_eff = px + (-1 if xside == "sell" else 1) * SLIP_TICKS * tk
        sgn = 1 if pos["side"] == "buy" else -1
        pu = self.p[f] or 1.0
        gross = sgn * (px - pos["px"]) * pos["q"] * pu
        slip = SLIP_TICKS * tk * 2 * pos["q"] * pu
        fees = pos["fee_in"] + fee(f, pos["S"], px * pu, pos["q"])
        net = gross - slip - fees
        self.book_trade(var, coin, dict(strat="S1", var=var, key=k, venue=f, side=pos["side"], qty=pos["q"],
                                        px_in=pos["px"], px_out=px, t_in=pos["t_in"], t_out=t_exit, gross=gross,
                                        slip=slip, fees=fees, net=net, S=pos["S"]))
        self.open_s1.discard((var, f, k))

    # ---------- S2 ----------
    def check_s2(self, k, te):
        coin = self.inst[k]["coin"]
        live = {}
        for v in VENUES:
            s = self.books[(v, k)].last()
            if self.valid[(v, k)] and s and s[7]:
                live[v] = s
        if len(live) < 2 or self.p["bybit"] is None:
            return
        _, _, K, cp = k.split("-")
        K = float(K)
        for A in live:  # buy on A
            for B in live:  # sell on B
                if A == B:
                    continue
                a, b = live[A], live[B]
                if a[2] is None or b[1] is None or not a[4] or not b[3]:
                    continue
                pA, pB = self.p[A], self.p[B]
                raw = b[1] - a[2]
                gap = gap_worst(K, cp, pA, pB)
                gross = b[1] * pB - a[2] * pA + gap  # USD, worst-case locked
                S = a[7]
                ck = (coin, A, B)
                if gross > self.best_cross.get(ck, (-1e18,))[0]:
                    self.best_cross[ck] = (gross, k, te, a[2], b[1], S, raw, gap)
                if gross <= 0:
                    continue
                self.stats[f"s2_crossed:{A}->{B}"] += 1
                tkA, tkB = self.tick(A, k, a[2]), self.tick(B, k, b[1])
                cost = (fee(A, S, a[2], 1) + fee(B, S, b[1], 1) + EXERCISE * S + SETTLE_BP * 1e-4 * S
                        + SLIP_TICKS * (tkA + tkB))
                if gross - cost <= 0:
                    self.stats[f"s2_crossed_but_costs:{A}->{B}"] += 1
                    continue
                if (A, k, "buy") in self.s2_block or (B, k, "sell") in self.s2_block:
                    continue
                if te - self.s2_cool.get((k, A, B), -1e18) < 1000:
                    continue
                self.s2_cool[(k, A, B)] = te
                qty = min(a[4], b[3], self.s2_max[coin])
                qty = math.floor(qty / self.min_qty[coin] + 1e-9) * self.min_qty[coin]
                if qty <= 0:
                    self.stats["s2_too_small"] += 1
                    continue
                self.stats[f"s2_signal:{A}->{B}"] += 1
                t_dec = self.now_exch()
                legs = {A: ("buy", a[2], t_dec + self.rtt[A] / 2), B: ("sell", b[1], t_dec + self.rtt[B] / 2)}
                legs["_gap"] = gap
                t_eval = max(l[2] + self.fdelay[v] for v, l in legs.items() if v != "_gap")
                self.pending.append((t_eval, self.s2_fill, (k, A, B, legs, qty, gross, cost, te, t_dec)))

    def s2_fill(self, k, A, B, legs, qty, gross_sig, cost_sig, te_sig, t_dec):
        coin = self.inst[k]["coin"]
        fills = {}
        gap = legs.pop("_gap")
        pA, pB = self.p[A], self.p[B]
        for v, (side, limit, t_arr) in legs.items():
            st = self.books[(v, k)].at(t_arr)
            if st is None or not self.valid[(v, k)]:
                fills[v] = (0.0, None, st)
                continue
            q, px = walk(st, side, qty, self.tick(v, k, limit), limit=limit)
            fills[v] = (math.floor(q / self.min_qty[coin] + 1e-9) * self.min_qty[coin], px, st)
            self.s2_block.add((v, k, side))
        qa, pa, sa = fills[A]
        qb, pb, sb = fills[B]
        S = self.index.get(coin)
        qm = min(qa, qb)
        rec = dict(strat="S2", var=f"S2|{A}->{B}", key=k, buy=A, sell=B, qty_sig=qty, qty_buy=qa, qty_sell=qb,
                   gross_sig=gross_sig, cost_sig=cost_sig, te_sig=te_sig, t_dec=t_dec, S=S)
        net = 0.0
        if qm > 0:
            tkA, tkB = self.tick(A, k, pa), self.tick(B, k, pb)
            g = (pb * pB - pa * pA + gap) * qm
            fees = fee(A, S, pa * pA, qm) + fee(B, S, pb * pB, qm) + EXERCISE * S * qm
            slip = SLIP_TICKS * (tkA + tkB) * qm
            settle = SETTLE_BP * 1e-4 * S * qm
            net = g - fees - slip - settle
            rec.update(qty=qm, px_buy=pa, px_sell=pb, gross=g, gap=gap * qm, fees=fees, slip=slip, settle=settle,
                       p_buy=pA, p_sell=pB)
            self.capital["S2"] += qm * (pa + 0.15 * S)  # premium paid + ~15% IM on short leg
            self.peak_capital["S2"] = max(self.peak_capital["S2"], self.capital["S2"])
        # legging: unwind any excess on the filled side at market on that venue's book
        leg_loss = 0.0
        for v, q, px, st, side in ((A, qa, pa, sa, "buy"), (B, qb, pb, sb, "sell")):
            ex = q - qm
            if ex > 1e-9:
                xs = "sell" if side == "buy" else "buy"
                st2 = self.books[(v, k)].last() or st
                q2, px2 = walk(st2, xs, ex, self.tick(v, k, px))
                if not px2:
                    px2 = px * (0.5 if xs == "sell" else 1.5)
                tk = self.tick(v, k, px)
                loss = (px - px2 if side == "buy" else px2 - px) * ex * self.p[v]
                loss += fee(v, S, px, ex) + fee(v, S, px2, ex) + 2 * SLIP_TICKS * tk * ex
                leg_loss += loss
                self.stats["s2_legged"] += 1
        rec["leg_loss"] = leg_loss
        rec["net"] = net - leg_loss
        rec["filled"] = qm > 0
        if qm == 0 and leg_loss == 0:
            self.stats[f"s2_nofill:{A}->{B}"] += 1
        self.book_trade(rec["var"], coin, rec)

    # ---------- bookkeeping ----------
    def book_trade(self, var, coin, rec):
        rec["coin"] = coin
        rec["t_local"] = time.time() * 1000
        self.log(rec)
        self.pnl[var] += rec["net"]
        self.ntr[var] += 1
        # running totals for the live dashboard
        self.gross[var] += rec.get("gross", 0.0)
        self.costs[var] += rec.get("fees", 0.0) + rec.get("slip", 0.0)
        self.wins[var] += rec["net"] > 0

    def process_pending(self):
        now = self.now_exch()
        due = [p for p in self.pending if p[0] <= now]
        if not due:
            return
        self.pending = [p for p in self.pending if p[0] > now]
        for _, fn, args in sorted(due, key=lambda p: p[0]):
            fn(*args)

    def snapshot_crosses(self):
        t = self.now_exch()
        for (coin, A, B), (g, k, te, ask, bid, S, raw, gap) in self.best_cross.items():
            self.nm.write(json.dumps({"t": t, "coin": coin, "buy": A, "sell": B, "best_gross": g, "key": k,
                                      "ask": ask, "bid": bid, "S": S, "raw_cross": raw, "gap_worst": gap,
                                      "p": self.p["bybit"]}) + "\n")
        self.best_cross = {}
        for coin, d in self.vidx.items():
            if "deribit" in d and "bybit" in d:
                self.idx_diffs[coin].append(d["bybit"] - d["deribit"])

    def summary(self):
        return {"pnl": dict(self.pnl), "n": dict(self.ntr), "stats": dict(self.stats), "p_usdt": self.p["bybit"],
                "peak_capital": dict(self.peak_capital),
                "index_diff_bybit_minus_deribit": {c: {"n": len(x), "mean": sum(x) / len(x),
                                                       "mad": sum(abs(y) for y in x) / len(x)}
                                                   for c, x in self.idx_diffs.items() if x}}
