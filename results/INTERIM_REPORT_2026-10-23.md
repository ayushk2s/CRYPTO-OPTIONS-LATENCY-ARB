# Interim report — Deribit-led cross-venue options latency arb (2026-10-23, ~16:45 UTC)

Status: Gate 0 PASS · Gate 1 lag statistically real but economically negligible · paper trade
(S1 latency-follow, S2 crossed-market arb) running, 1.5 h of 3 h done. **Verdict so far: no edge.**

## 1. Gate 0 — connectivity and market structure
- All three venues reachable (REST + WS) from this machine, with one major caveat:
  **the ISP's DNS (Jio) intermittently sinkholes every `*.binance.com` host to 49.44.79.236.**
  Public resolvers return Binance's real AWS IPs and work fine. This caused the ~52 min Binance
  outage in the Gate 1 recording, and is the likely cause of the prior project's "fstream delivers zero".
  Per user decision: no bypass; Binance used only while system DNS resolves it; outages masked.
- Binance's old `nbstream` options WS host is retired (404). Working streams: `fstream /public/ws/<lowercase>@bookTicker`
  (event-driven BBO) and `/market/ws/<und>@optionMarkPrice` (whole chain, 1 s cadence).
- Chains are full (Deribit 930/784 BTC/ETH instruments, Binance 638/528, Bybit 686/568).
- **Premise check failed:** nearest-expiry ATM spreads are much TIGHTER on Binance/Bybit ($5 BTC, $0.1–0.2 ETH,
  ~1% of mid) than on Deribit ($33–82 BTC, $2.4 ETH, 8–15% of mid — coin-denominated tick). Binance/Bybit
  ETH 24h volume ≥ Deribit's. They are not thin venues for short-dated ATM options.
- **USD vs USDT:** Deribit options settle on a USD index; Binance/Bybit on a USDT index. USDT traded at
  0.9987–0.9988 USD (Coinbase, Deribit `usdt_usd`, index ratios agree). A USDT K-strike option ≈ a USD
  p·K-strike option, a ~$100 strike gap on BTC. Without this adjustment Deribit↔Bybit quotes looked
  "crossed" by $23–34 all the time; after it, they never crossed.
- Local Windows clock is 330–780 ms behind the exchanges and drifts; all timing uses exchange timestamps
  plus a measured offset.

## 2. Gate 1 — does Binance/Bybit lag Deribit?
Data: 4 h synchronized recording (still finishing), 12 options (BTC/ETH, 24-Oct expiry, 3 ATM strikes, C+P),
all three venues + Binance/Deribit perps. Outage windows masked.

| test | result |
|---|---|
| Who leads? (clean 44 min, exchange clock) | **The perp leads every option venue.** Bybit/Binance option mids react at +50–100 ms; Deribit's at +100 ms (exch) / +450–500 ms as received here. LLR perp→venue 3–15. |
| Deribit → Binance/Bybit cross-correlation | Peaks at lag 0 or negative (−50…−500 ms); LLR mostly < 1, i.e. followers tend to move *before* Deribit's update arrives. |
| Event study (local clock) | When a Deribit move arrives here, Binance/Bybit have already done 65–85 % of their net move over [−5 s, +10 s] (ratio 0.40–0.48 at arrival vs 0.57–0.61 at +10 s; corrected 2026-11-01). |
| Incremental regression, 3 h, 60k samples, perp controlled | Deribit adds small but **statistically significant** info: t = 4–6.5, circular-shift null p < 0.005 (0/200). (20-min version was underpowered: t≈1.) |
| Economic size of that info | After a "pure" Deribit move (perp quiet): follower drifts **+$0.33–1.08 per BTC contract** over 1–5 s (CI excl. 0), ETH +$0.02–0.19. Round-trip cost ≈ **$54 BTC / $1.6 ETH**. |

Feed delay as seen here: Deribit option ticker ~520 ms slower than Bybit/Binance option feeds
(p50 ~640 ms after its own timestamp, p95 ~1.3 s). Bybit ~84 ms, Binance ~110 ms.

**Gate 1 verdict:** the lag exists statistically but is ~1–2 % of the cost of trading it. Fail on economics.

## 3. Gate 2 — latency (measured, no orders)
Warm REST RTT p50: Bybit 138 ms, Binance 211 ms, Deribit 255 ms. Combined with the ~640 ms Deribit
feed delay, the earliest this machine can act on a Deribit move is ~0.7–0.9 s after it happens —
by which time the followers have mostly moved. Fail.

## 4. Live paper trade (run 1.5 h of 3 h; 56 instruments: BTC/ETH × 2 expiries × 7 strikes × C/P)
Fill model: IOC at signal price, evaluated against the target venue's book at exchange time
t_decide + RTT/2 (no lookahead); taker 0.03 % of index (caps: Bybit 7 %, Binance 10 % assumed, Deribit 12.5 %);
½-tick slippage per fill + depth walk; S2 adds exercise fee 0.015 %, 2 bp settlement-index haircut,
worst-case USDT strike gap with ±5 bp basis drift.

### S1 latency-follow (hit stale Binance/Bybit quote after a Deribit move; exit after hold)
| venue / hold | trades | gross/trade | fees/trade | net/trade [95% CI] | signal − random-null gross | perm p |
|---|---|---|---|---|---|---|
| Bybit 1 s | 40,825 | −$3.86 | $16.7 | **−$23.56** [−23.94, −23.16] | +$0.03 | 0.08 |
| Bybit 5 s | 18,718 | −$3.96 | $16.7 | −$23.63 | +$0.05 | 0.14 |
| Bybit 30 s | 4,612 | −$4.25 | $16.6 | −$23.80 | +$0.02 | 0.47 |
| Binance 1 s | 13,733 | −$5.89 | $29.1 | **−$39.54** [−40.14, −38.85] | $0.00 | 0.52 |
| Binance 5 s | 6,307 | −$6.05 | $29.1 | −$39.62 | $0.00 | 0.49 |
| Binance 30 s | 1,584 | −$6.74 | $29.0 | −$40.21 | −$0.38 | 0.86 |
Zero-latency (oracle) variants: no better (Bybit 1 s −$25.44, Binance 1 s −$39.45). Win rate 0 %.
Per coin (Bybit 1 s): BTC gross −$6.07 / net −$35.49 (1 contract); ETH gross −$1.26 / net −$9.53 (10 contracts).
Gross is negative because each round trip crosses the spread; Deribit's directional information
(+$0.03/trade vs random) is ~0.2 % of the fee.

### S2 crossed-market arb (buy venue A ask, sell venue B bid, hold to expiry)
| pair | best gross cross, median / p99 / max per 10 s window (USD/contract) | quote updates crossed | killed by costs | trades |
|---|---|---|---|---|
| Deribit ↔ Bybit | BTC −$11.8…−$17.9 / max −$3.0; ETH max −$0.24 | 0 | — | 0 |
| Deribit ↔ Binance | BTC max −$0.83; ETH max −$1.6 | 0 | — | 0 |
| Binance ↔ Bybit | BTC median $0–5, p99 $30, max $40; ETH p99 $0.8, max $1.10 | 77,449 | 77,449 (100 %) | 0 |
Cost hurdle for a locked pair: **≈ $82 per BTC contract, ≈ $2.4 per ETH contract.** Largest cross seen is ~½ of it.
REST spot-check of Binance vs Bybit books: touching (0 cross) at best; crosses are 1–2-tick transients.

## 5. Conclusion so far
No edge. Three independent reasons, any one of which is sufficient:
1. Deribit is not the leader for short-dated ATM options — the underlying perp is, and Binance/Bybit
   react to it faster than Deribit does. Deribit's residual information is real but worth ~$1/BTC contract.
2. This machine sees Deribit option updates ~0.7–0.9 s late (feed delay + RTT); the followers have
   mostly re-quoted by then.
3. Taker fees (0.03 % of index ≈ $24.5 per BTC contract per side) are 10–50× any lag or cross observed.

Only theoretical avenue left: maker-side (posting quotes, earning/avoiding taker fees) on Binance↔Bybit —
a market-making business with inventory risk, not latency arb, and not testable with public data alone.

Pending: full 3 h paper-trade report (~18:10 UTC), full 4 h Gate 1 rerun with outage mask.
