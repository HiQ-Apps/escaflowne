# Escaflowne — Celeri-on-MGC

Test whether Celeri's EMA-crossover-with-ADX-trend edge transfers from
equity index futures (MES/MNQ) to gold futures (MGC).

## Phase 0a (current): Celeri's exact strategy params, run on MGC

If this produces edge, the EMA+ADX trend-follow generalizes across instruments
and we have a second uncorrelated bot for the cost of a config file. If it
doesn't, we know Celeri's edge is more fragile than it looks before we sweep.

## Project layout

```
escaflowne/
├── .env                     # DATABENTO_API_KEY (gitignored)
├── backtest/
│   ├── data_loader.py       # MGC pipeline: continuous contract, 1m + 5m, gold roll schedule
│   ├── engine.py            # single-instrument MGC engine (cloned from Celeri)
│   └── stats.py             # comprehensive statistics (verbatim from Celeri)
├── strategy/
│   ├── indicators.py        # EMA9/21, ADX14, ATR14, rolling 5000-bar window
│   └── signals.py           # entry crossover + ADX filter (Celeri logic, MGC params)
├── config/
│   ├── base.py              # MGC contract specs ($0.10 tick, $10/pt)
│   └── backtest.py          # Celeri's exact strategy params + MGC scale calibrations
├── research/
│   └── pull_mgc.py          # Databento parent-symbol pull
├── data/
│   ├── raw/                 # CSV.zst from Databento (gitignored)
│   └── processed/           # parquet caches (gitignored)
└── reports/                 # backtest output (gitignored)
```

## Setup

```powershell
# Create venv and install deps
python -m venv .venv
.venv\Scripts\activate
python -m pip install databento python-dotenv pandas pandas-ta pyarrow numpy zstandard

# Copy .env.example to .env and fill in DATABENTO_API_KEY
copy .env.example .env
```

## Running Phase 0a

```powershell
# 1. Pull 7 years of MGC 1-min bars (~$5-10 in Databento credits)
python research/pull_mgc.py

# 2. Process raw -> continuous contract -> 1m + 5m parquet
python -m backtest.data_loader data\raw\mgc_1min_2019-01-01_2026-05-09.csv.zst

# 3. Precompute indicators (rolling 5000-bar window)
python -c "from backtest.data_loader import precompute_indicators; precompute_indicators()"

# 4. Run the backtest
python -m backtest.engine
```

## Decision matrix after Phase 0a

| Result | Interpretation | Next step |
|---|---|---|
| Sharpe > 1.0, PF > 1.3 | Edge transfers cleanly | Port to live, paper trade |
| Sharpe 0.5-1.0, PF 1.0-1.3 | Borderline | Phase 0b: retune scale params (MIN_STOP, EMA_MIN_RISK) |
| Sharpe < 0.5, PF < 1.0 | Edge doesn't transfer as-is | Phase 0c: targeted parameter sweep with walk-forward |
| Negative expectancy | Edge is fragile/equity-specific | Try M6E or kill the idea |

The discipline: don't escalate past Phase 0a unless results genuinely justify it.