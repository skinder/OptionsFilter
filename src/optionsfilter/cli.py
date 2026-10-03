"""optionsfilter — 15 stocks that fit the 30–45 DTE framework, with IV rank, delta and liquidity."""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import date, datetime
from pathlib import Path

from optionsfilter.auth import FileTokenStorage
from optionsfilter.client import RobinhoodMCP
from optionsfilter.rules import BUY_CALL, BUY_PUT, MODES, SELL_CALL, SELL_PUT, Pick, Rules
from optionsfilter.screener import screen


RULE_SUMMARY = {
    SELL_PUT: "30–45 DTE, IV rank > 50, IV30 > HV30, |delta| 15–30, no earnings before expiry",
    SELL_CALL: "30–45 DTE, IV rank > 50, IV30 > HV30, delta 15–30, no earnings; COVERED ONLY, watch ex-div",
    BUY_CALL: "45–90 DTE, IV rank < 30, delta 60–70, contract IV < HV30; bullish thesis",
    BUY_PUT: "45–90 DTE, IV rank < 30, |delta| 60–70, contract IV < HV30; bearish thesis",
}


def pct(x: float | None, digits: int = 0) -> str:
    return "-" if x is None else f"{x:.{digits}%}"


def row(n: int, p: Pick) -> dict[str, str]:
    s, c = p.stock, p.contract
    return {
        "#": str(n),
        "Symbol": s.symbol,
        "Price": f"{s.price:,.2f}",
        "IVR": pct(s.ivr),
        "IV30": pct(s.iv30),
        "HV30": pct(s.hv30),
        "IV-HV": "-" if s.iv_hv is None else f"{s.iv_hv * 100:+.0f}",
        "Trend": s.trend,
        "Earnings": f"{s.earnings:%m-%d}" if s.earnings else "-",
        "Expiry": f"{c.expiration[5:]} ({c.dte}d)" if c else "-",
        "Strike": f"{c.strike:g}{c.type[0].upper()}" if c else "-",
        "Delta": f"{c.delta:+.2f}" if c else "-",
        "Theta": f"{c.theta:.3f}" if c and c.theta is not None else "-",
        "C.IV": pct(c.iv) if c else "-",
        "Bid/Ask": f"{c.bid:.2f}/{c.ask:.2f}" if c else "-",
        "Spread": pct(c.spread, 1) if c else "-",
        "OI": f"{c.open_interest:,}" if c else "-",
        "Vol": f"{c.volume:,}" if c else "-",
        "Status": "PASS" if p.passed else "; ".join(p.problems),
        "Notes": "; ".join(p.notes),
    }


def csv_row(n: int, p: Pick) -> dict:
    """Raw numbers (fractions, not percents) so spreadsheets can sort and chart them."""
    s, c = p.stock, p.contract
    r4 = lambda x: None if x is None else round(x, 4)
    return {
        "rank": n, "status": "PASS" if p.passed else "near miss", "symbol": s.symbol, "name": s.name,
        "price": s.price, "strategy": p.strategy, "iv_rank": r4(s.ivr), "iv30": r4(s.iv30), "hv30": r4(s.hv30),
        "iv_minus_hv": r4(s.iv_hv), "sma50": s.sma50,
        "vs_sma50": r4(s.price / s.sma50 - 1) if s.sma50 else None, "options_volume": s.options_volume, "options_open_interest": s.options_oi,
        "lists": " ".join(s.sources),
        "earnings_date": s.earnings.isoformat() if s.earnings else None,
        "expiration": c.expiration if c else None, "dte": c.dte if c else None,
        "type": c.type if c else None, "strike": c.strike if c else None,
        "delta": c.delta if c else None, "theta": c.theta if c else None, "contract_iv": c.iv if c else None,
        "bid": c.bid if c else None, "ask": c.ask if c else None, "mid": r4(c.mid) if c else None,
        "spread_pct": r4(c.spread) if c else None, "open_interest": c.open_interest if c else None,
        "contract_volume": c.volume if c else None,
        "problems": "; ".join(p.problems), "notes": "; ".join(p.notes),
    }


def write_csv(path: Path, results: dict[str, list[Pick]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [csv_row(n, pk) for picks in results.values() for n, pk in enumerate(picks, 1)]
    if not rows:
        return
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def print_table(rows: list[dict[str, str]]) -> None:
    cols = list(rows[0])
    widths = {k: max(len(k), *(len(r[k]) for r in rows)) for k in cols}
    print("  ".join(k.ljust(widths[k]) for k in cols).rstrip())
    print("  ".join("-" * widths[k] for k in cols))
    for r in rows:
        print("  ".join(r[k].ljust(widths[k]) for k in cols).rstrip())


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="optionsfilter", description=__doc__)
    p.add_argument("--mode", choices=list(MODES), default="all",
                   help="all = sell put + sell call (IVR>50) and buy call + buy put (IVR<30 & IV30<HV30)")
    p.add_argument("--symbols", nargs="+", help="screen only these tickers instead of the default universe")
    p.add_argument("--limit", type=int, default=15, help="rows per strategy")
    p.add_argument("--min-dte", type=int, default=30, help="sells")
    p.add_argument("--max-dte", type=int, default=45, help="sells")
    p.add_argument("--buy-min-dte", type=int, default=45)
    p.add_argument("--buy-max-dte", type=int, default=90)
    p.add_argument("--min-oi", type=int, default=100, help="contract open interest")
    p.add_argument("--min-volume", type=int, default=10, help="contract volume today")
    p.add_argument("--min-chain-oi", type=int, default=10_000, help="total options open interest on the underlying")
    p.add_argument("--max-spread", type=float, default=0.03,
                   help="allowed spread = max(--spread-floor, this × mid); default 3%%")
    p.add_argument("--spread-floor", type=float, default=0.05, help="dollars")
    p.add_argument("--csv", help="CSV path (default: results/optionsfilter_<mode>_<date>_<time>.csv)")
    p.add_argument("--no-csv", action="store_true", help="don't save a CSV")
    p.add_argument("--login", action="store_true", help="authorize with Robinhood and exit")
    p.add_argument("--logout", action="store_true", help="delete cached tokens and exit")
    args = p.parse_args(argv)
    if args.symbols:  # accept "AAPL MSFT" or "AAPL,MSFT" as one argument (VS Code prompt)
        args.symbols = [s for arg in args.symbols for s in arg.replace(",", " ").split()]

    if args.logout:
        FileTokenStorage().clear()
        print("Cached tokens removed.")
        return

    rules = Rules(min_dte=args.min_dte, max_dte=args.max_dte, buy_min_dte=args.buy_min_dte,
                  buy_max_dte=args.buy_max_dte, min_open_interest=args.min_oi, min_volume=args.min_volume,
                  min_underlying_oi=args.min_chain_oi, max_spread=args.max_spread, spread_floor=args.spread_floor)

    async def run() -> None:
        async with RobinhoodMCP() as rh:
            if args.login:
                print(f"Authorized. Tokens cached at {rh.storage.path}")
                return
            label = ' '.join(args.symbols) if args.symbols else (
                "top 100 stocks + top 100 incl. ETFs (by options volume) + Robinhood 100 most popular")
            print(f"Screening {label} "
                  f"({args.mode})...", file=sys.stderr)
            results, stats = await screen(rh, rules, args.mode, args.symbols, args.limit)

        print(f"\nAs of {date.today()} — PASS rows meet every rule; the rest are the closest near misses.")
        for strategy, picks in results.items():
            print(f"\n=== {strategy} — {RULE_SUMMARY[strategy]}\n")
            if picks:
                print_table([row(n, pk) for n, pk in enumerate(picks, 1)])
            else:
                print("  no stocks qualify today")
        total = sum(len(v) for v in results.values())
        if total and not args.no_csv:
            path = Path(args.csv or f"results/optionsfilter_{args.mode}_{datetime.now():%Y-%m-%d_%H%M}.csv")
            write_csv(path, results)
            print(f"\nSaved {total} rows to {path.resolve()}")
        print("\nFunnel:", ", ".join(f"{k} {v}" for k, v in stats.items()))
        print("Not automated: whether the strike is a support level you'd be happy to own shares at.")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
