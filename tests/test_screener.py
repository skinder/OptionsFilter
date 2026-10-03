import asyncio
from datetime import date

from optionsfilter.client import is_read_only
from optionsfilter.rules import (
    BUY_CALL, BUY_PUT, SELL_CALL, SELL_PUT, Contract, Rules, Stock, best_contract, candidate_expirations, next_ex_div,
    stock_from_scan, strategies_for, ymd,
)
from optionsfilter.screener import screen

TODAY = date(2026, 9, 27)
RULES = Rules()


def stock(**kw) -> Stock:
    base = dict(symbol="XYZ", name="Xyz", price=100.0, ivr=0.6, iv30=0.40, hv30=0.35,
                options_volume=50_000, earnings=date(2026, 11, 20), last_ex_div=None)
    return Stock(**{**base, **kw})


def contract(delta, bid=1.00, ask=1.02, oi=5000, strike=90.0, type_="put") -> Contract:
    return Contract("2026-11-06", 40, type_, strike, delta, -0.05, 0.4, bid, ask, oi, 100)


# --- parsing (shapes captured from the live scanner) ---------------------------------

def test_scan_row_parsing():
    row = {"ticker": "META", "columns": {
        "Name": "Meta", "Last": "700.5", "IV Rank": "0.6181657848324515", "Implied volatility": "0.3213864",
        "Historical volatility": "0.47300433217741306", "Options volume": "250000",
        "Earnings date": "2.0261029e+07", "Ex-dividend date": "2.0260915e+07"}}
    s = stock_from_scan(row)
    assert s.symbol == "META" and s.ivr > 0.61 and s.earnings == date(2026, 10, 29)
    assert s.last_ex_div == date(2026, 9, 15)
    assert round(s.iv_hv, 3) == -0.152
    assert ymd("") is None and ymd("garbage") is None


# --- stock-level rules ---------------------------------------------------------------

def test_strategy_selection():
    assert strategies_for(stock(ivr=0.55), RULES) == [SELL_PUT, SELL_CALL]
    assert strategies_for(stock(ivr=0.55), RULES, "sell-call") == [SELL_CALL]
    assert strategies_for(stock(ivr=0.20, iv30=0.25, hv30=0.30), RULES) == [BUY_CALL, BUY_PUT]
    assert strategies_for(stock(ivr=0.20, iv30=0.25, hv30=0.30), RULES, "buy-put") == [BUY_PUT]
    # Buys no longer need IV30 < HV30 at stock level (checked per contract instead)
    assert strategies_for(stock(ivr=0.20, iv30=0.35, hv30=0.30), RULES) == [BUY_CALL, BUY_PUT]
    # Sells need IV30 > HV30 as well as high rank
    assert strategies_for(stock(ivr=0.80, iv30=0.30, hv30=0.45), RULES) == []
    assert strategies_for(stock(ivr=0.40), RULES) == []  # no edge
    assert strategies_for(stock(ivr=0.55), RULES, "buy") == []


def test_trend_hint():
    assert stock(price=110.0, sma50=100.0).trend == "up +10%"
    assert stock(price=90.0, sma50=100.0).trend == "down -10%"
    assert stock().trend == "-"


def test_seller_picks_expiry_before_earnings():
    exps = ["2026-10-16", "2026-10-30", "2026-11-06", "2026-11-20"]
    s = stock(earnings=date(2026, 11, 3))
    assert candidate_expirations(exps, s, SELL_PUT, RULES, TODAY) == (["2026-10-30"], False)
    # Earnings before every in-window expiry -> latest expiry, flagged.
    s = stock(earnings=date(2026, 10, 20))
    assert candidate_expirations(exps, s, SELL_PUT, RULES, TODAY) == (["2026-10-30", "2026-11-06"], True)
    assert candidate_expirations(["2026-10-16"], s, SELL_PUT, RULES, TODAY) == ([], False)
    assert candidate_expirations(exps, stock(earnings=None), BUY_CALL, RULES, TODAY) == (["2026-11-20"], False)  # 45–90 DTE


def test_ex_div_projection():
    assert next_ex_div(stock(last_ex_div=date(2026, 8, 14)), TODAY) == (date(2026, 11, 13), True)
    assert next_ex_div(stock(last_ex_div=date(2026, 10, 5)), TODAY) == (date(2026, 10, 5), False)
    assert next_ex_div(stock(last_ex_div=date(2015, 4, 30)), TODAY) == (None, False)


# --- contract-level rules ------------------------------------------------------------

def test_best_contract_prefers_liquid_near_target_delta():
    liquid_far = contract(-0.29)
    illiquid_near = contract(-0.22, bid=1.00, ask=1.20)
    c, problems = best_contract([liquid_far, illiquid_near, contract(-0.45)], SELL_PUT, RULES)
    assert c is liquid_far and problems == []


def test_best_contract_picks_liquid_expiry_over_newest_weekly():
    new_weekly = contract(-0.22, oi=0, bid=14.65, ask=17.00)
    older = contract(-0.20, oi=1500, bid=12.40, ask=12.60)
    assert best_contract([new_weekly, older], SELL_PUT, RULES) == (older, [])


def test_spread_rule_uses_dollar_floor_and_percent():
    from optionsfilter.rules import contract_problems

    cheap = contract(-0.20, bid=0.48, ask=0.52)  # $0.04 wide on $0.50 mid: fine under the $0.05 floor
    assert contract_problems(cheap, RULES) == []
    pricey = contract(-0.20, bid=19.70, ask=20.30)  # $0.60 > 3% of $20 = $0.60? equal -> ok
    assert contract_problems(pricey, RULES) == []
    wide = contract(-0.20, bid=19.50, ask=20.50)  # $1.00 > $0.60
    assert contract_problems(wide, RULES) == ["spread $1.00 > $0.60"]


def test_contract_volume_floor():
    from optionsfilter.rules import contract_problems

    c = contract(-0.20)
    c.volume = 3
    assert contract_problems(c, RULES) == ["volume 3 < 10"]


def test_buys_judge_the_contracts_own_iv_against_hv30():
    from optionsfilter.rules import contract_problems

    s = stock(ivr=0.2, iv30=0.25, hv30=0.30)
    cheap_call = contract(0.65, type_="call")
    cheap_call.iv = 0.27
    rich_put = contract(-0.65)
    rich_put.iv = 0.34  # skew: puts richer than the chain average
    assert contract_problems(cheap_call, RULES, BUY_CALL, s) == []
    assert contract_problems(rich_put, RULES, BUY_PUT, s) == ["contract IV 34% ≥ HV30 30%"]
    assert contract_problems(rich_put, RULES, SELL_PUT, s) == []  # sells don't apply it


def test_buys_use_their_own_dte_window_and_delta():
    exps = ["2026-10-30", "2026-11-06", "2026-11-20", "2026-12-18", "2027-01-15"]
    assert candidate_expirations(exps, stock(earnings=None), BUY_CALL, RULES, TODAY)[0] == ["2026-11-20", "2026-12-18"]
    c, problems = best_contract([contract(0.50, type_="call"), contract(0.66, type_="call")], BUY_CALL, RULES)
    assert c.delta == 0.66 and problems == []


def test_best_contract_reports_liquidity_failures():
    c, problems = best_contract([contract(-0.21, oi=50, bid=1.0, ask=1.1)], SELL_PUT, RULES)
    assert c.delta == -0.21
    assert any(p.startswith("OI") for p in problems) and any(p.startswith("spread") for p in problems)
    c, problems = best_contract([contract(-0.05)], SELL_PUT, RULES)
    assert c is None and problems[0].startswith("no contract")


def test_read_only_guard():
    assert is_read_only("get_option_quotes") and is_read_only("preview_scan")
    assert not is_read_only("place_option_order") and not is_read_only("create_scan")


# --- end to end with a fake server -----------------------------------------------------

class FakeRH:
    def __init__(self):
        self.scan_rows = [
            _scan("GOOD", ivr="0.70", earnings="2.0261120e+07"),  # passes
            _scan("EARN", ivr="0.80", earnings="2.0261001e+07"),  # earnings before every expiry
            _scan("MEH", ivr="0.40"),  # no edge
            _scan("CHEAP", ivr="0.10", iv="0.20", hv="0.50"),  # buy candidate; contract IV 40% < HV 50%
        ]

    async def scan(self, filters, columns):
        return self.scan_rows, len(self.scan_rows)

    async def curated_list(self, name):
        return [r["ticker"] for r in self.scan_rows]

    async def option_chains(self, symbol):
        self.chains_requested = getattr(self, "chains_requested", []) + [symbol]
        return [{"id": f"chain-{symbol}", "symbol": symbol,
                 "expiration_dates": ["2026-10-16", "2026-10-30", "2026-11-06", "2026-12-18"]}]

    async def option_instruments(self, chain_id, expiration, option_type):
        sym = chain_id.split("-")[1]
        return [{"id": f"{sym}-{k}-{option_type}-{expiration}", "chain_symbol": sym, "expiration_date": expiration,
                 "strike_price": str(k), "type": option_type} for k in (90, 95, 100, 105, 110)]

    async def option_quotes(self, ids):
        put = {"90": "-0.18", "95": "-0.30", "100": "-0.50", "105": "-0.70", "110": "-0.82"}
        call = {"90": "0.82", "95": "0.70", "100": "0.50", "105": "0.30", "110": "0.18"}
        out = {}
        for i in ids:
            sym, k, typ = i.split("-")[:3]
            d = (put if typ == "put" else call)[k]
            out[i] = {"instrument_id": i, "delta": d, "theta": "-0.04", "implied_volatility": "0.4",
                      "bid_price": "2.00", "ask_price": "2.02", "open_interest": 4000, "volume": 300}
        return out


def _scan(sym, ivr, earnings="", iv="0.40", hv="0.35"):
    return {"ticker": sym, "columns": {"Name": sym, "Last": "100", "IV Rank": ivr, "Implied volatility": iv,
                                       "Historical volatility": hv, "Options volume": "20000",
                                       "Earnings date": earnings, "Ex-dividend date": ""}}


def test_screen_end_to_end():
    rh = FakeRH()
    results, stats = asyncio.run(screen(rh, RULES, today=TODAY))
    assert list(results) == [SELL_PUT, SELL_CALL, BUY_CALL, BUY_PUT]
    sp = {p.stock.symbol: p for p in results[SELL_PUT]}
    sc = {p.stock.symbol: p for p in results[SELL_CALL]}
    bc = {p.stock.symbol: p for p in results[BUY_CALL]}
    bp = {p.stock.symbol: p for p in results[BUY_PUT]}
    assert sp["GOOD"].passed and sp["GOOD"].contract.strike == 90 and sp["GOOD"].contract.type == "put"
    assert sc["GOOD"].passed and sc["GOOD"].contract.strike == 110 and sc["GOOD"].contract.type == "call"
    assert "covered only" in sc["GOOD"].notes
    assert bc["CHEAP"].passed and bc["CHEAP"].contract.delta == 0.70
    assert bp["CHEAP"].passed and bp["CHEAP"].contract.delta == -0.70
    assert bc["CHEAP"].contract.expiration == "2026-12-18"  # 45–90 DTE for buys
    assert not sp["EARN"].passed and "earnings" in sp["EARN"].problems[0]
    assert "MEH" not in sp and "CHEAP" not in sp and "GOOD" not in bc
    assert [p.stock.symbol for p in results[SELL_PUT]][-1] == "EARN"  # passes before near misses
    assert stats["sell put passed"] == 1 and stats["buy put passed"] == 1
    assert sorted(rh.chains_requested) == ["CHEAP", "EARN", "GOOD"]  # one chain lookup per symbol


def test_csv_export(tmp_path):
    import csv as _csv
    from optionsfilter.cli import write_csv
    results, _ = asyncio.run(screen(FakeRH(), RULES, today=TODAY))
    path = tmp_path / "out" / "r.csv"
    write_csv(path, results)
    rows = list(_csv.DictReader(open(path)))
    assert len(rows) == sum(len(v) for v in results.values())
    assert {r["strategy"] for r in rows} == {"SELL PUT", "SELL CALL", "BUY CALL", "BUY PUT"}
    good = next(r for r in rows if r["symbol"] == "GOOD" and r["strategy"] == "SELL PUT")
    assert good["status"] == "PASS" and float(good["iv_rank"]) == 0.7 and float(good["delta"]) == -0.18
    assert next(r for r in rows if r["symbol"] == "EARN")["problems"].startswith("earnings")


class UniverseRH:
    """Scanner fake: 300 liquid names (S0..S299, plus ETFs E0..E9 when unscoped), popular list P0..P4."""

    def __init__(self):
        self.calls = []

    @staticmethod
    def _row(t, vol, price=50):
        return {"ticker": t, "columns": {"Options volume": str(vol), "Last": str(price)}}

    async def curated_list(self, name):
        assert name == "100 most popular"
        return ["P0", "P1", "P2", "P3", "P4", "S0", "E0"]

    async def scan(self, filters, columns):
        self.calls.append(filters)
        sym = next((f for f in filters if f["expression"] == "symbol"), None)
        if sym and sym["predicate"] == "PREDICATE_ANY_OF":  # popular list: no volume floor applied
            assert not any(f["expression"] == "optionsTotalDayVolume" for f in filters)
            rows = [self._row(t, 5 if t.startswith("P") else 1) for t in sym["values"]]
            return rows, len(rows)
        floor = int(next(f for f in filters if f["expression"] == "optionsTotalDayVolume")["values"][0])
        universe = [self._row(f"S{i}", 1_000_000 - i * 1000, price=1 + i) for i in range(300)]
        if sym is None:  # unscoped: ETFs too, and they trade the most
            universe += [self._row(f"E{i}", 5_000_000 - i, price=400) for i in range(10)]
        rows = [r for r in universe if int(r["columns"]["Options volume"]) >= floor]
        price = [f for f in filters if f["expression"] == "tradeAllDay.price"]
        if price:
            lo = float(price[0]["values"][0])
            hi = float(price[1]["values"][0]) if len(price) > 1 else float("inf")
            rows = [r for r in rows if lo <= float(r["columns"]["Last"]) < hi]
        return rows[:200], len(rows)  # server caps responses at 200


def test_universe_is_distinct_union_of_three_lists():
    from optionsfilter.screener import POPULAR, TOP_ALL, TOP_STOCKS, load_universe

    stocks = asyncio.run(load_universe(UniverseRH(), RULES))
    by = {s.symbol: s for s in stocks}
    assert len(by) == len(stocks)  # distinct
    assert all(f"S{i}" in by for i in range(100))  # top 100 stocks
    assert "S100" not in by or POPULAR in by["S100"].sources
    assert all(f"E{i}" in by for i in range(10))  # ETFs from the incl-ETFs list
    assert all(f"P{i}" in by for i in range(5))  # popular kept despite tiny options volume
    assert by["S0"].sources == [TOP_STOCKS, TOP_ALL, POPULAR]
    assert by["E0"].sources == [TOP_ALL, POPULAR]
    assert by["P0"].sources == [POPULAR]
    assert len(stocks) == 100 + 10 + 5  # 90 stocks overlap between the two top-100 lists


def test_capped_scans_are_split_by_price_not_dropped():
    from optionsfilter.screener import scan_bands, _f

    rows = asyncio.run(scan_bands(UniverseRH(), [_f("optionsTotalDayVolume", "GREATER_THAN_OR_EQUAL", [10_000])]))
    assert len(rows) == 310  # 300 stocks + 10 ETFs > 200 cap, all recovered across price bands


def test_symbols_override():
    from optionsfilter.screener import load_universe

    rh = UniverseRH()
    stocks = asyncio.run(load_universe(rh, RULES, ["p0", "S0"]))
    assert {s.symbol for s in stocks} == {"P0", "S0"} and len(rh.calls) == 1


def test_share_class_tickers_use_slash_for_scanner():
    from optionsfilter.screener import _tickers

    assert _tickers(["brk.b", "AAPL"])["values"] == ["BRK/B", "AAPL"]
