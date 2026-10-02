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
    assert strategies_for(stock(ivr=0.20, iv30=0.35, hv30=0.30), RULES) == []  # IV above HV
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
    assert candidate_expirations(exps, stock(earnings=None), BUY_CALL, RULES, TODAY) == (["2026-10-30", "2026-11-06"], False)


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


def test_best_contract_reports_liquidity_failures():
    c, problems = best_contract([contract(-0.21, oi=200, bid=1.0, ask=1.1)], SELL_PUT, RULES)
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
            _scan("CHEAP", ivr="0.10", iv="0.20", hv="0.30"),  # buy candidate
        ]

    async def scan(self, filters, columns):
        return self.scan_rows, len(self.scan_rows)

    async def option_chains(self, symbol):
        self.chains_requested = getattr(self, "chains_requested", []) + [symbol]
        return [{"id": f"chain-{symbol}", "symbol": symbol,
                 "expiration_dates": ["2026-10-16", "2026-10-30", "2026-11-06"]}]

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
    assert bc["CHEAP"].passed and bc["CHEAP"].contract.delta == 0.50
    assert bp["CHEAP"].passed and bp["CHEAP"].contract.delta == -0.50
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


def test_most_traded_takes_top_by_options_volume_and_splits_capped_scans():
    from optionsfilter.screener import load_universe

    class Capped:
        def __init__(self):
            self.calls = []

        async def scan(self, filters, columns):
            self.calls.append(filters)
            price = [f for f in filters if f["expression"] == "tradeAllDay.price"]
            if not price:  # unbanded: always capped; raising the floor leaves too few names
                floor = int(next(f for f in filters if f["expression"] == "optionsTotalDayVolume")["values"][0])
                return [], 300 if floor <= 10_000 else 2
            lo = float(price[0]["values"][0])
            rows = [{"ticker": f"S{lo:g}", "columns": {"Options volume": str(int(lo * 1000 + 1)), "Last": str(lo)}}]
            return rows, 1

    rh = Capped()
    stocks = asyncio.run(load_universe(rh, RULES, ["most-traded"], None, top=3, etfs=False))
    assert [s.symbol for s in stocks] == ["S600", "S250", "S100"]  # top 3 by options volume
    assert len(rh.calls) == 2 + 7  # capped scan, overshooting higher floor, then one per price band
    assert rh.calls[0][0]["values"] == ["Russell3000"]  # stocks only by default


def test_popular_universe_uses_robinhood_list():
    from optionsfilter.screener import load_universe

    class Pop:
        async def curated_list(self, name):
            assert name == "100 most popular"
            return ["AAPL", "SPY"]

        async def scan(self, filters, columns):
            assert filters[0] == {"expression": "symbol", "predicate": "PREDICATE_ANY_OF", "values": ["AAPL", "SPY"]}
            return [], 0

    assert asyncio.run(load_universe(Pop(), RULES, ["popular"], None, top=100, etfs=False)) == []


def test_most_traded_raises_floor_when_that_fits_in_one_response():
    from optionsfilter.screener import load_universe

    class Fits:
        def __init__(self):
            self.calls = 0

        async def scan(self, filters, columns):
            self.calls += 1
            floor = int(next(f for f in filters if f["expression"] == "optionsTotalDayVolume")["values"][0])
            if floor <= 10_000:
                return [], 368
            rows = [{"ticker": f"T{i}", "columns": {"Options volume": str(100_000 - i)}} for i in range(150)]
            return rows, 150

    rh = Fits()
    stocks = asyncio.run(load_universe(rh, RULES, ["most-traded"], None, top=100, etfs=True))
    assert len(stocks) == 100 and stocks[0].symbol == "T0" and rh.calls == 2
