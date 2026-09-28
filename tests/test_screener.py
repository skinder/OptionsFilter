import asyncio
from datetime import date

from optionsfilter.client import is_read_only
from optionsfilter.rules import (
    BUY, SELL_CALL, SELL_PUT, Contract, Rules, Stock, best_contract, candidate_expirations, next_ex_div,
    stock_from_scan, strategy_for, ymd,
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
    assert strategy_for(stock(ivr=0.55), RULES, "all") == SELL_PUT
    assert strategy_for(stock(ivr=0.55), RULES, "sell-call") == SELL_CALL
    assert strategy_for(stock(ivr=0.20, iv30=0.25, hv30=0.30), RULES, "all") == BUY
    assert strategy_for(stock(ivr=0.20, iv30=0.35, hv30=0.30), RULES, "all") is None  # IV above HV
    assert strategy_for(stock(ivr=0.40), RULES, "all") is None  # no edge
    assert strategy_for(stock(ivr=0.55), RULES, "buy") is None


def test_seller_picks_expiry_before_earnings():
    exps = ["2026-10-16", "2026-10-30", "2026-11-06", "2026-11-20"]
    s = stock(earnings=date(2026, 11, 3))
    assert candidate_expirations(exps, s, SELL_PUT, RULES, TODAY) == (["2026-10-30"], False)
    # Earnings before every in-window expiry -> latest expiry, flagged.
    s = stock(earnings=date(2026, 10, 20))
    assert candidate_expirations(exps, s, SELL_PUT, RULES, TODAY) == (["2026-10-30", "2026-11-06"], True)
    assert candidate_expirations(["2026-10-16"], s, SELL_PUT, RULES, TODAY) == ([], False)
    assert candidate_expirations(exps, stock(earnings=None), BUY, RULES, TODAY) == (["2026-10-30", "2026-11-06"], False)


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
        return self.scan_rows

    async def option_chains(self, symbol):
        return [{"id": f"chain-{symbol}", "symbol": symbol,
                 "expiration_dates": ["2026-10-16", "2026-10-30", "2026-11-06"]}]

    async def option_instruments(self, chain_id, expiration, option_type):
        sym = chain_id.split("-")[1]
        return [{"id": f"{sym}-{k}", "chain_symbol": sym, "expiration_date": expiration,
                 "strike_price": str(k), "type": option_type} for k in (90, 95, 100)]

    async def option_quotes(self, ids):
        delta = {"90": "-0.18", "95": "-0.30", "100": "-0.50"}
        out = {}
        for i in ids:
            sym, k = i.split("-")
            d = delta[k] if sym != "CHEAP" else {"90": "0.80", "95": "0.65", "100": "0.50"}[k]
            out[i] = {"instrument_id": i, "delta": d, "theta": "-0.04", "implied_volatility": "0.4",
                      "bid_price": "2.00", "ask_price": "2.02", "open_interest": 4000, "volume": 300}
        return out


def _scan(sym, ivr, earnings="", iv="0.40", hv="0.35"):
    return {"ticker": sym, "columns": {"Name": sym, "Last": "100", "IV Rank": ivr, "Implied volatility": iv,
                                       "Historical volatility": hv, "Options volume": "20000",
                                       "Earnings date": earnings, "Ex-dividend date": ""}}


def test_screen_end_to_end():
    picks, stats = asyncio.run(screen(FakeRH(), RULES, today=TODAY))
    by_symbol = {p.stock.symbol: p for p in picks}
    assert by_symbol["GOOD"].passed and by_symbol["GOOD"].strategy == SELL_PUT
    assert by_symbol["GOOD"].contract.strike == 90 and by_symbol["GOOD"].contract.expiration == "2026-10-30"
    assert by_symbol["CHEAP"].passed and by_symbol["CHEAP"].strategy == BUY
    assert not by_symbol["EARN"].passed and "earnings" in by_symbol["EARN"].problems[0]
    assert "MEH" not in by_symbol
    assert [p.stock.symbol for p in picks][-1] == "EARN"  # passes listed before near misses
    assert stats["passed"] == 2
