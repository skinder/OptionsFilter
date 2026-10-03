# OptionsFilter

Screens for **15 stocks that fit the 1-month (30–45 DTE) options framework** using the Robinhood MCP server. It shows IV rank, IV30 vs HV30, the best-fit contract's delta and theta, and liquidity for each. It's read-only and never places orders.

| Strategy | Rule |
|---|---|
| **Sell put** | 30–45 DTE. IV rank > 50 **and** IV30 > HV30, \|delta\| 15–30, no earnings before expiry |
| **Sell call** | Same, covered only, plus an ex-dividend-before-expiry flag (early assignment) |
| **Buy call** | 45–90 DTE. IV rank < 30, delta 0.60–0.70, and the **contract's own IV < HV30** (IV30 is a chain average that hides skew). A bullish thesis is yours to supply |
| **Buy put** | Same as buy call, for the bearish side. The Trend column (price vs its 50-day average) is a hint for choosing between them, not a signal |
| **Liquidity** | Contract open interest ≥ 100, contract volume ≥ 10 today, spread ≤ max($0.05, 3% of mid), underlying's total options open interest ≥ 10,000 |

**Definitions** (from Robinhood's scanner):
- IV rank = (IV − 52-week low) / (52-week high − 52-week low).
- HV30 = annualized close-to-close standard deviation of daily log returns over one calendar month.
- Earnings dates may be estimates, not confirmed dates.
- Volume and spread are only meaningful during market hours, so run it mid-session.

**Universe:** a distinct list built from three sources (about 190 names today):
1. the top 100 stocks by today's options volume
2. the top 100 stocks and ETFs by today's options volume (adds SPY, QQQ, IWM, TLT, IBIT…)
3. Robinhood's "100 most popular" list, kept whole with no volume minimum

Nothing is dropped by price. The CSV's `lists` column shows which source(s) each name came from.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/optionsfilter --login     # opens robinhood.com once; tokens are cached in ~/.config/optionsfilter/
```

## Run

```bash
.venv/bin/optionsfilter                          # all 4 strategies over the universe above
.venv/bin/optionsfilter --mode sell              # sell put + sell call only
.venv/bin/optionsfilter --mode buy               # buy call + buy put only
.venv/bin/optionsfilter --mode sell-put          # or sell-call, buy-call, buy-put
.venv/bin/optionsfilter --symbols AAPL KO BRK.B   # only these tickers
.venv/bin/optionsfilter --csv today.csv
```

The thresholds can be changed with `--min-dte --max-dte --buy-min-dte --buy-max-dte --min-oi --min-volume --min-chain-oi --max-spread --spread-floor --limit`.

## How it works

1. **Universe:** the three lists above, merged and de-duplicated. A few Robinhood scanner calls return IV rank, IV30, HV30, the 50-day average, earnings and ex-dividend dates. The scanner caps each response at 200 rows sorted by price, so capped results are split by price range to keep every name. Calls are kept few and back off on rate limits.
2. **Stock rules:** IV rank > 50 makes a stock a candidate for both sell put and sell call. IV rank < 30 with IV30 < HV30 makes it a candidate for both buy call and buy put. Anything in between has no edge and is dropped.
3. **Expiry:** every expiry in the window (30–45 DTE for sells, 45–90 for buys) is checked, because a newly listed weekly often has no open interest yet. Sellers only use expiries before earnings when there are any.
4. **Contract:** from the chain, the tool takes the contract closest to delta 0.22 (0.65 for buys) that passes every contract rule. If none passes, it shows the closest one with the failures listed.
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
