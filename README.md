# OptionsFilter

Screens for **15 stocks that fit the 1-month (30–45 DTE) options framework** using the Robinhood MCP server. It shows IV rank, IV30 vs HV30, the best-fit contract's delta and theta, and liquidity for each. It's read-only and never places orders.

| Strategy | Rule |
|---|---|
| **Sell put** | IV rank > 50, \|delta\| 15–30, no earnings before expiry |
| **Sell call** | Same, covered only, plus an ex-dividend-before-expiry flag (early assignment) |
| **Buy call** | IV rank < 30 and IV30 < HV30, delta ~0.50, a bullish thesis that plays out within 30 days |
| **Buy put** | Same as buy call, for the bearish side. The Trend column (price vs its 50-day average) is a hint for choosing between them, not a signal |
| **Liquidity** | Underlying options volume > 10k/day, contract OI > 1k, bid/ask spread < 2% of mid |

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/optionsfilter --login     # opens robinhood.com once; tokens are cached in ~/.config/optionsfilter/
```

## Run

```bash
.venv/bin/optionsfilter                          # top 100 most-traded stocks (by options volume), 15 results
.venv/bin/optionsfilter --universe popular       # Robinhood's "100 most popular" list (includes ETFs)
.venv/bin/optionsfilter --etfs                   # most-traded incl. ETFs (SPY, QQQ, IWM, TLT…)
.venv/bin/optionsfilter --top 200                # widen the most-traded universe
.venv/bin/optionsfilter --universe SP500 NDX     # index members instead
.venv/bin/optionsfilter --mode sell              # sell put + sell call only
.venv/bin/optionsfilter --mode buy               # buy call + buy put only
.venv/bin/optionsfilter --mode sell-put          # or sell-call, buy-call, buy-put
.venv/bin/optionsfilter --symbols AAPL KO XOM MRK NKE
.venv/bin/optionsfilter --csv today.csv
```

The thresholds can be changed with `--min-dte --max-dte --min-oi --max-spread --min-options-volume --limit --universe`.

## How it works

1. **Universe:** the top 100 Russell 3000 stocks by today's options volume (default), Robinhood's "100 most popular" list, or index members. The Robinhood scanner returns IV rank, IV30, HV30, earnings date and ex-dividend date for each stock with ≥ 10k options volume. Because the scanner caps each response at 200 rows and is rate-limited, the tool raises the volume floor or splits the scan by price range so it uses as few calls as possible, and backs off when it's rate-limited.
2. **Stock rules:** IV rank > 50 makes a stock a candidate for both sell put and sell call. IV rank < 30 with IV30 < HV30 makes it a candidate for both buy call and buy put. Anything in between has no edge and is dropped.
3. **Expiry:** every expiry within 30–45 DTE is checked, because a newly listed weekly often has no open interest yet. Sellers only use expiries before earnings when there are any.
4. **Contract:** from the chain, the tool takes the contract closest to delta 0.22 (0.50 for buys) that passes OI and spread. If none passes, it shows the closest one with the failures listed.
5. **Output:** one table per strategy, each with up to `--limit` (15) rows: PASS rows first, then the best near misses. Then a funnel of rejection counts, plus one combined CSV in `results/` with a `strategy` column.

Not automated: whether the strike is a support level you'd be happy to own shares at.

## Files

- `rules.py`: the framework as pure functions, with every threshold in `Rules`
- `screener.py`: the scanner → chain → quotes pipeline
- `client.py` / `auth.py`: the read-only MCP client and OAuth
- `cli.py`: the output table and CSV

```bash
.venv/bin/pytest
```
