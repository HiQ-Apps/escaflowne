# Escaflowne 🜚

A trend-following bot for micro gold futures, ported from [Celeri](#sibling-bots).

> _"A bot named after a giant mecha that runs on belief and Ancient Atlantean technology — naturally chosen to trade gold."_

---

## What this is

Escaflowne trades **MGC** (CME Micro Gold Futures) on IBKR using an EMA crossover with an ADX trend filter. It's a faithful port of Celeri's strategy, retuned for gold's price scale.

The thesis being tested:
> If Celeri's edge on equity index futures (MES/MNQ) is real and not specific to a single market, the same logic should produce edge on uncorrelated instruments. Gold is an uncorrelated instrument.

The thesis being de-tested at every step:
> If the edge IS specific to equity indices, we should find out before deploying capital, not after.

---

## Strategy in one paragraph

Long-only EMA(8) over EMA(21) crossover on 5-minute bars. Take the entry only if ADX(14) ≥ 10 — i.e. there's a real trend, not just chop. Place an ATR-based stop (4× ATR) at the time of entry. Exit on the EMA cross-down. Risk-tier position sizing as the account grows. Flatten everything Friday at 16:59 ET because gold gaps over the weekend on news. No discretionary overrides, no tweaking mid-trade.

---

## Validation history

The strategy passed every gate it was tested against:

| Test | Verdict |
|---|---|
| Eyeball test on raw MGC data | Trades cluster sensibly in trends, avoid chop ✓ |
| Regime-conditional test (annual buckets, fixed params) | 7 of 8 years profitable ✓ |
| Walk-forward with 108-parameter sweep (10 walks, 24mo train / 6mo test) | 10 of 10 walks profitable, mean PF 2.11 ✓ |
| Optimized full-history backtest | PF 2.22, max DD -1.22% ✓ |
| Monte Carlo bootstrap (10K paths) | 100% profitable, 0% blowup probability ✓ |
| Realistic position-sizing rerun | Holds up under risk-tiered sizing ✓ |

Optimized parameters chosen by the walk-forward (ADX=10, EMA(8,21), ATR×4, MIN_STOP_POINTS=2.0).

Backtest numbers are not promises. Paper trading is the next gate.

---

## Project layout

```
escaflowne/
├── .env                       # DATABENTO_API_KEY (gitignored)
├── README.md
├── run.py                     # live trading entry point
│
├── config/
│   ├── base.py
│   ├── backtest.py            # MGC-specific values
│   └── live.py                # live config (mirrors backtest)
│
├── strategy/
│   ├── indicators.py
│   └── signals.py
│
├── backtest/
│   ├── data_loader.py         # MGC pipeline: continuous contract, 1m + 5m, gold roll schedule
│   ├── engine.py              # single-instrument MGC engine (cloned from Celeri)
│   ├── walkforward.py         # regime-conditional + walk-forward optimization
│   ├── montecarlo.py          # bootstrap path simulation
│   └── stats.py               # Sharpe, Sortino, MFE/MAE, by-context breakdowns
│
├── live/
│   ├── contracts.py           # schedule-driven contract month resolution
│   ├── broker.py              # IBKR connection + MGC qualification
│   ├── feed.py                # 5-min bar streaming with EMA cache fix
│   ├── alerts.py              # Discord webhooks
│   ├── state_publisher.py     # state.json for dashboards/monitors
│   └── runner.py              # the brain: signals, orders, kill switches
│
├── research/
│   ├── pull_mgc.py            # Databento → CSV.zst → parquet
│   ├── verify.py              # Sharpe sanity checks, walk DD inspection
│   └── smoke_broker.py        # paper Gateway smoke test
│
└── data/
    ├── raw/                   # Databento CSV.zst pulls
    └── processed/             # continuous-contract parquets
```

---

## Sibling bots

Escaflowne is one of a planned trio of trend-following bots, each running independently on different instruments. They share strategy DNA but no code — each gets its own repo, its own state, its own bug-fix history.

| Bot | Instrument | Repo | Status |
|---|---|---|---|
| **Celeri** | MES (E-mini S&P) | (separate repo) | live, paper-stage, scaling |
| **Celeri** | MNQ (E-mini Nasdaq) | (within Celeri) | will re-enable when account >$7K |
| **Escaflowne** | MGC (Micro Gold) | (this repo) | building live code, paper soon |

The intent is diversification across uncorrelated trends. When equities trend, Celeri eats. When gold trends, Escaflowne eats. When both trend, both eat. When neither trends, both sit on their hands and you pay commissions for the privilege.

---

## Operational plan

**Phase 1 — Paper trading (60-90 days)**
- Run on IBKR paper Gateway (port 4002), separate from Celeri's live Gateway (port 4001)
- Two Gateway instances on the same Windows VPS
- Validate that real fills track backtest-modeled fills within ~30%
- If not, debug. If yes, proceed.

**Phase 2 — Live deployment (when account allows)**
- Gated on Celeri growing the IBKR account to ~$10K liquid
- MGC needs $5,074 overnight margin per contract at IBKR
- Want at least $2,500 buffer for adverse moves (Monte Carlo p5 DD)
- = ~$7,500 minimum to safely deploy 1 MGC contract live

**Phase 3 — Add bots**
- When account hits $25K+, re-enable MNQ (currently disabled in Celeri)
- Three bots running simultaneously, each on its own instrument

---

## Operational commitments to myself

- Don't go live before paper trading validates fill assumptions
- Don't fund Escaflowne's account by raiding Celeri's working capital
- Don't size up faster than the tier table allows, no matter how good a regime feels
- Don't tweak the strategy once it's live — if a change is needed, paper trade the change first
- Don't watch tick-by-tick once it's running — that's the whole point of automation

---

## Quick reference

**Run a backtest:**
```bash
python -m backtest.engine
```

**Walk-forward validation:**
```bash
python -m backtest.walkforward --phase both --workers 4
```

**Monte Carlo bootstrap:**
```bash
python -m backtest.montecarlo
```

**Smoke test the live code (requires paper Gateway on 4002):**
```bash
python -m research.smoke_broker
```

**Pull fresh data from Databento:**
```bash
python -m research.pull_mgc
```

---

## Honest disclaimers

- Backtests are backtests. Slippage, latency, and fill quality in live conditions will degrade real Sharpe vs. measured Sharpe by 30-50%.
- The 2019-2026 sample window is gold-bull-heavy. The strategy hasn't been tested on a multi-year sideways-gold regime (something like 2013-2018). That's the next falsifiability test if a regime shift hits.
- Position sizing scales with account growth, but slippage scales worse than linearly. Multi-contract MGC during news events is going to fill worse than backtested.
- This is not investment advice. This is one person's project to test whether a working trend-follower transfers to gold. Don't deploy this with anyone else's money.

---

_"It's better to know than to wonder."_
