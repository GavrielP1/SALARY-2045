"""
data_fetcher.py — Salary 2045 Portfolio Data Fetcher
Fetches live data via yfinance and saves portfolio.json in the same folder.
Holdings are read from holdings.json (copy holdings.example.json to get started).
"""

import json
import os
from datetime import datetime, timezone

import yfinance as yf

# ── Portfolio holdings (loaded from holdings.json) ───────────────────────────
_HOLDINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holdings.json")
if not os.path.exists(_HOLDINGS_FILE):
    print(f"ERROR: holdings.json not found at {_HOLDINGS_FILE}")
    print("Copy holdings.example.json to holdings.json and fill in your positions.")
    raise SystemExit(1)
with open(_HOLDINGS_FILE, "r", encoding="utf-8") as _f:
    HOLDINGS = json.load(_f)

GOAL_ANNUAL_INCOME = 24_000  # $2,000/month — Salary 2045 target

OUTPUT_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "portfolio.json")


def safe_get(info: dict, key: str):
    """Return info[key] if present and non-None, else None."""
    val = info.get(key)
    return val if val is not None else None


def get_current_price(info: dict) -> float | None:
    """
    yfinance uses different price keys for stocks vs ETFs.
    Try in priority order until we find a non-zero value.
    """
    for key in ("currentPrice", "regularMarketPrice", "navPrice",
                "previousClose", "open"):
        val = info.get(key)
        if val is not None and val != 0:
            return float(val)
    return None


def fetch_dividend_history(ticker_obj) -> list:
    """Return last 8 quarters of dividends as [{date, amount}, ...]."""
    try:
        divs = ticker_obj.dividends
        if divs is None or divs.empty:
            return []
        # Resample to quarterly sums, keep last 8 quarters
        quarterly = divs.resample("QE").sum().tail(8)
        result = []
        for ts, amount in quarterly.items():
            result.append({
                "date":   ts.strftime("%Y-%m-%d"),
                "amount": round(float(amount), 6),
            })
        return result
    except Exception:
        return []


def fetch_holding(holding: dict) -> dict:
    ticker_sym = holding["ticker"]
    shares     = holding["shares"]
    avg_cost   = holding["avgCost"]

    print(f"Fetching {ticker_sym}...", end=" ", flush=True)

    t = yf.Ticker(ticker_sym)

    try:
        info = t.info or {}
    except Exception:
        info = {}

    current_price              = get_current_price(info)
    name                       = safe_get(info, "shortName")
    sector                     = safe_get(info, "sector")
    industry                   = safe_get(info, "industry")
    dividend_yield             = safe_get(info, "dividendYield")
    trailing_annual_div_rate   = safe_get(info, "trailingAnnualDividendRate")
    payout_ratio               = safe_get(info, "payoutRatio")
    trailing_eps               = safe_get(info, "trailingEps")
    five_year_avg_div_yield    = safe_get(info, "fiveYearAvgDividendYield")
    dividend_rate              = safe_get(info, "dividendRate")

    # Calculated fields
    market_value  = round(shares * current_price, 4)  if current_price           is not None else None
    cost_basis    = round(shares * avg_cost,       4)
    gain_loss     = round(market_value - cost_basis,  4) if market_value          is not None else None
    gain_loss_pct = round(gain_loss / cost_basis,     6) if gain_loss             is not None else None
    annual_income = round(shares * trailing_annual_div_rate, 4) if trailing_annual_div_rate is not None else None
    yield_on_cost = round(trailing_annual_div_rate / avg_cost, 6) if trailing_annual_div_rate is not None else None

    div_history = fetch_dividend_history(t)

    print("done")

    return {
        "ticker":               ticker_sym,
        "name":                 name,
        "sector":               sector,
        "industry":             industry,
        "shares":               shares,
        "avgCost":              avg_cost,
        "currentPrice":         current_price,
        "marketValue":          market_value,
        "costBasis":            cost_basis,
        "gainLoss":             gain_loss,
        "gainLossPct":          gain_loss_pct,
        "dividendYield":        dividend_yield,
        "annualDividendPerShare": trailing_annual_div_rate,
        "annualIncome":         annual_income,
        "yieldOnCost":          yield_on_cost,
        "payoutRatio":          payout_ratio,
        "trailingEps":          trailing_eps,
        "fiveYearAvgDividendYield": five_year_avg_div_yield,
        "dividendRate":         dividend_rate,
        "dividendHistory":      div_history,
    }


def build_portfolio() -> dict:
    holdings_data = [fetch_holding(h) for h in HOLDINGS]

    # Portfolio-level totals (skip None values in sums)
    total_market_value  = sum(h["marketValue"]  for h in holdings_data if h["marketValue"]  is not None)
    total_cost_basis    = sum(h["costBasis"]    for h in holdings_data if h["costBasis"]    is not None)
    total_gain_loss     = sum(h["gainLoss"]     for h in holdings_data if h["gainLoss"]     is not None)
    total_annual_income = sum(h["annualIncome"] for h in holdings_data if h["annualIncome"] is not None)

    total_gain_loss_pct = round(total_gain_loss / total_cost_basis, 6) if total_cost_basis else None
    portfolio_yield     = round(total_annual_income / total_market_value, 6) if total_market_value else None
    portfolio_yoc       = round(total_annual_income / total_cost_basis,   6) if total_cost_basis   else None

    metadata = {
        "lastUpdated":       datetime.now(timezone.utc).isoformat(),
        "baseCurrency":      "USD",
        "goalAnnualIncome":  GOAL_ANNUAL_INCOME,
        "totalMarketValue":  round(total_market_value,  2),
        "totalCostBasis":    round(total_cost_basis,    2),
        "totalGainLoss":     round(total_gain_loss,     2),
        "totalGainLossPct":  total_gain_loss_pct,
        "totalAnnualIncome": round(total_annual_income, 2),
        "portfolioYield":    portfolio_yield,
        "portfolioYoC":      portfolio_yoc,
    }

    return {"metadata": metadata, "holdings": holdings_data}


def print_summary(portfolio: dict) -> None:
    m = portfolio["metadata"]
    goal_pct = (m["totalAnnualIncome"] / m["goalAnnualIncome"] * 100) if m["goalAnnualIncome"] else 0
    print()
    print("=" * 52)
    print("  SALARY 2045 — PORTFOLIO SUMMARY")
    print("=" * 52)
    print(f"  Total Market Value : ${m['totalMarketValue']:>10,.2f}")
    print(f"  Total Cost Basis   : ${m['totalCostBasis']:>10,.2f}")
    print(f"  Total Gain / Loss  : ${m['totalGainLoss']:>+10,.2f}  ({(m['totalGainLossPct'] or 0)*100:+.2f}%)")
    print(f"  Annual Income      : ${m['totalAnnualIncome']:>10,.2f}")
    print(f"  Portfolio Yield    :  {(m['portfolioYield'] or 0)*100:>9.2f}%")
    print(f"  Yield on Cost      :  {(m['portfolioYoC'] or 0)*100:>9.2f}%")
    print(f"  Goal Progress      :  {goal_pct:>9.1f}%  (${m['totalAnnualIncome']:,.0f} / ${m['goalAnnualIncome']:,})")
    print("=" * 52)
    print(f"  Saved to: {OUTPUT_FILE}")
    print()


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print()
    print("Salary 2045 — fetching live portfolio data...")
    print()

    portfolio = build_portfolio()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(portfolio, f, indent=2, ensure_ascii=False)

    print_summary(portfolio)


if __name__ == "__main__":
    main()
