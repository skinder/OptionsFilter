"""optionsfilter — 15 stocks that fit the 30–45 DTE framework, with IV rank, delta and liquidity."""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import date

from optionsfilter.auth import FileTokenStorage
from optionsfilter.client import RobinhoodMCP
from optionsfilter.rules import Pick, Rules
from optionsfilter.screener import screen


def pct(x: float | None, digits: int = 0) -> str:
    return "-" if x is None else f"{x:.{digits}%}"


def row(n: int, p: Pick) -> dict[str, str]:
    s, c = p.stock, p.contract
    return {
        "#": str(n),
        "Symbol": s.symbol,
        "Price": f"{s.price:,.2f}",
        "Strategy": p.strategy,
        "IVR": pct(s.ivr),
        "IV30": pct(s.iv30),
        "HV30": pct(s.hv30),
        "IV-HV": "-" if s.iv_hv is None else f"{s.iv_hv * 100:+.0f}",
        "Earnings": f"{s.earnings:%m-%d}" if s.earnings else "-",
        "Expiry": f"{c.expiration[5:]} ({c.dte}d)" if c else "-",
        "Strike": f"{c.strike:g}{c.type[0].upper()}" if c else "-",
        "Delta": f"{c.delta:+.2f}" if c else "-",
        "Theta": f"{c.theta:.3f}" if c and c.theta is not None else "-",
        "Bid/Ask": f"{c.bid:.2f}/{c.ask:.2f}" if c else "-",
        "Spread": pct(c.spread, 1) if c else "-",
        "OI": f"{c.open_interest:,}" if c else "-",
        "Status": "PASS" if p.passed else "; ".join(p.problems),
        "Notes": "; ".join(p.notes),
    }


def print_table(rows: list[dict[str, str]]) -> None:
    cols = list(rows[0])
    widths = {k: max(len(k), *(len(r[k]) for r in rows)) for k in cols}
    print("  ".join(k.ljust(widths[k]) for k in cols).rstrip())
    print("  ".join("-" * widths[k] for k in cols))
    for r in rows:
        print("  ".join(r[k].ljust(widths[k]) for k in cols).rstrip())


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="optionsfilter", description=__doc__)
    p.add_argument("--mode", choices=["all", "sell-put", "sell-call", "buy"], default="all",
                   help="all = sell puts where IVR>50, buy where IVR<30 & IV30<HV30")
    p.add_argument("--symbols", nargs="+", help="screen these tickers instead of an index")
    p.add_argument("--universe", nargs="+", default=["SP500", "NDX"], help="index codes, e.g. SP500 NDX SP100 Russell1000")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--min-dte", type=int, default=30)
    p.add_argument("--max-dte", type=int, default=45)
    p.add_argument("--min-oi", type=int, default=1000, help="contract open interest")
    p.add_argument("--max-spread", type=float, default=0.02, help="bid/ask spread as fraction of mid")
    p.add_argument("--min-options-volume", type=int, default=10_000, help="underlying daily options volume")
    p.add_argument("--csv", help="also write the table to this CSV file")
    p.add_argument("--login", action="store_true", help="authorize with Robinhood and exit")
    p.add_argument("--logout", action="store_true", help="delete cached tokens and exit")
    args = p.parse_args(argv)

    if args.logout:
        FileTokenStorage().clear()
        print("Cached tokens removed.")
        return

    rules = Rules(min_dte=args.min_dte, max_dte=args.max_dte, min_open_interest=args.min_oi,
                  max_spread=args.max_spread, min_options_volume=args.min_options_volume)

    async def run() -> None:
        async with RobinhoodMCP() as rh:
            if args.login:
                print(f"Authorized. Tokens cached at {rh.storage.path}")
                return
            print(f"Screening {' '.join(args.symbols or args.universe)} for {args.min_dte}–{args.max_dte} DTE "
                  f"({args.mode})...", file=sys.stderr)
            picks, stats = await screen(rh, rules, args.mode, args.universe, args.symbols, args.limit)

        if not picks:
            print("Nothing matched.")
        else:
            rows = [row(n, pk) for n, pk in enumerate(picks, 1)]
            print(f"\nAs of {date.today()} — PASS rows meet every rule; the rest are the closest near misses.\n")
            print_table(rows)
            if args.csv:
                with open(args.csv, "w", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                    w.writeheader()
                    w.writerows(rows)
                print(f"\nWrote {args.csv}", file=sys.stderr)
        print("\nFunnel:", ", ".join(f"{k} {v}" for k, v in stats.items()))
        print("Not automated: whether the strike is a support level you'd be happy to own shares at.")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
