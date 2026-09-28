"""Pipeline: one scanner call for the universe → stock-level rules → best contract per stock."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date

from optionsfilter.client import RobinhoodMCP
from optionsfilter.rules import (
    BUY, SELL_CALL, SELL_PUT, Pick, Rules, best_contract, candidate_expirations, contract_from,
    next_ex_div, score, stock_from_scan, strategy_for, strike_band,
)

SCAN_COLUMNS = [
    {"display_name": "IV Rank", "expression": "ivRank"},
    {"display_name": "IV30", "expression": "atmIv30Day"},  # shown as "Implied volatility"
    {"display_name": "HV30", "expression": "statVol1Month"},  # shown as "Historical volatility"
    {"display_name": "Earnings date", "expression": "fundamental.earningsYmd"},
    {"display_name": "Ex-dividend date", "expression": "fundamental.exYmd"},
]
CONCURRENCY = 6


async def screen(
    rh: RobinhoodMCP,
    rules: Rules,
    mode: str = "all",
    universe: list[str] | None = None,
    symbols: list[str] | None = None,
    limit: int = 15,
    max_evaluate: int = 60,
    today: date | None = None,
) -> tuple[list[Pick], Counter]:
    today = today or date.today()
    stats: Counter = Counter()

    scope = (
        {"expression": "symbol", "predicate": "PREDICATE_ANY_OF", "values": [s.upper() for s in symbols]}
        if symbols
        else {"expression": "symbol", "predicate": "PREDICATE_IN_LIST", "values": universe or ["SP500", "NDX"]}
    )
    liquid = {"expression": "optionsTotalDayVolume", "predicate": "PREDICATE_GREATER_THAN_OR_EQUAL",
              "values": [str(rules.min_options_volume)]}
    stocks = [stock_from_scan(r) for r in await rh.scan([scope, liquid], SCAN_COLUMNS)]
    stats["scanned (options volume ok)"] = len(stocks)

    candidates = []
    for s in stocks:
        strategy = strategy_for(s, rules, mode)
        if strategy:
            candidates.append(Pick(s, strategy))
        else:
            stats["no IV edge (IVR between buy/sell thresholds)"] += 1
    candidates.sort(key=score, reverse=True)

    evaluated: list[Pick] = []
    sem = asyncio.Semaphore(CONCURRENCY)

    async def run(p: Pick) -> Pick:
        async with sem:
            try:
                await evaluate(rh, p, rules, today)
            except Exception as e:  # one bad symbol shouldn't sink the run
                p.problems.append(f"error: {e}")
            return p

    # Evaluate in batches until we have `limit` passes or hit max_evaluate.
    queue = candidates[:max_evaluate]
    while queue and sum(p.passed for p in evaluated) < limit:
        batch, queue = queue[:CONCURRENCY], queue[CONCURRENCY:]
        evaluated += await asyncio.gather(*(run(p) for p in batch))

    for p in evaluated:
        for problem in p.problems:
            stats["rejected: " + _reason(problem, rules)] += 1
    passed = [p for p in evaluated if p.passed]
    near = [p for p in evaluated if not p.passed and p.contract]
    stats["passed"] = len(passed)
    # Passes first; fill the list with the best near misses so you always see `limit` names.
    return (passed + near)[:limit], stats


def _reason(problem: str, rules: Rules) -> str:
    for prefix, label in (
        ("earnings", "earnings before expiry"),
        ("spread", f"bid/ask spread > {rules.max_spread:.0%}"),
        ("OI", f"open interest < {rules.min_open_interest:,}"),
        ("no contract", "no contract in delta range"),
        ("no expiry", f"no {rules.min_dte}–{rules.max_dte} DTE expiry"),
    ):
        if problem.startswith(prefix):
            return label
    return problem


async def evaluate(rh: RobinhoodMCP, pick: Pick, rules: Rules, today: date) -> None:
    s, strategy = pick.stock, pick.strategy
    chains = [c for c in await rh.option_chains(s.symbol) if c["symbol"] == s.symbol] or await rh.option_chains(s.symbol)
    if not chains:
        pick.problems.append("no option chain")
        return
    chain = chains[0]

    expirations, earnings_hit = candidate_expirations(chain["expiration_dates"], s, strategy, rules, today)
    if not expirations:
        pick.problems.append(f"no expiry {rules.min_dte}–{rules.max_dte} DTE")
        return
    if earnings_hit:
        msg = f"earnings {s.earnings:%m-%d} before expiry"
        (pick.notes if strategy == BUY else pick.problems).append(msg)

    option_type = "put" if strategy == SELL_PUT else "call"
    instruments = []
    for expiration in expirations:
        lo, hi = strike_band(s, strategy, (date.fromisoformat(expiration) - today).days)
        instruments += [
            i for i in await rh.option_instruments(chain["id"], expiration, option_type)
            if lo <= float(i["strike_price"]) <= hi
        ]
    quotes = await rh.option_quotes([i["id"] for i in instruments])
    contracts = [c for i in instruments if i["id"] in quotes and (c := contract_from(i, quotes[i["id"]], today))]

    pick.contract, problems = best_contract(contracts, strategy, rules)
    pick.problems += problems

    if strategy == SELL_CALL:
        pick.notes.append("covered only")
        ex, estimated = next_ex_div(s, today)
        if ex and ex <= date.fromisoformat(expiration):
            pick.notes.append(f"ex-div {ex:%m-%d}{' (est.)' if estimated else ''} before expiry: early-assignment risk")
    if strategy == BUY:
        pick.notes.append("call or put = your thesis; must play out inside 30 days")
