# OptionsFilter

Screens for **15 stocks that fit the 1-month (30–45 DTE) options framework** using the Robinhood MCP server. It shows IV rank, IV30 vs HV30, the best-fit contract's delta and theta, and liquidity for each. It's read-only and never places orders.

| Strategy | Rule |
|---|---|
| **Sell put** | IV rank > 50, \|delta\| 15–30, no earnings before expiry |
| **Sell call** | Same, covered only, plus an ex-dividend-before-expiry flag (early assignment) |
| **Buy call/put** | IV rank < 30 and IV30 < HV30 (the direction is your thesis) |
| **Liquidity** | Underlying options volume > 10k/day, contract OI > 1k, bid/ask spread < 2% of mid |

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/optionsfilter --login     # opens robinhood.com once; tokens are cached in ~/.config/optionsfilter/
```

## Run

```bash
.venv/bin/optionsfilter                          # S&P 500 + Nasdaq-100, sell puts & buys, top 15
.venv/bin/optionsfilter --mode sell-put
.venv/bin/optionsfilter --mode sell-call         # covered calls, with ex-div flags
.venv/bin/optionsfilter --mode buy
.venv/bin/optionsfilter --symbols AAPL KO XOM MRK NKE
.venv/bin/optionsfilter --csv today.csv
```

The thresholds can be changed with `--min-dte --max-dte --min-oi --max-spread --min-options-volume --limit --universe`.

## How it works

1. **One scanner call** returns IV rank, IV30, HV30, earnings date and ex-dividend date for every stock in the universe with ≥ 10k options volume.
2. **Stock rules:** IV rank > 50 means sell. IV rank < 30 with IV30 < HV30 means buy. Anything in between has no edge and is dropped.
3. **Expiry:** the latest one within 30–45 DTE. Sellers get an earlier in-window expiry if that avoids earnings.
4. **Contract:** from the chain, the tool takes the contract closest to delta 0.22 (0.50 for buys) that passes OI and spread. If none passes, it shows the closest one with the failures listed.
5. **Output:** PASS rows first, then the best near misses so you always see 15 names, plus a funnel of rejection counts.

Not automated: whether the strike is a support level you'd be happy to own shares at.

## Files

- `rules.py`: the framework as pure functions, with every threshold in `Rules`
- `screener.py`: the scanner → chain → quotes pipeline
- `client.py` / `auth.py`: the read-only MCP client and OAuth
- `cli.py`: the output table and CSV

```bash
.venv/bin/pytest
```
