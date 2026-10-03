"""The options framework as pure functions — no network, easy to test.

  Sell put    30–45 DTE. IV rank > 50 AND IV30 > HV30, |delta| 15–30, no earnings before expiry.
  Sell call   Same, covered only, flag ex-dividend before expiry (early assignment).
  Buy call    45–90 DTE. IV rank < 30, delta 60–70, and the contract's own IV < HV30
              (IV30 is a chain average that hides skew). Direction = your thesis.
  Buy put     Same as buy call, bearish side.
  Liquidity   contract OI ≥ 100, contract volume ≥ 10/day, spread ≤ max($0.05, 3% of mid),
              underlying total options OI ≥ 10,000.

IV rank and HV30 come from Robinhood's scanner: IV rank = (IV − 52w low) / (52w high − 52w low);
HV30 = annualized close-to-close stdev of daily log returns over one calendar month.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

SELL_PUT, SELL_CALL, BUY_CALL, BUY_PUT = "SELL PUT", "SELL CALL", "BUY CALL", "BUY PUT"
STRATEGIES = [SELL_PUT, SELL_CALL, BUY_CALL, BUY_PUT]
BUYS = {BUY_CALL, BUY_PUT}
MODES = {
    "all": STRATEGIES,
    "sell": [SELL_PUT, SELL_CALL],
    "buy": [BUY_CALL, BUY_PUT],
    "sell-put": [SELL_PUT],
    "sell-call": [SELL_CALL],
    "buy-call": [BUY_CALL],
    "buy-put": [BUY_PUT],
}


@dataclass
class Rules:
    min_dte: int = 30  # sells
    max_dte: int = 45
    buy_min_dte: int = 45  # buys: room for a ~30-day thesis without holding through the steepest decay
    buy_max_dte: int = 90
    sell_min_ivr: float = 0.50
    buy_max_ivr: float = 0.30
    sell_delta: tuple[float, float] = (0.15, 0.30)
    sell_target_delta: float = 0.22
    buy_delta: tuple[float, float] = (0.60, 0.70)  # less extrinsic than ATM, more stock-like
    buy_target_delta: float = 0.65
    min_options_volume: int = 10_000  # starting floor for the top-100 scans (the 100th name trades far more)
    min_open_interest: int = 100  # the chosen contract (stale by a day, hence the volume floor too)
    min_volume: int = 10  # the chosen contract, today
    min_underlying_oi: int = 10_000  # all contracts on the underlying
    max_spread: float = 0.03  # allowed spread = max(spread_floor, max_spread × mid)
    spread_floor: float = 0.05  # dollars

    def dte_window(self, strategy: str) -> tuple[int, int]:
        return (self.buy_min_dte, self.buy_max_dte) if strategy in BUYS else (self.min_dte, self.max_dte)


@dataclass
class Stock:
    symbol: str
    name: str
    price: float
    ivr: float | None  # 0..1
    iv30: float | None
    hv30: float | None
    options_volume: int
    earnings: date | None
    last_ex_div: date | None
    sma50: float | None = None
    options_oi: int | None = None  # total open interest across the underlying's chain
    sources: list[str] = field(default_factory=list)  # which universe lists it came from

    @property
    def iv_hv(self) -> float | None:
        return None if self.iv30 is None or self.hv30 is None else self.iv30 - self.hv30

    @property
    def trend(self) -> str:
        """Price vs its 50-day average — a hint for picking call vs put, not a signal."""
        if not self.sma50 or not self.price:
            return "-"
        return f"{'up' if self.price >= self.sma50 else 'down'} {self.price / self.sma50 - 1:+.0%}"


@dataclass
class Contract:
    expiration: str
    dte: int
    type: str
    strike: float
    delta: float
    theta: float | None
    iv: float | None
    bid: float
    ask: float
    open_interest: int
    volume: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        """Spread as a fraction of mid (for display)."""
        return (self.ask - self.bid) / self.mid if self.mid > 0 else math.inf

    @property
    def width(self) -> float:
        return self.ask - self.bid


@dataclass
class Pick:
    stock: Stock
    strategy: str
    contract: Contract | None = None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.contract is not None and not self.problems


# --- parsing ----------------------------------------------------------------------


def num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def ymd(value) -> date | None:
    """Scanner dates arrive as floats like '2.0261029e+07'."""
    n = num(value)
    if not n:
        return None
    s = str(int(n))
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def stock_from_scan(row: dict) -> Stock:
    c = row["columns"]
    return Stock(
        symbol=row["ticker"],
        name=c.get("Name", ""),
        price=num(c.get("Last")) or 0.0,
        ivr=num(c.get("IV Rank")),
        iv30=num(c.get("Implied volatility")),
        hv30=num(c.get("Historical volatility")),
        options_volume=int(num(c.get("Options volume")) or 0),
        earnings=ymd(c.get("Earnings date")),
        last_ex_div=ymd(c.get("Ex-dividend date")),
        sma50=num(c.get("SMA50")),
        options_oi=None if num(c.get("Options OI")) is None else int(num(c.get("Options OI"))),
    )


def contract_from(instrument: dict, quote: dict, today: date) -> Contract | None:
    delta = num(quote.get("delta"))
    if delta is None:
        return None
    return Contract(
        expiration=instrument["expiration_date"],
        dte=(date.fromisoformat(instrument["expiration_date"]) - today).days,
        type=instrument["type"],
        strike=float(instrument["strike_price"]),
        delta=delta,
        theta=num(quote.get("theta")),
        iv=num(quote.get("implied_volatility")),
        bid=num(quote.get("bid_price")) or 0.0,
        ask=num(quote.get("ask_price")) or 0.0,
        open_interest=int(quote.get("open_interest") or 0),
        volume=int(quote.get("volume") or 0),
    )


# --- stock-level rules --------------------------------------------------------------


def strategies_for(stock: Stock, rules: Rules, mode: str = "all") -> list[str]:
    """Every strategy (within `mode`) this stock's volatility qualifies it for; [] = no edge.

    Sells need rich IV both vs its own history (rank) and vs realized vol (IV30 > HV30) — high
    rank alone can just mean the stock became structurally more volatile. Buys only need low
    rank here; the IV-vs-HV test runs per contract (see contract_problems)."""
    if stock.ivr is None:
        return []
    sell = stock.ivr >= rules.sell_min_ivr and stock.iv_hv is not None and stock.iv_hv > 0
    buy = stock.ivr <= rules.buy_max_ivr
    return [st for st in MODES[mode] if (sell and st not in BUYS) or (buy and st in BUYS)]


def option_type(strategy: str) -> str:
    return "put" if strategy in (SELL_PUT, BUY_PUT) else "call"


def score(pick: Pick) -> float:
    """Sellers: higher IV rank is better. Buyers: IV further below HV is better."""
    s = pick.stock
    return -(s.iv_hv or 0) if pick.strategy in BUYS else s.ivr


def earnings_in_window(stock: Stock, expiration: date, today: date) -> bool:
    return stock.earnings is not None and today <= stock.earnings <= expiration


def next_ex_div(stock: Stock, today: date) -> tuple[date | None, bool]:
    """(date, estimated?) — projects the last ex-date forward quarterly when it's in the past."""
    ex = stock.last_ex_div
    if ex is None or ex < today - timedelta(days=400):
        return None, False  # no recent dividend
    if ex >= today:
        return ex, False
    while ex < today:
        ex += timedelta(days=91)
    return ex, True


def candidate_expirations(expirations: list[str], stock: Stock, strategy: str, rules: Rules, today: date) -> tuple[list[str], bool]:
    """Every expiry in the DTE window (the best contract is picked across all of them — the
    newest weekly often has zero open interest). Sellers keep only expiries before earnings
    when any exist.

    Returns (expirations, earnings_in_window)."""
    lo, hi = rules.dte_window(strategy)
    window = sorted(d for d in expirations if lo <= (date.fromisoformat(d) - today).days <= hi)
    if not window:
        return [], False
    if strategy not in BUYS:
        clean = [d for d in window if not earnings_in_window(stock, date.fromisoformat(d), today)]
        if clean:
            return clean, False
    return window, earnings_in_window(stock, date.fromisoformat(window[-1]), today)


def strike_band(stock: Stock, strategy: str, dte: int) -> tuple[float, float]:
    """Strikes worth quoting, in price terms, from the 1-sigma expected move."""
    sigma = (stock.iv30 or 0.5) * math.sqrt(max(dte, 1) / 365) * stock.price
    p = stock.price
    if strategy == SELL_PUT:
        return p - 1.8 * sigma, p - 0.2 * sigma
    if strategy == SELL_CALL:
        return p + 0.2 * sigma, p + 1.6 * sigma
    if strategy == BUY_CALL:  # delta 0.60–0.70 is slightly in the money
        return p - 1.0 * sigma, p + 0.1 * sigma
    return p - 0.1 * sigma, p + 1.0 * sigma


# --- contract-level rules -----------------------------------------------------------


def delta_range(strategy: str, rules: Rules) -> tuple[tuple[float, float], float]:
    if strategy in BUYS:
        return rules.buy_delta, rules.buy_target_delta
    return rules.sell_delta, rules.sell_target_delta


def max_width(c: Contract, rules: Rules) -> float:
    """Allowed bid/ask width: a dollar floor for cheap contracts, a percentage for expensive ones."""
    return max(rules.spread_floor, rules.max_spread * c.mid)


def contract_problems(c: Contract, rules: Rules, strategy: str = SELL_PUT, stock: Stock | None = None) -> list[str]:
    problems = []
    if c.open_interest < rules.min_open_interest:
        problems.append(f"OI {c.open_interest:,} < {rules.min_open_interest:,}")
    if c.volume < rules.min_volume:
        problems.append(f"volume {c.volume:,} < {rules.min_volume:,}")
    if c.width > max_width(c, rules) + 1e-9:
        problems.append(f"spread ${c.width:.2f} > ${max_width(c, rules):.2f}")
    if strategy in BUYS and stock is not None and stock.hv30 is not None and (c.iv is None or c.iv >= stock.hv30):
        problems.append(f"contract IV {pct_or_dash(c.iv)} ≥ HV30 {stock.hv30:.0%}")
    return problems


def pct_or_dash(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def best_contract(
    contracts: list[Contract], strategy: str, rules: Rules, stock: Stock | None = None
) -> tuple[Contract | None, list[str]]:
    """Closest to target delta among contracts that pass; otherwise the least-bad one."""
    (lo, hi), target = delta_range(strategy, rules)
    in_range = [c for c in contracts if lo <= abs(c.delta) <= hi]
    if not in_range:
        return None, [f"no contract with |delta| {lo:.2f}–{hi:.2f}"]
    problems = {id(c): contract_problems(c, rules, strategy, stock) for c in in_range}
    ok = [c for c in in_range if not problems[id(c)]]
    if ok:
        return min(ok, key=lambda c: abs(abs(c.delta) - target)), []
    closest = min(in_range, key=lambda c: (len(problems[id(c)]), c.spread))
    return closest, problems[id(closest)]
