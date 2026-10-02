"""The 1-month (30–45 DTE) framework as pure functions — no network, easy to test.

  Sell put    IV rank > 50, |delta| 15–30, no earnings before expiry.
  Sell call   Same, covered only, flag ex-dividend before expiry (early assignment).
  Buy call    IV rank < 30 and IV30 < HV30; direction = your thesis (trend vs 50-day SMA shown as a hint).
  Buy put     Same as buy call, bearish side.
  Liquidity   underlying options volume > 10k/day, contract OI ≥ 100, bid/ask spread < 2% of mid.
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
    min_dte: int = 30
    max_dte: int = 45
    sell_min_ivr: float = 0.50
    buy_max_ivr: float = 0.30
    sell_delta: tuple[float, float] = (0.15, 0.30)
    sell_target_delta: float = 0.22
    buy_delta: tuple[float, float] = (0.40, 0.60)
    buy_target_delta: float = 0.50
    min_options_volume: int = 10_000  # starting floor for the top-100 scans (the 100th name trades far more)
    min_open_interest: int = 100  # the chosen contract
    max_spread: float = 0.02  # (ask - bid) / mid


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
        return (self.ask - self.bid) / self.mid if self.mid > 0 else math.inf


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
    """Every strategy (within `mode`) this stock's volatility qualifies it for; [] = no edge."""
    if stock.ivr is None:
        return []
    sell = stock.ivr >= rules.sell_min_ivr
    buy = stock.ivr <= rules.buy_max_ivr and stock.iv_hv is not None and stock.iv_hv < 0
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
    window = sorted(
        d for d in expirations if rules.min_dte <= (date.fromisoformat(d) - today).days <= rules.max_dte
    )
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
    return p - 0.4 * sigma, p + 0.4 * sigma


# --- contract-level rules -----------------------------------------------------------


def delta_range(strategy: str, rules: Rules) -> tuple[tuple[float, float], float]:
    if strategy in BUYS:
        return rules.buy_delta, rules.buy_target_delta
    return rules.sell_delta, rules.sell_target_delta


def liquidity_problems(c: Contract, rules: Rules) -> list[str]:
    problems = []
    if c.open_interest < rules.min_open_interest:
        problems.append(f"OI {c.open_interest:,} < {rules.min_open_interest:,}")
    if c.spread > rules.max_spread:
        problems.append(f"spread {c.spread:.1%} > {rules.max_spread:.0%}")
    return problems


def best_contract(contracts: list[Contract], strategy: str, rules: Rules) -> tuple[Contract | None, list[str]]:
    """Closest to target delta among liquid contracts; otherwise the least-bad illiquid one."""
    (lo, hi), target = delta_range(strategy, rules)
    in_range = [c for c in contracts if lo <= abs(c.delta) <= hi]
    if not in_range:
        return None, [f"no contract with |delta| {lo:.2f}–{hi:.2f}"]
    liquid = [c for c in in_range if not liquidity_problems(c, rules)]
    if liquid:
        return min(liquid, key=lambda c: abs(abs(c.delta) - target)), []
    closest = min(in_range, key=lambda c: (len(liquidity_problems(c, rules)), c.spread))
    return closest, liquidity_problems(closest, rules)
