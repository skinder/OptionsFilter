"""Pipeline: scanner call(s) for the universe → stock-level rules → best contract per stock."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date

from optionsfilter.client import RobinhoodMCP
from optionsfilter.rules import (
    BUYS, MODES, SELL_CALL, Pick, Rules, Stock, best_contract, candidate_expirations, contract_from,
    next_ex_div, option_type, score, stock_from_scan, strategies_for, strike_band,
)

SCAN_COLUMNS = [
    {"display_name": "IV Rank", "expression": "ivRank"},
    {"display_name": "IV30", "expression": "atmIv30Day"},  # shown as "Implied volatility"
    {"display_name": "HV30", "expression": "statVol1Month"},  # shown as "Historical volatility"
    {"display_name": "Earnings date", "expression": "fundamental.earningsYmd"},
    {"display_name": "Ex-dividend date", "expression": "fundamental.exYmd"},
    {"display_name": "SMA50", "expression": 'closeAvg(candleCount=50, candlePeriod="1d", session="all")'},
    {"display_name": "Options OI", "expression": "optionsTotalOpenInterest"},
]
CONCURRENCY = 6
SCAN_CAP = 200  # rows per scanner response
PRICE_BANDS = [(0, 10), (10, 25), (25, 50), (50, 100), (100, 250), (250, 600), (600, None)]

POPULAR_LIST = "100 most popular"
TOP_N = 100
# Universe sources (tags shown in the CSV "lists" column)
TOP_STOCKS, TOP_ALL, POPULAR = "top100-stocks", "top100-incl-etfs", "rh-popular"


def _tickers(symbols: list[str]) -> dict:
    """symbol ANY_OF filter. The scanner matches share classes as BRK/B (but returns BRK.B)."""
    return _f("symbol", "ANY_OF", [t.upper().replace(".", "/") for t in symbols])


def _f(expression: str, predicate: str, values: list) -> dict:
    return {"expression": expression, "predicate": f"PREDICATE_{predicate}", "values": [str(v) for v in values]}


async def scan_all(rh: RobinhoodMCP, filters: list[dict]) -> list[dict]:
    """Scanner rows for `filters`, splitting by price band when the response is capped."""
    rows, total = await rh.scan(filters, SCAN_COLUMNS)
    return rows if len(rows) >= total else await scan_bands(rh, filters)


async def scan_bands(rh: RobinhoodMCP, filters: list[dict]) -> list[dict]:
    by_ticker: dict[str, dict] = {}
    for lo, hi in PRICE_BANDS:
        band = [_f("tradeAllDay.price", "GREATER_THAN_OR_EQUAL", [lo])]
        if hi is not None:
            band.append(_f("tradeAllDay.price", "LESS_THAN", [hi]))
        band_rows, band_total = await rh.scan(filters + band, SCAN_COLUMNS)
        if len(band_rows) < band_total:
            print(f"warning: price band {lo}-{hi} truncated ({len(band_rows)}/{band_total})")
        by_ticker.update({r["ticker"]: r for r in band_rows})
    return list(by_ticker.values())


async def load_universe(rh: RobinhoodMCP, rules: Rules, symbols: list[str] | None = None) -> list[Stock]:
    """Distinct stocks + ETFs to screen, each carrying IV rank / IV30 / HV30 / earnings:

      1. top 100 stocks by today's options volume (Russell 3000)
      2. top 100 stocks + ETFs by today's options volume (adds SPY, QQQ, IWM, TLT, IBIT...)
      3. Robinhood's "100 most popular" list — kept whole, no volume floor

    `symbols` replaces all three with an explicit list. No price filter anywhere: capped scanner
    responses are split by price band so every match is kept.
    """
    if symbols:
        rows = await scan_all(rh, [_tickers(symbols)])
        return [stock_from_scan(r) for r in rows]

    def top(rows: list[dict]) -> list[Stock]:
        stocks = [stock_from_scan(r) for r in rows]
        return sorted(stocks, key=lambda s: s.options_volume, reverse=True)[:TOP_N]

    sources = {
        TOP_STOCKS: top(await scan_top(rh, [_f("symbol", "IN_LIST", ["Russell3000"])], rules.min_options_volume, TOP_N)),
        TOP_ALL: top(await scan_top(rh, [], rules.min_options_volume, TOP_N)),
        POPULAR: [stock_from_scan(r) for r in await scan_all(rh, [_tickers(await rh.curated_list(POPULAR_LIST))])],
    }
    merged: dict[str, Stock] = {}
    for tag, stocks in sources.items():
        for st in stocks:
            merged.setdefault(st.symbol, st).sources.append(tag)
    return sorted(merged.values(), key=lambda s: s.options_volume, reverse=True)


async def scan_top(rh: RobinhoodMCP, scope: list[dict], min_volume: int, top: int) -> list[dict]:
    """Rows covering the `top` names by options volume, in as few scanner calls as possible:
    raise the volume floor until everything fits in one response (the scanner is rate-limited)."""
    floor = min_volume
    while True:
        rows, total = await rh.scan(scope + [_f("optionsTotalDayVolume", "GREATER_THAN_OR_EQUAL", [floor])], SCAN_COLUMNS)
        if len(rows) >= total:
            return rows
        higher = int(floor * 2)
        rows_hi, total_hi = await rh.scan(
            scope + [_f("optionsTotalDayVolume", "GREATER_THAN_OR_EQUAL", [higher])], SCAN_COLUMNS
        )
        if total_hi < top:  # overshot: split the lower floor by price instead
            return await scan_bands(rh, scope + [_f("optionsTotalDayVolume", "GREATER_THAN_OR_EQUAL", [floor])])
        if len(rows_hi) >= total_hi:
            return rows_hi
        floor = higher


async def screen(
    rh: RobinhoodMCP,
    rules: Rules,
    mode: str = "all",
    symbols: list[str] | None = None,
    limit: int = 15,
    max_evaluate: int = 40,
    today: date | None = None,
) -> tuple[dict[str, list[Pick]], Counter]:
    """Returns {strategy: up to `limit` picks (passes first, then near misses)} and a funnel."""
    today = today or date.today()
    stats: Counter = Counter()

    stocks = await load_universe(rh, rules, symbols)
    stats["universe (distinct)"] = len(stocks)
    for tag in (TOP_STOCKS, TOP_ALL, POPULAR):
        if n := sum(tag in s.sources for s in stocks):
            stats[tag] = n

    candidates: dict[str, list[Pick]] = {st: [] for st in MODES[mode]}
    for s in stocks:
        if s.options_oi is not None and s.options_oi < rules.min_underlying_oi:
            stats[f"underlying options OI < {rules.min_underlying_oi:,}"] += 1
            continue
        strategies = strategies_for(s, rules, mode)
        if not strategies:
            stats["no IV edge"] += 1
        for st in strategies:
            candidates[st].append(Pick(s, st))

    sem = asyncio.Semaphore(CONCURRENCY)
    chains: dict[str, asyncio.Task] = {}  # one chain lookup per symbol, shared across strategies

    async def run(p: Pick) -> Pick:
        async with sem:
            try:
                await evaluate(rh, p, rules, today, chains)
            except Exception as e:  # one bad symbol shouldn't sink the run
                p.problems.append(f"error: {e}")
            return p

    async def run_strategy(strategy: str) -> list[Pick]:
        """Best candidates first, in batches, until `limit` pass or `max_evaluate` are checked."""
        queue = sorted(candidates[strategy], key=score, reverse=True)[:max_evaluate]
        evaluated: list[Pick] = []
        while queue and sum(p.passed for p in evaluated) < limit:
            batch, queue = queue[:CONCURRENCY], queue[CONCURRENCY:]
            evaluated += await asyncio.gather(*(run(p) for p in batch))
        return evaluated

    results: dict[str, list[Pick]] = {}
    for strategy, evaluated in zip(candidates, await asyncio.gather(*(run_strategy(st) for st in candidates))):
        for p in evaluated:
            for problem in p.problems:
                stats["rejected: " + _reason(problem, rules)] += 1
        passed = [p for p in evaluated if p.passed]
        near = [p for p in evaluated if not p.passed and p.contract]
        stats[f"{strategy.lower()} passed"] = len(passed)
        results[strategy] = (passed + near)[:limit]
    return results, stats


def _reason(problem: str, rules: Rules) -> str:
    for prefix, label in (
        ("earnings", "earnings before expiry"),
        ("spread", f"spread > max(${rules.spread_floor:.2f}, {rules.max_spread:.0%} of mid)"),
        ("OI", f"open interest < {rules.min_open_interest:,}"),
        ("volume", f"contract volume < {rules.min_volume:,}"),
        ("contract IV", "contract IV ≥ HV30 (buys)"),
        ("no contract", "no contract in delta range"),
        ("no expiry", "no expiry in DTE window"),
    ):
        if problem.startswith(prefix):
            return label
    return problem


async def evaluate(rh: RobinhoodMCP, pick: Pick, rules: Rules, today: date, cache: dict | None = None) -> None:
    s, strategy = pick.stock, pick.strategy
    cache = {} if cache is None else cache
    if s.symbol not in cache:
        cache[s.symbol] = asyncio.ensure_future(rh.option_chains(s.symbol))
    all_chains = await cache[s.symbol]
    chains = [c for c in all_chains if c["symbol"] == s.symbol] or all_chains
    if not chains:
        pick.problems.append("no option chain")
        return
    chain = chains[0]

    expirations, earnings_hit = candidate_expirations(chain["expiration_dates"], s, strategy, rules, today)
    if not expirations:
        lo, hi = rules.dte_window(strategy)
        pick.problems.append(f"no expiry {lo}–{hi} DTE")
        return
    if earnings_hit:
        msg = f"earnings {s.earnings:%m-%d} before expiry"
        (pick.notes if strategy in BUYS else pick.problems).append(msg)

    instruments = []
    for expiration in expirations:
        lo, hi = strike_band(s, strategy, (date.fromisoformat(expiration) - today).days)
        instruments += [
            i for i in await rh.option_instruments(chain["id"], expiration, option_type(strategy))
            if lo <= float(i["strike_price"]) <= hi
        ]
    quotes = await rh.option_quotes([i["id"] for i in instruments])
    contracts = [c for i in instruments if i["id"] in quotes and (c := contract_from(i, quotes[i["id"]], today))]

    pick.contract, problems = best_contract(contracts, strategy, rules, s)
    pick.problems += problems

    if strategy == SELL_CALL:
        pick.notes.append("covered only")
        ex, estimated = next_ex_div(s, today)
        if ex and ex <= date.fromisoformat(expiration):
            pick.notes.append(f"ex-div {ex:%m-%d}{' (est.)' if estimated else ''} before expiry: early-assignment risk")
    if strategy in BUYS:
        pick.notes.append("needs a thesis that plays out inside 30 days")
