"""
app.py — Salary 2045 Portfolio Tracker (Flask Backend)
Run:  python app.py
Open: http://localhost:5000
"""

import os
import json
import time
import calendar
import tempfile
import requests
from datetime import datetime, timezone, date, timedelta

from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import yfinance as yf

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
HOLDINGS_FILE = os.path.join(BASE_DIR, "holdings.json")

AV_API_KEY = "OAGLVSDNFAM4B54H"
AV_BASE    = "https://www.alphavantage.co/query"
CACHE_DIR   = os.path.join(BASE_DIR, "financials_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

app = Flask(__name__)
CORS(app)

# ── Hardcoded fallback (used only if holdings.json is completely missing) ────────
# Keep generic — must never contain real personal holdings.
_FALLBACK_HOLDINGS = [
    {"ticker": "SCHD", "shares": 10, "avgCost": 26.00},
    {"ticker": "JNJ",  "shares":  5, "avgCost": 150.00},
    {"ticker": "PG",   "shares":  3, "avgCost": 140.00},
]

TAX_RATE  = 0.25     # Israeli capital gains tax
CACHE_TTL = 900      # 15 minutes

GOAL_FILE      = os.path.join(BASE_DIR, "goal.json")
_GOAL_DEFAULTS = {"goal": 24_000, "milestones": [6000, 12000, 18000, 24000]}


def load_goal() -> dict:
    """Read goal settings from goal.json; return defaults if missing/corrupt."""
    if not os.path.exists(GOAL_FILE):
        return dict(_GOAL_DEFAULTS)
    try:
        with open(GOAL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        g  = float(data.get("goal", 24_000))
        ms = [float(x) for x in data.get("milestones", _GOAL_DEFAULTS["milestones"])]
        return {"goal": g, "milestones": ms}
    except Exception:
        return dict(_GOAL_DEFAULTS)


def save_goal(data: dict) -> None:
    """Atomically write goal settings to goal.json."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=BASE_DIR, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, GOAL_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

_cache: dict = {}

# ── Holdings persistence ───────────────────────────────────────────────────────

def load_holdings() -> list:
    """Read holdings from JSON file; seed from fallback if file is absent."""
    if not os.path.exists(HOLDINGS_FILE):
        save_holdings(_FALLBACK_HOLDINGS)
        return list(_FALLBACK_HOLDINGS)
    try:
        with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return list(_FALLBACK_HOLDINGS)


def save_holdings(holdings: list) -> None:
    """Atomically write holdings to disk (temp file + os.replace)."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=BASE_DIR, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(holdings, f, indent=2)
        os.replace(tmp_path, HOLDINGS_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _clear_portfolio_cache():
    """Invalidate all portfolio-derived cache entries."""
    for key in ("portfolio", "income", "perf"):
        _cache.pop(key, None)


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _cget(key):
    e = _cache.get(key)
    return e["d"] if (e and time.monotonic() - e["t"] < CACHE_TTL) else None

def _cset(key, data):
    _cache[key] = {"t": time.monotonic(), "d": data}

# ── yfinance helpers ───────────────────────────────────────────────────────────

def _price(info: dict):
    """ETF-safe current price: tries multiple yfinance keys."""
    for k in ("currentPrice", "regularMarketPrice", "navPrice", "previousClose"):
        v = info.get(k)
        if v is not None and v != 0:
            return float(v)
    return None

def _s(info: dict, key: str):
    v = info.get(key)
    return v if v is not None else None

def _ann_div_last4q(divs) -> float:
    """Sum of the last 4 raw dividend payments (handles ETFs better than trailingAnnualDividendRate)."""
    if divs is None or divs.empty:
        return 0.0
    return float(divs.tail(4).sum())

def _quarterly_history(divs, n: int = 12) -> list:
    """Last n quarter-resampled dividend sums as [{date, amount}]."""
    if divs is None or divs.empty:
        return []
    try:
        q = divs.resample("QE").sum().tail(n)
        return [
            {"date": ts.strftime("%Y-%m-%d"), "amount": round(float(v), 6)}
            for ts, v in q.items() if v > 0
        ]
    except Exception:
        return []

def _streak(divs) -> int:
    """Consecutive full calendar years of rising annual dividends."""
    if divs is None or divs.empty:
        return 0
    try:
        annual = divs.resample("YE").sum()
        annual = annual[annual.index.year < datetime.now().year]
        vals = list(annual.values)
        if len(vals) < 2:
            return 0
        count = 0
        for i in range(len(vals) - 1, 0, -1):
            if vals[i] > vals[i - 1] * 1.001:
                count += 1
            else:
                break
        return count
    except Exception:
        return 0

def _add_months(d: date, n: int) -> date:
    """Add n months to date d, clamping to valid days."""
    total = d.month - 1 + n
    yr = d.year + total // 12
    mo = total % 12 + 1
    max_day = calendar.monthrange(yr, mo)[1]
    return date(yr, mo, min(d.day, max_day))

def _detect_div_frequency(divs) -> tuple:
    """Detect actual dividend payment frequency from history.
    Returns (frequency: int, is_estimated: bool).
    frequency is one of 1 (annual), 2 (semi-annual), 4 (quarterly), 12 (monthly).

    Strategy:
    1. Try a 2-year window first. If it has >= 8 payments, use it.
    2. Otherwise fall back to ALL available history (handles new tickers like QQQI).
    3. Compute avg_per_year from total payments / months spanned, then snap to standard freq.
    4. is_estimated = True if total_payments < 4.
    """
    if divs is None or divs.empty:
        return 4, True
    try:
        import pandas as pd

        # ── Try 2-year window first ────────────────────────────────────────────
        cutoff = pd.Timestamp.now(tz="UTC") - pd.DateOffset(years=2)
        idx = divs.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        recent = divs[idx >= cutoff]

        if len(recent) >= 8:
            working = recent
        else:
            # Fall back to full history for new/short-history tickers
            working = divs

        if len(working) < 2:
            return 4, True

        # ── Compute avg payments per year from span ────────────────────────────
        first_date = working.index[0]
        last_date  = working.index[-1]
        months_spanned = (last_date - first_date).days / 30.44

        if months_spanned < 1:
            return 4, True

        total_payments = len(working)
        avg_per_year   = (total_payments / months_spanned) * 12

        # ── Snap to nearest standard frequency ────────────────────────────────
        if avg_per_year >= 10:
            freq = 12
        elif avg_per_year >= 3:
            freq = 4
        elif avg_per_year >= 1.5:
            freq = 2
        else:
            freq = 1

        is_estimated = total_payments < 4
        return freq, is_estimated

    except Exception:
        return 4, True


def _div_cagr(divs, n: int):
    """n-year dividend CAGR from annual dividend sums. Returns None if history is insufficient."""
    if divs is None or divs.empty:
        return None
    try:
        ann_q    = divs.resample("YE").sum()
        now_yr   = datetime.now().year
        ann_full = ann_q[ann_q.index.year < now_yr]
        ann_dict = {int(ts.year): float(v) for ts, v in ann_full.items() if v > 0}
        yrs      = sorted(ann_dict)
        if len(yrs) < n + 1:
            return None
        start = ann_dict[yrs[-(n + 1)]]
        end   = ann_dict[yrs[-1]]
        return round((end / start) ** (1 / n) - 1, 6) if start > 0 else None
    except Exception:
        return None

def _safety_score(info: dict, ann_div: float, streak: int) -> dict:
    """
    Heuristic dividend safety score 0-100 from up to 5 factors.
    Weights (missing factors get weight redistributed proportionally):
      payoutRatio:      30  — earnings payout ratio (lower = safer)
      fcfPayout:        25  — total dividends / free cash flow (lower = safer)
      debtEquity:       20  — debt/equity ratio (lower = safer)
      earningsCoverage: 15  — EPS / ann_div coverage (higher = safer)
      streak:           10  — consecutive years of dividend increases
    Grade: 90+ A, 80-89 B, 65-79 C, 50-64 D, <50 F
    NOTE: Heuristic estimate from payout/FCF/debt/coverage/streak.
          NOT Seeking Alpha sector-relative grade — use for relative
          comparison within your portfolio only.
    """
    ALL_W = {"payoutRatio": 30, "fcfPayout": 25, "debtEquity": 20,
             "earningsCoverage": 15, "streak": 10}
    factors = {k: None for k in ALL_W}

    # Factor 1 — Payout ratio (weight 30)
    pr = info.get("payoutRatio")
    if pr is not None and isinstance(pr, (int, float)):
        if pr < 0.5:
            factors["payoutRatio"] = 100.0
        elif pr < 0.75:
            factors["payoutRatio"] = round(100.0 * (0.75 - pr) / 0.25, 1)
        else:
            factors["payoutRatio"] = max(0.0, round(100.0 * max(0.0, 1.0 - pr) / 0.25, 1))

    # Factor 2 — FCF payout (weight 25): (ann_div * shares_out) / freeCashflow
    fcf       = info.get("freeCashflow")
    shares_out = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
    if (fcf and shares_out and ann_div and ann_div > 0
            and isinstance(fcf, (int, float)) and isinstance(shares_out, (int, float))
            and fcf > 0):
        try:
            ratio = (ann_div * float(shares_out)) / float(fcf)
            if ratio < 0.4:
                factors["fcfPayout"] = 100.0
            elif ratio < 0.8:
                factors["fcfPayout"] = round(100.0 * (0.8 - ratio) / 0.4, 1)
            else:
                factors["fcfPayout"] = max(0.0, round(100.0 * (1.2 - ratio) / 0.4, 1))
        except Exception:
            pass

    # Factor 3 — Debt/Equity (weight 20)
    # yfinance returns D/E as a ratio; some tickers return it ×100 (e.g. 150 = 1.50x)
    de = info.get("debtToEquity")
    if de is not None and isinstance(de, (int, float)):
        de_norm = de / 100.0 if de > 20 else de
        if de_norm < 0.5:
            factors["debtEquity"] = 100.0
        elif de_norm < 1.5:
            factors["debtEquity"] = round(100.0 - 50.0 * (de_norm - 0.5), 1)
        elif de_norm < 3.0:
            factors["debtEquity"] = round(50.0 * (3.0 - de_norm) / 1.5, 1)
        else:
            factors["debtEquity"] = 0.0

    # Factor 4 — Earnings coverage: EPS / ann_div (weight 15)
    eps = info.get("trailingEps")
    if eps is not None and ann_div and ann_div > 0 and isinstance(eps, (int, float)) and eps > 0:
        cov = eps / ann_div
        if cov >= 2.5:
            factors["earningsCoverage"] = 100.0
        elif cov >= 1.0:
            factors["earningsCoverage"] = round(100.0 * (cov - 1.0) / 1.5, 1)
        else:
            factors["earningsCoverage"] = 0.0

    # Factor 5 — Dividend streak (weight 10)
    if streak is not None and isinstance(streak, int):
        if streak >= 25:
            factors["streak"] = 100.0
        elif streak >= 10:
            factors["streak"] = round(50.0 + 50.0 * (streak - 10) / 15.0, 1)
        elif streak >= 5:
            factors["streak"] = round(25.0 + 25.0 * (streak - 5) / 5.0, 1)
        elif streak > 0:
            factors["streak"] = round(25.0 * streak / 5.0, 1)
        else:
            factors["streak"] = 0.0

    available  = {k: v for k, v in factors.items() if v is not None}
    factor_cnt = len(available)
    if not available:
        return {"score": None, "grade": "N/A", "factors": factors, "factorCount": 0}

    total_w = sum(ALL_W[k] for k in available)
    if total_w == 0:
        return {"score": None, "grade": "N/A", "factors": factors, "factorCount": 0}

    score = sum(available[k] * ALL_W[k] for k in available) / total_w
    grade = "A" if score >= 90 else ("B" if score >= 80 else ("C" if score >= 65 else ("D" if score >= 50 else "F")))
    return {"score": round(score, 1), "grade": grade, "factors": factors, "factorCount": factor_cnt}

# ── Financials file-cache helpers ─────────────────────────────────────────────

def _merge_periods(old_list, new_list):
    """Merge two [{period, value}] lists; new_list wins on conflict. Returns sorted asc."""
    combined = {item["period"]: item for item in (old_list or [])}
    for item in (new_list or []):
        combined[item["period"]] = item
    return sorted(combined.values(), key=lambda x: x["period"])


def _cache_path(ticker):
    return os.path.join(CACHE_DIR, f"{ticker}.json")


def _load_cache(ticker):
    path = _cache_path(ticker)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(ticker, data):
    path    = _cache_path(ticker)
    payload = dict(data)

    # Merge time-series lists with any existing cache so history accumulates
    existing = _load_cache(ticker)
    if existing:
        for period_key in ("annual", "quarterly"):
            src  = existing.get(period_key) or {}
            dest = payload.get(period_key)  or {}
            if not isinstance(src, dict) or not isinstance(dest, dict):
                continue
            for metric in ("revenue", "netIncome", "eps", "expenses", "margin", "growth",
                           "divGrowth", "divPerShare", "payoutHistory", "fcf"):
                old_v = src.get(metric)
                new_v = dest.get(metric)
                if old_v and new_v:
                    dest[metric] = _merge_periods(old_v, new_v)
                elif old_v and not new_v:
                    dest[metric] = old_v
            payload[period_key] = dest

    payload["last_updated"] = date.today().strftime("%Y-%m-%d")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        pass


def _fetch_av_financials(ticker):
    """Fetch income statements from Alpha Vantage. Returns None on any failure."""

    url = (f"{AV_BASE}?function=INCOME_STATEMENT"
           f"&symbol={ticker}&apikey={AV_API_KEY}")
    print(f"[AlphaVantage] GET {url.replace(AV_API_KEY, '****')}")

    try:
        r = requests.get(url, timeout=20)
    except Exception as e:
        print(f"[AlphaVantage] Request exception: {e}")
        return None

    print(f"[AlphaVantage] Status: {r.status_code}")
    if r.status_code != 200:
        print(f"[AlphaVantage] Error body: {r.text[:300]}")
        return None

    try:
        data = r.json()
    except Exception:
        print(f"[AlphaVantage] JSON decode error")
        return None

    if "Information" in data:
        print(f"[AlphaVantage] Rate limit / info message: {data['Information'][:200]}")
        return None
    if "Error Message" in data:
        print(f"[AlphaVantage] Error: {data['Error Message'][:200]}")
        return None

    annual_reports  = data.get("annualReports")  or []
    qtr_reports     = data.get("quarterlyReports") or []

    if not annual_reports and not qtr_reports:
        print(f"[AlphaVantage] No reports found for {ticker}")
        return None

    def _safe_float(v):
        """Convert AV string value to float; return None for 'None' or non-numeric."""
        if v is None or str(v).strip().lower() == "none":
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    def _parse_reports(reports):
        revenue, net_income, eps_list, expenses, dividends_paid = [], [], [], [], []
        for rec in reports:
            period = str(rec.get("fiscalDateEnding", ""))[:10]
            if not period:
                continue

            rev = _safe_float(rec.get("totalRevenue"))
            ni  = _safe_float(rec.get("netIncome"))

            # EPS: prefer basicEPS, fall back to dilutedEPS
            ep = _safe_float(rec.get("basicEPS"))
            if ep is None:
                ep = _safe_float(rec.get("dilutedEPS"))
            # Calculated fallback: netIncome / commonStockSharesOutstanding
            if ep is None and ni is not None:
                shs = _safe_float(rec.get("commonStockSharesOutstanding"))
                if shs is not None and shs > 0:
                    ep = round(ni / shs, 4)

            # Expenses: first non-None of operatingExpenses, totalOperatingExpenses, costOfRevenue
            exp = None
            for k in ("operatingExpenses", "totalOperatingExpenses", "costOfRevenue"):
                exp = _safe_float(rec.get(k))
                if exp is not None:
                    break

            dp = _safe_float(rec.get("dividendsPaid"))

            if rev is not None:
                revenue.append({"period": period, "value": round(rev, 2)})
            if ni is not None:
                net_income.append({"period": period, "value": round(ni, 2)})
            if ep is not None:
                eps_list.append({"period": period, "value": round(ep, 4)})
            if exp is not None:
                expenses.append({"period": period, "value": round(exp, 2)})
            if dp is not None:
                dividends_paid.append({"period": period, "value": round(abs(dp), 2)})

        return {
            "revenue":       revenue        or None,
            "netIncome":     net_income     or None,
            "eps":           eps_list       or None,
            "expenses":      expenses       or None,
            "dividendsPaid": dividends_paid or None,
        }

    print(f"[AlphaVantage] Success for {ticker}: "
          f"{len(annual_reports)} annual, {len(qtr_reports)} quarterly reports")

    annual_parsed    = _parse_reports(annual_reports)
    quarterly_parsed = _parse_reports(qtr_reports)

    # ── Second EPS fallback: AV EARNINGS endpoint ──────────────────────────────
    # Fills years where INCOME_STATEMENT returns null basicEPS/dilutedEPS/shares.
    # Only called when annual EPS is still sparse after the first parse.
    ann_eps_have = len(annual_parsed.get("eps") or [])
    if ann_eps_have < len(annual_reports) // 2:
        try:
            time.sleep(13)  # stay within 5 req/min free tier limit
            earn_url = (f"{AV_BASE}?function=EARNINGS"
                        f"&symbol={ticker}&apikey={AV_API_KEY}")
            print(f"[AlphaVantage] GET {earn_url.replace(AV_API_KEY, '****')} (earnings EPS fallback)")
            re = requests.get(earn_url, timeout=20)
            print(f"[AlphaVantage] EARNINGS Status: {re.status_code}")
            if re.status_code == 200:
                earn_data = re.json()
                if "Information" in earn_data:
                    print(f"[AlphaVantage] EARNINGS rate limit: {earn_data['Information'][:120]}")
                elif "Error Message" in earn_data:
                    print(f"[AlphaVantage] EARNINGS error: {earn_data['Error Message'][:120]}")
                if "Information" not in earn_data and "Error Message" not in earn_data:
                    # Annual EPS fill
                    ann_eps_map = {}
                    for rec in (earn_data.get("annualEarnings") or []):
                        p  = str(rec.get("fiscalDateEnding", ""))[:10]
                        ep = _safe_float(rec.get("reportedEPS"))
                        if p and ep is not None:
                            ann_eps_map[p] = ep

                    if ann_eps_map:
                        have     = {e["period"] for e in (annual_parsed.get("eps") or [])}
                        ni_have  = {e["period"] for e in (annual_parsed.get("netIncome") or [])}
                        fill = [{"period": p, "value": round(v, 4)}
                                for p, v in ann_eps_map.items()
                                if p not in have and p in ni_have]
                        if fill:
                            annual_parsed["eps"] = _merge_periods(
                                annual_parsed.get("eps") or [], fill)
                            print(f"[AlphaVantage] EPS annual fill: +{len(fill)} periods from EARNINGS")

                    # Quarterly EPS fill
                    qtr_eps_map = {}
                    for rec in (earn_data.get("quarterlyEarnings") or []):
                        p  = str(rec.get("fiscalDateEnding", ""))[:10]
                        ep = _safe_float(rec.get("reportedEPS"))
                        if p and ep is not None:
                            qtr_eps_map[p] = ep

                    if qtr_eps_map:
                        have    = {e["period"] for e in (quarterly_parsed.get("eps") or [])}
                        ni_have = {e["period"] for e in (quarterly_parsed.get("netIncome") or [])}
                        fill = [{"period": p, "value": round(v, 4)}
                                for p, v in qtr_eps_map.items()
                                if p not in have and p in ni_have]
                        if fill:
                            quarterly_parsed["eps"] = _merge_periods(
                                quarterly_parsed.get("eps") or [], fill)
                            print(f"[AlphaVantage] EPS quarterly fill: +{len(fill)} periods from EARNINGS")
        except Exception as e:
            print(f"[AlphaVantage] EARNINGS fetch failed: {e}")

    return {
        "annual":    annual_parsed,
        "quarterly": quarterly_parsed,
    }


def _fetch_av_cashflow(ticker):
    """Fetch cash flow statements from Alpha Vantage. Returns None on any failure."""

    url = (f"{AV_BASE}?function=CASH_FLOW"
           f"&symbol={ticker}&apikey={AV_API_KEY}")
    print(f"[AlphaVantage] GET {url.replace(AV_API_KEY, '****')}")

    try:
        r = requests.get(url, timeout=20)
    except Exception as e:
        print(f"[AlphaVantage] Request exception: {e}")
        return None

    print(f"[AlphaVantage] Status: {r.status_code}")
    if r.status_code != 200:
        print(f"[AlphaVantage] Error body: {r.text[:300]}")
        return None

    try:
        data = r.json()
    except Exception:
        print(f"[AlphaVantage] JSON decode error")
        return None

    if "Information" in data:
        print(f"[AlphaVantage] Rate limit / info message: {data['Information'][:200]}")
        return None
    if "Error Message" in data:
        print(f"[AlphaVantage] Error: {data['Error Message'][:200]}")
        return None

    annual_reports = data.get("annualReports")  or []
    qtr_reports    = data.get("quarterlyReports") or []

    if not annual_reports and not qtr_reports:
        print(f"[AlphaVantage] No cash flow reports found for {ticker}")
        return None

    def _safe_float(v):
        if v is None or str(v).strip().lower() == "none":
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    def _parse_cf_reports(reports):
        fcf_list            = []
        dividends_paid_list = []
        for rec in reports:
            period = str(rec.get("fiscalDateEnding", ""))[:10]
            if not period:
                continue
            op_cf    = _safe_float(rec.get("operatingCashflow"))
            capex    = _safe_float(rec.get("capitalExpenditures"))
            div_paid = _safe_float(rec.get("dividendPayout"))
            if div_paid is None:
                div_paid = _safe_float(rec.get("dividendPayoutCommonStock"))

            if op_cf is not None and capex is not None:
                fcf = op_cf - abs(capex)
            elif op_cf is not None:
                fcf = op_cf
            else:
                fcf = None

            if fcf is not None:
                fcf_list.append({"period": period, "value": round(fcf, 2)})
            if div_paid is not None:
                dividends_paid_list.append({"period": period, "value": round(abs(div_paid), 2)})

        return {
            "fcf":           fcf_list            or None,
            "dividendsPaid": dividends_paid_list  or None,
        }

    print(f"[AlphaVantage] CASH_FLOW success for {ticker}: "
          f"{len(annual_reports)} annual, {len(qtr_reports)} quarterly reports")

    return {
        "annual":    _parse_cf_reports(annual_reports),
        "quarterly": _parse_cf_reports(qtr_reports),
    }


def _compute_margin(revenue_list, net_income_list):
    """Return [{period, value}] where value = (netIncome/revenue)*100 (percent)."""
    if not revenue_list or not net_income_list:
        return None
    rev_map = {r["period"]: r["value"] for r in revenue_list}
    result = []
    for ni in net_income_list:
        rev = rev_map.get(ni["period"])
        if rev and rev != 0:
            result.append({"period": ni["period"], "value": round(ni["value"] / rev * 100, 2)})
    return result or None


def _compute_growth(revenue_list):
    """Return [{period, value}] where value = YoY revenue growth % (percent)."""
    if not revenue_list or len(revenue_list) < 2:
        return None
    sorted_rev = sorted(revenue_list, key=lambda x: x["period"])
    result = []
    for i in range(1, len(sorted_rev)):
        prev = sorted_rev[i - 1]["value"]
        if prev == 0:
            continue
        growth = (sorted_rev[i]["value"] - prev) / abs(prev) * 100
        result.append({"period": sorted_rev[i]["period"], "value": round(growth, 2)})
    return result or None


def _compute_div_growth(divs):
    """Return [{period, value}] YoY dividend growth % from annual dividend sums."""
    try:
        if divs is None or divs.empty:
            return None
        ann = divs.resample("YE").sum()
        ann = ann[ann.index.year < datetime.now().year]
        ann = ann[ann > 0]
        if len(ann) < 2:
            return None
        vals = list(ann.values)
        tss  = list(ann.index)
        result = []
        for i in range(1, len(vals)):
            prev = vals[i - 1]
            if prev == 0:
                continue
            growth = (vals[i] - prev) / prev * 100
            result.append({"period": tss[i].strftime("%Y-%m-%d"), "value": round(growth, 2)})
        return sorted(result, key=lambda x: x["period"]) or None
    except Exception:
        return None


def _compute_div_pershare(divs):
    """Return [{period, value}] annual dividend per share from annual dividend sums."""
    try:
        if divs is None or divs.empty:
            return None
        ann = divs.resample("YE").sum()
        ann = ann[ann.index.year < datetime.now().year]
        ann = ann[ann > 0]
        if len(ann) == 0:
            return None
        result = [{"period": ts.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
                  for ts, v in ann.items()]
        return sorted(result, key=lambda x: x["period"]) or None
    except Exception:
        return None


def _compute_payout_history(cf_annual, annual_ni):
    """Return [{period, value}] payout ratio % = dividendsPaid / netIncome * 100."""
    try:
        ni_map = {item["period"]: item["value"] for item in (annual_ni or [])}
        result = []
        for item in (cf_annual or {}).get("dividendsPaid") or []:
            ni = ni_map.get(item["period"])
            if ni and ni > 0:
                payout = item["value"] / ni * 100
                result.append({"period": item["period"], "value": round(payout, 2)})
        return sorted(result, key=lambda x: x["period"]) or None
    except Exception:
        return None


# ── Single holding fetch ───────────────────────────────────────────────────────

def _fetch_one(h: dict) -> dict:
    sym, shs, ac = h["ticker"], h["shares"], h["avgCost"]

    # CASH special case — skip yfinance entirely
    if sym == "CASH":
        mv = round(shs * ac, 4)
        return {
            "ticker":                 "CASH",
            "name":                   "Cash (USD)",
            "sector":                 "Cash",
            "industry":               "Cash",
            "shares":                 shs,
            "avgCost":                ac,
            "currentPrice":           ac,
            "marketValue":            mv,
            "costBasis":              mv,
            "gainLoss":               0.0,
            "gainLossPct":            0.0,
            "dividendYield":          0,
            "annualDividendPerShare": 0.0,
            "annualIncome":           0.0,
            "yieldOnCost":            0.0,
            "payoutRatio":            None,
            "trailingEps":            None,
            "dividendRate":           None,
            "consecutiveIncreases":   0,
            "dividendHistory":        [],
            "beta":                   None,
            "debtToEquity":           None,
            "fiveYearAvgYield":       None,
            "freeCashflow":           None,
            "operatingCashflow":      None,
            "totalDebt":              None,
            "ebitda":                 None,
            "divCagr5y":              None,
            "safetyScore":            None,
            "safetyGrade":            "N/A",
            "safetyFactors":          0,
            "divFrequency":           None,
            "divFrequencyEstimated":  False,
            "lastPaymentDate":        None,
            "lastPaymentAmount":      None,
        }

    try:
        t    = yf.Ticker(sym)
        info = t.info or {}
        divs = t.dividends
    except Exception:
        info = {}
        divs = None

    quote_type = (info.get("quoteType") or "").upper()
    is_etf     = quote_type in ("ETF", "MUTUALFUND")

    price = _price(info)

    # BUG 2 fix: dynamic frequency detection instead of hardcoded ×4
    freq, freq_est = _detect_div_frequency(divs)
    if divs is not None and not divs.empty:
        last_pay_amt  = round(float(divs.iloc[-1]), 6)
        last_pay_date = divs.index[-1].date().isoformat()
        ann_div       = round(last_pay_amt * freq, 6)
    else:
        last_pay_amt  = None
        last_pay_date = None
        ann_div       = 0.0

    history = _quarterly_history(divs, 12)
    streak  = _streak(divs)
    safety  = _safety_score(info, ann_div, streak)

    mv  = round(shs * price, 4) if price is not None else None
    cb  = round(shs * ac, 4)
    gl  = round(mv - cb, 4) if mv is not None else None
    # BUG 1 fix: guard against cb == 0
    glp = round(gl / cb, 6) if (gl is not None and cb) else None

    # Beta: try beta first, fall back to beta3Year (often available for ETFs)
    beta = info.get("beta")
    if beta is None:
        beta = info.get("beta3Year")

    # 5Y avg yield: stocks have fiveYearAvgDividendYield; ETFs do not —
    # fall back to dividendYield which yfinance returns in percent form for ETFs
    # (same format as fiveYearAvgDividendYield for stocks, e.g. 1.82 or 3.29)
    five_yr_yield = info.get("fiveYearAvgDividendYield")
    if five_yr_yield is None and is_etf:
        five_yr_yield = info.get("dividendYield")   # already in percent form for ETFs

    # ETF-specific: null out fields that don't apply to funds
    payout_ratio = None if is_etf else _s(info, "payoutRatio")
    debt_equity  = None if is_etf else _s(info, "debtToEquity")

    return {
        "ticker":                 sym,
        "name":                   _s(info, "shortName"),
        "sector":                 _s(info, "sector"),
        "industry":               _s(info, "industry"),
        "shares":                 shs,
        "avgCost":                ac,
        "currentPrice":           price,
        "marketValue":            mv,
        "costBasis":              cb,
        "gainLoss":               gl,
        "gainLossPct":            glp,
        "dividendYield":          _s(info, "dividendYield"),
        "annualDividendPerShare": ann_div,
        "annualIncome":           round(shs * ann_div, 4),
        # BUG 1 fix: guard against ac == 0
        "yieldOnCost":            round(ann_div / ac, 6) if (ann_div and ac) else None,
        "payoutRatio":            payout_ratio,
        "trailingEps":            _s(info, "trailingEps"),
        "dividendRate":           _s(info, "dividendRate"),
        "consecutiveIncreases":   streak,
        "dividendHistory":        history,
        # Feature A — new risk/quality fields
        "beta":                   beta,
        "debtToEquity":           debt_equity,
        "fiveYearAvgYield":       five_yr_yield,
        "freeCashflow":           _s(info, "freeCashflow"),
        "operatingCashflow":      _s(info, "operatingCashflow"),
        "totalDebt":              _s(info, "totalDebt"),
        "ebitda":                 _s(info, "ebitda"),
        "divCagr5y":              _div_cagr(divs, 5),
        # Feature C — heuristic safety score
        "safetyScore":            safety["score"],
        "safetyGrade":            safety["grade"],
        "safetyFactors":          safety["factorCount"],
        # BUG 2/3: frequency detection for dynamic annualization and calendar projection
        "divFrequency":           freq,
        "divFrequencyEstimated":  freq_est,
        "lastPaymentDate":        last_pay_date,
        "lastPaymentAmount":      last_pay_amt,
    }

# ── Shared portfolio build (with 15-min cache) ─────────────────────────────────

def _portfolio() -> dict:
    cached = _cget("portfolio")
    if cached:
        return cached

    holdings = [_fetch_one(h) for h in load_holdings()]

    tmv = sum(h["marketValue"]  for h in holdings if h["marketValue"]  is not None)
    tcb = sum(h["costBasis"]    for h in holdings)
    tgl = sum(h["gainLoss"]     for h in holdings if h["gainLoss"]     is not None)
    tai = sum(h["annualIncome"] for h in holdings)

    # Feature B — weighted portfolio beta
    beta_mv  = [(h["beta"], h["marketValue"]) for h in holdings if h["beta"] is not None and h["marketValue"] is not None]
    port_beta = round(sum(b * mv for b, mv in beta_mv) / sum(mv for _, mv in beta_mv), 4) if beta_mv else None
    beta_cov  = f"{len(beta_mv)}/{len(holdings)}"

    _goal = load_goal()["goal"]
    data = {
        "metadata": {
            "lastUpdated":      datetime.now(timezone.utc).isoformat(),
            "totalMarketValue": round(tmv, 2),
            "totalCostBasis":   round(tcb, 2),
            "totalGainLoss":    round(tgl, 2),
            "totalGainLossPct": round(tgl / tcb, 6) if tcb else None,
            "totalAnnualIncome":round(tai, 2),
            "portfolioYield":   round(tai / tmv, 6) if tmv else None,
            "portfolioYoC":     round(tai / tcb, 6) if tcb else None,
            "goalAnnualIncome": _goal,
            "goalProgress":     round(tai / _goal, 6) if _goal else 0,
            "portfolioBeta":    port_beta,
            "betaCoverage":     beta_cov,
        },
        "holdings": holdings,
    }
    _cset("portfolio", data)
    return data

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/portfolio")
def api_portfolio():
    return jsonify(_portfolio())


# ── Holdings CRUD ──────────────────────────────────────────────────────────────

@app.route("/api/holdings", methods=["GET"])
def api_holdings_list():
    """Return the raw holdings list (no yfinance, instant)."""
    return jsonify(load_holdings())


@app.route("/api/holdings", methods=["POST"])
def api_holdings_add():
    """Add a new holding."""
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    ticker = str(body.get("ticker", "")).strip().upper()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400

    try:
        shares = float(body.get("shares", 0))
        avg_cost = float(body.get("avgCost", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "shares and avgCost must be numbers"}), 400

    if shares <= 0:
        return jsonify({"error": "shares must be > 0"}), 400
    if avg_cost < 0:
        return jsonify({"error": "avgCost must be >= 0"}), 400

    holdings = load_holdings()
    if any(h["ticker"] == ticker for h in holdings):
        return jsonify({"error": f"{ticker} already exists in portfolio"}), 409

    # Optionally validate ticker via yfinance (skip for CASH)
    warning = None
    if ticker != "CASH":
        try:
            info = yf.Ticker(ticker).info or {}
            if not info.get("symbol") and not info.get("shortName"):
                warning = f"Could not verify {ticker} via yfinance — added anyway"
        except Exception:
            warning = f"Could not verify {ticker} via yfinance — added anyway"

    new_holding = {"ticker": ticker, "shares": shares, "avgCost": avg_cost}
    holdings.append(new_holding)
    save_holdings(holdings)
    _clear_portfolio_cache()

    resp = dict(new_holding)
    if warning:
        resp["warning"] = warning
    return jsonify(resp), 201


@app.route("/api/holdings/<ticker>", methods=["PUT"])
def api_holdings_update(ticker):
    """Update shares and/or avgCost for an existing holding."""
    ticker = ticker.upper().strip()

    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    holdings = load_holdings()
    idx = next((i for i, h in enumerate(holdings) if h["ticker"] == ticker), None)
    if idx is None:
        return jsonify({"error": f"{ticker} not found"}), 404

    h = holdings[idx]

    if "shares" in body:
        try:
            shares = float(body["shares"])
        except (TypeError, ValueError):
            return jsonify({"error": "shares must be a number"}), 400
        if shares <= 0:
            return jsonify({"error": "shares must be > 0"}), 400
        h["shares"] = shares

    if "avgCost" in body:
        try:
            avg_cost = float(body["avgCost"])
        except (TypeError, ValueError):
            return jsonify({"error": "avgCost must be a number"}), 400
        if avg_cost < 0:
            return jsonify({"error": "avgCost must be >= 0"}), 400
        h["avgCost"] = avg_cost

    holdings[idx] = h
    save_holdings(holdings)
    _clear_portfolio_cache()
    return jsonify(h)


@app.route("/api/holdings/<ticker>", methods=["DELETE"])
def api_holdings_delete(ticker):
    """Remove a holding."""
    ticker = ticker.upper().strip()
    holdings = load_holdings()
    new_holdings = [h for h in holdings if h["ticker"] != ticker]
    if len(new_holdings) == len(holdings):
        return jsonify({"error": f"{ticker} not found"}), 404
    save_holdings(new_holdings)
    _clear_portfolio_cache()
    return jsonify({"deleted": ticker})


# ── Existing read-only endpoints ───────────────────────────────────────────────

@app.route("/api/predict/<ticker>")
def api_predict(ticker):
    ticker = ticker.upper().strip()
    if ticker == "CASH":
        return jsonify({"error": f"No dividend history for {ticker}"}), 404
    cached = _cget(f"pred_{ticker}")
    if cached:
        return jsonify(cached)

    try:
        t    = yf.Ticker(ticker)
        info = t.info or {}
        divs = t.dividends
    except Exception:
        return jsonify({"error": f"Cannot fetch {ticker}"}), 400

    if divs is None or divs.empty:
        return jsonify({"error": f"No dividend history for {ticker}"}), 404

    price   = _price(info)
    name    = _s(info, "shortName") or ticker
    sector  = _s(info, "sector")
    eps     = _s(info, "trailingEps")
    pr      = _s(info, "payoutRatio")
    history = _quarterly_history(divs, 12)

    # CAGR — reuse shared helper (refactored from inline closure)
    c1, c5, c10, c20 = _div_cagr(divs, 1), _div_cagr(divs, 5), _div_cagr(divs, 10), _div_cagr(divs, 20)

    # Consistency: quarters with payment / total quarters in history
    try:
        q_series   = divs.resample("QE").sum()
        total_q    = max(1, len(q_series))
        paid_q     = int((q_series > 0).sum())
        consistency = round(paid_q / total_q * 100, 1)
    except Exception:
        consistency = 0.0

    streak  = _streak(divs)
    # BUG 2 fix: use dynamic frequency instead of hardcoded ×4
    freq_pred, freq_pred_est = _detect_div_frequency(divs)
    ann_div = round(float(divs.iloc[-1]) * freq_pred, 6)

    # Earnings coverage + payout ratio
    ec = round(eps / ann_div, 2) if eps and ann_div else None
    if not pr and eps and ann_div:
        pr = round(ann_div / eps, 4)

    # Confidence score
    cs   = consistency
    sb   = min(streak * 5, 50)
    crp  = 25 if (pr and pr > 0.75) else 0
    conf = min(100.0, max(0.0, cs + sb - crp))
    tier = "High" if conf >= 80 else ("Medium" if conf >= 50 else "Low")

    # Last raw payment (use actual payment, not quarterly-resampled)
    last_a = float(divs.iloc[-1])
    last_d_ts = divs.index[-1]
    last_d = last_d_ts  # keep as Timestamp for .date() / .strftime()

    # Forward projection using detected frequency (3 years = freq * 3 payments)
    gr          = c5 or c1 or 0.0
    today       = date.today()
    base_date   = last_d.date() if last_d is not None else today
    months_step = 12 // freq_pred  # months between payments

    payouts = []
    n_proj  = freq_pred * 3  # 3 years of payments
    for i in range(n_proj):
        amt = last_a * ((1 + gr / freq_pred) ** (i + 1))
        ed  = _add_months(base_date, (i + 1) * months_step)
        pd  = ed + timedelta(days=14)
        payouts.append({
            "exDate":     ed.strftime("%Y-%m-%d"),
            "payDate":    pd.strftime("%Y-%m-%d"),
            "amount":     round(amt, 4),
            "yoy":        round(gr, 4),
            "confidence": tier if i < freq_pred else ("Medium" if conf >= 50 else "Low"),
        })

    proj_annual = sum(p["amount"] for p in payouts[:freq_pred])  # first year of payments
    next_days   = (date.fromisoformat(payouts[0]["exDate"]) - today).days if payouts else None

    result = {
        "ticker":          ticker,
        "name":            name,
        "sector":          sector,
        "currentPrice":    price,
        "lastDeclared":    {
            "amount": round(last_a, 4),
            "date":   last_d.strftime("%Y-%m-%d") if last_d else None,
        },
        "nextPredicted":   {
            "amount":   payouts[0]["amount"] if payouts else None,
            "date":     payouts[0]["exDate"] if payouts else None,
            "daysAway": next_days,
        },
        "confidence":      round(conf, 1),
        "confidenceTier":  tier,
        "predictedPayouts":payouts,
        "history":         history,
        "growth":          {"cagr1y": c1, "cagr5y": c5, "cagr10y": c10, "cagr20y": c20},
        "consistency":     consistency,
        "consecutiveIncreases": streak,
        "safety":          {"payoutRatio": pr, "earningsCoverage": ec, "annualDivPerShare": ann_div},
        "confidenceBreakdown": {"consistencyScore": round(cs, 1), "streakBonus": round(sb, 1), "cutRiskPenalty": round(crp, 1)},
        "projectedAnnual":              round(proj_annual, 2),
        "projectedAnnualFor100Shares":  round(proj_annual * 100, 2),
    }
    _cset(f"pred_{ticker}", result)
    return jsonify(result)


@app.route("/api/income")
def api_income():
    cached = _cget("income")
    if cached:
        return jsonify(cached)

    port  = _portfolio()
    today = date.today()

    # Build 12-month window starting this month
    months_keys = []
    for i in range(12):
        total = today.month - 1 + i
        yr    = today.year + total // 12
        mo    = total % 12 + 1
        months_keys.append(f"{yr:04d}-{mo:02d}")

    monthly = {k: {"total": 0.0, "holdings": []} for k in months_keys}

    for h in port["holdings"]:
        # BUG 3 fix: project forward using detected frequency instead of static pay_months
        last_date_str = h.get("lastPaymentDate")
        last_amt      = h.get("lastPaymentAmount") or 0.0
        freq          = h.get("divFrequency") or 4
        shs           = h["shares"]

        if not last_date_str or not last_amt:
            continue

        last_pay    = date.fromisoformat(last_date_str)
        months_step = 12 // freq  # 1 for monthly, 3 for quarterly, 6 for semi-annual, 12 for annual

        proj = last_pay
        for _ in range(freq + 2):  # project enough cycles to fill the 12-month window
            proj = _add_months(proj, months_step)
            key  = f"{proj.year:04d}-{proj.month:02d}"
            if key not in monthly:
                continue
            amt = round(last_amt * shs, 2)
            monthly[key]["total"] = round(monthly[key]["total"] + amt, 2)
            monthly[key]["holdings"].append({
                "ticker":    h["ticker"],
                "amount":    amt,
                "projected": True,
            })

    yearly_total = round(sum(v["total"] for v in monthly.values()), 2)
    result = {
        "monthly":     monthly,
        "yearlyTotal": yearly_total,
        "monthlyAvg":  round(yearly_total / 12, 2),
    }
    _cset("income", result)
    return jsonify(result)


@app.route("/api/performance")
def api_performance():
    cached = _cget("perf")
    if cached:
        return jsonify(cached)

    port = _portfolio()
    perf = []
    for h in port["holdings"]:
        pr = h["gainLossPct"] or 0.0
        # BUG 1 fix: guard against avgCost == 0
        dr = round(h["annualDividendPerShare"] / h["avgCost"], 6) if (h["annualDividendPerShare"] and h["avgCost"]) else 0.0
        perf.append({
            "ticker":        h["ticker"],
            "totalReturn":   round(pr + dr, 6),
            "priceReturn":   pr,
            "dividendReturn":dr,
            "gainLoss":      h["gainLoss"],
            "gainLossPct":   h["gainLossPct"],
        })

    m      = port["metadata"]
    result = {
        "holdings": perf,
        "portfolio": {
            "totalGainLoss":    m["totalGainLoss"],
            "totalGainLossPct": m["totalGainLossPct"],
        },
    }
    _cset("perf", result)
    return jsonify(result)


# ── Stock Deep-Dive financials ────────────────────────────────────────────────

@app.route("/api/financials/<ticker>")
def api_financials(ticker):
    ticker    = ticker.upper().strip()

    # CASH special case — no financial statements
    if ticker == "CASH":
        return jsonify({
            "isETF":  False,
            "isCash": True,
            "ticker": "CASH",
            "name":   "Cash (USD)",
            "sector": "Cash",
            "currentPrice": None,
            "annual":   {},
            "quarterly": {},
            "forward": {},
        })

    cache_key = f"fin_{ticker}"

    # In-memory cache (15 min)
    cached = _cget(cache_key)
    if cached:
        return jsonify(cached)

    # File cache (90 days)
    file_cached = _load_cache(ticker)
    if file_cached:
        try:
            lu = date.fromisoformat(file_cached.get("last_updated", ""))
            if (date.today() - lu).days < 90:
                _cset(cache_key, file_cached)
                return jsonify(file_cached)
        except Exception:
            pass

    try:
        t    = yf.Ticker(ticker)
        info = t.info or {}
    except Exception:
        return jsonify({"error": "fetch_failed", "message": f"Cannot fetch data for {ticker}"})

    # ── ETF detection ──────────────────────────────────────────────────────────
    quote_type = (info.get("quoteType") or "").upper()
    has_sector = bool(info.get("sector"))
    is_etf     = quote_type in ("ETF", "MUTUALFUND") or not has_sector

    if is_etf:
        # Double-check: if income statement exists, treat as stock
        try:
            stmt = t.income_stmt
            if stmt is not None and not stmt.empty:
                is_etf = False
        except Exception:
            pass

    if is_etf:
        price_history = []
        try:
            hist = t.history(period="5y", interval="1mo")
            for ts, row in hist.iterrows():
                try:
                    close = float(row["Close"])
                    if close == close and close > 0:   # NaN check
                        price_history.append({
                            "date":  ts.strftime("%Y-%m-%d"),
                            "price": round(close, 2),
                        })
                except (TypeError, ValueError, KeyError):
                    pass
        except Exception:
            pass

        try:
            _etf_divs = t.dividends
        except Exception:
            _etf_divs = None
        div_history = _quarterly_history(_etf_divs, 20)

        _etf_div_growth   = _compute_div_growth(_etf_divs)   if (_etf_divs is not None and not _etf_divs.empty) else None
        _etf_div_pershare = _compute_div_pershare(_etf_divs) if (_etf_divs is not None and not _etf_divs.empty) else None
        result = {
            "isETF":          True,
            "ticker":         ticker,
            "name":           _s(info, "shortName"),
            "currentPrice":   _price(info),
            "priceHistory":   price_history,
            "dividendHistory": div_history,
            # Top-level for backwards compatibility
            "divGrowth":      _etf_div_growth,
            "divPerShare":    _etf_div_pershare,
            # Also in annual dict so renderDeepDiveCharts can display them
            "annual": {
                "divGrowth":     _etf_div_growth,
                "divPerShare":   _etf_div_pershare,
                "payoutHistory": None,  # ETFs have no income statement
            },
        }
        _cset(cache_key, result)
        _save_cache(ticker, result)
        return jsonify(result)

    # ── Stock path ─────────────────────────────────────────────────────────────

    def _extract_row(df, *row_names):
        if df is None or df.empty:
            return None
        for name in row_names:
            if name in df.index:
                row = df.loc[name].dropna()
                items = sorted(
                    [{"period": str(col.date() if hasattr(col, "date") else col)[:10],
                      "value":  round(float(v), 2)}
                     for col, v in row.items()],
                    key=lambda x: x["period"]
                )
                return items or None
        return None

    # yfinance data
    ann_yf = {"revenue": None, "netIncome": None, "eps": None, "expenses": None}
    qtr_yf = {"revenue": None, "netIncome": None, "eps": None, "expenses": None}

    try:
        stmt = t.income_stmt
        if stmt is not None and not stmt.empty:
            ann_yf["revenue"]   = _extract_row(stmt, "Total Revenue", "Revenue")
            ann_yf["netIncome"] = _extract_row(stmt, "Net Income", "Net Income Common Stockholders")
            ann_yf["eps"]       = _extract_row(stmt, "Basic EPS", "Diluted EPS")
            ann_yf["expenses"]  = _extract_row(stmt, "Total Expenses", "Operating Expense",
                                               "Total Operating Expenses", "Cost Of Revenue")
    except Exception:
        pass

    try:
        qstmt = t.quarterly_income_stmt
        if qstmt is not None and not qstmt.empty:
            qtr_yf["revenue"]   = _extract_row(qstmt, "Total Revenue", "Revenue")
            qtr_yf["netIncome"] = _extract_row(qstmt, "Net Income", "Net Income Common Stockholders")
            qtr_yf["eps"]       = _extract_row(qstmt, "Basic EPS", "Diluted EPS")
            qtr_yf["expenses"]  = _extract_row(qstmt, "Total Expenses", "Operating Expense",
                                               "Total Operating Expenses", "Cost Of Revenue")
    except Exception:
        pass

    # Alpha Vantage enrichment + merge with yfinance
    av_data    = _fetch_av_financials(ticker)
    av_annual  = av_data["annual"]    if av_data else None
    av_qtr     = av_data["quarterly"] if av_data else None

    time.sleep(13)  # stay within 5 req/min AV free tier limit
    cf_data    = _fetch_av_cashflow(ticker)
    cf_annual  = cf_data["annual"]    if cf_data else None
    cf_qtr     = cf_data["quarterly"] if cf_data else None
    annual_fcf = cf_annual.get("fcf") if cf_annual else None
    qtr_fcf    = cf_qtr.get("fcf")    if cf_qtr    else None

    def _m(av_src, yf_src, key):
        tv = av_src.get(key) if av_src else None
        yv = yf_src.get(key)
        if tv and yv:
            return _merge_periods(tv, yv)
        return tv or yv

    annual_rev = _m(av_annual, ann_yf, "revenue")
    annual_ni  = _m(av_annual, ann_yf, "netIncome")
    annual_eps = _m(av_annual, ann_yf, "eps")
    annual_exp = _m(av_annual, ann_yf, "expenses")

    # Explicit yfinance EPS fill: cover any periods yfinance has that are still missing
    if ann_yf.get("eps") and annual_ni:
        have_eps   = {e["period"] for e in (annual_eps or [])}
        yf_eps_map = {e["period"]: e for e in ann_yf["eps"]}
        yf_fill    = [yf_eps_map[p["period"]] for p in annual_ni
                      if p["period"] not in have_eps and p["period"] in yf_eps_map]
        if yf_fill:
            annual_eps = _merge_periods(annual_eps or [], yf_fill)

    qtr_rev = _m(av_qtr, qtr_yf, "revenue")
    qtr_ni  = _m(av_qtr, qtr_yf, "netIncome")
    qtr_eps = _m(av_qtr, qtr_yf, "eps")
    qtr_exp = _m(av_qtr, qtr_yf, "expenses")

    # Computed metrics (percent values)
    annual_margin = _compute_margin(annual_rev, annual_ni)
    annual_growth = _compute_growth(annual_rev)
    qtr_margin    = _compute_margin(qtr_rev, qtr_ni)
    qtr_growth    = _compute_growth(qtr_rev)

    # Dividend metrics (yfinance dividend history)
    try:
        _divs = t.dividends
    except Exception:
        _divs = None
    ann_div_growth   = _compute_div_growth(_divs)   if (_divs is not None and not _divs.empty) else None
    ann_div_pershare = _compute_div_pershare(_divs) if (_divs is not None and not _divs.empty) else None
    payout_hist      = _compute_payout_history(cf_annual, annual_ni)

    # ── Forward estimates ──────────────────────────────────────────────────────
    fwd = {
        "epsCurrentYear": None, "epsNextYear": None,
        "revCurrentYear": None, "revNextYear": None,
        "ltGrowthRate":   None,
        "priceTargetLow": None, "priceTargetMean": None, "priceTargetHigh": None,
        "currentPrice":   _price(info),
    }

    try:
        ee = t.earnings_estimate
        if ee is not None and not ee.empty:
            def _ee_val(col):
                if col not in ee.columns:
                    return None
                for rn in ("Avg", "avg", "Average"):
                    if rn in ee.index:
                        v = ee.loc[rn, col]
                        return round(float(v), 4) if v is not None and str(v) != "nan" else None
                return None
            fwd["epsCurrentYear"] = _ee_val("0y")
            fwd["epsNextYear"]    = _ee_val("+1y")
    except Exception:
        pass

    # EPS fallbacks from info
    if fwd["epsCurrentYear"] is None:
        try:
            v = info.get("trailingEps")
            if v is not None:
                fwd["epsCurrentYear"] = round(float(v), 4)
        except Exception:
            pass
    if fwd["epsNextYear"] is None:
        try:
            v = info.get("forwardEps")
            if v is not None:
                fwd["epsNextYear"] = round(float(v), 4)
        except Exception:
            pass

    try:
        re = t.revenue_estimate
        if re is not None and not re.empty:
            def _re_val(col):
                if col not in re.columns:
                    return None
                for rn in ("Avg", "avg", "Average"):
                    if rn in re.index:
                        v = re.loc[rn, col]
                        return round(float(v), 2) if v is not None and str(v) != "nan" else None
                return None
            fwd["revCurrentYear"] = _re_val("0y")
            fwd["revNextYear"]    = _re_val("+1y")
    except Exception:
        pass

    try:
        ge = t.growth_estimates
        if ge is not None and not ge.empty:
            for idx_name in ("5 Years (per annum)", "Next 5 Years (per annum)", "+5y", "5y"):
                if idx_name in ge.index:
                    col = ge.columns[0] if len(ge.columns) else None
                    if col is not None:
                        v = ge.loc[idx_name, col]
                        if v is not None and str(v) != "nan":
                            fwd["ltGrowthRate"] = round(float(v), 4)
                    break
    except Exception:
        pass

    # LT growth fallback
    if fwd["ltGrowthRate"] is None:
        for k in ("earningsGrowth", "revenueGrowth"):
            try:
                v = info.get(k)
                if v is not None:
                    fwd["ltGrowthRate"] = round(float(v), 4)
                    break
            except Exception:
                pass

    try:
        apt = t.analyst_price_targets
        if apt is not None and isinstance(apt, dict):
            fwd["priceTargetLow"]  = apt.get("low")
            fwd["priceTargetMean"] = apt.get("mean")
            fwd["priceTargetHigh"] = apt.get("high")
    except Exception:
        pass

    # Price target fallbacks
    for fwd_key, info_key in (
        ("priceTargetMean", "targetMeanPrice"),
        ("priceTargetLow",  "targetLowPrice"),
        ("priceTargetHigh", "targetHighPrice"),
    ):
        if fwd[fwd_key] is None:
            try:
                v = info.get(info_key)
                if v is not None:
                    fwd[fwd_key] = round(float(v), 2)
            except Exception:
                pass

    result = {
        "isETF":        False,
        "ticker":       ticker,
        "name":         _s(info, "shortName"),
        "sector":       _s(info, "sector"),
        "currentPrice": _price(info),
        "annual": {
            "revenue": annual_rev, "netIncome": annual_ni,
            "eps": annual_eps,     "expenses":  annual_exp,
            "margin": annual_margin, "growth": annual_growth,
            "divGrowth":     ann_div_growth,
            "divPerShare":   ann_div_pershare,
            "payoutHistory": payout_hist,
            "fcf":           annual_fcf,
        },
        "quarterly": {
            "revenue": qtr_rev, "netIncome": qtr_ni,
            "eps": qtr_eps,     "expenses":  qtr_exp,
            "margin": qtr_margin, "growth": qtr_growth,
            "fcf":    qtr_fcf,
        },
        "forward": fwd,
    }
    _cset(cache_key, result)
    _save_cache(ticker, result)
    return jsonify(result)


@app.route("/api/financials/<ticker>/refresh", methods=["POST"])
def api_financials_refresh(ticker):
    ticker = ticker.upper().strip()
    path   = _cache_path(ticker)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    _cache.pop(f"fin_{ticker}", None)
    return jsonify({"ok": True})


# ── Goal settings ─────────────────────────────────────────────────────────────

@app.route("/api/goal", methods=["GET"])
def api_goal_get():
    return jsonify(load_goal())


@app.route("/api/goal", methods=["POST"])
def api_goal_post():
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    try:
        goal = float(body.get("goal", 24000))
    except (TypeError, ValueError):
        return jsonify({"error": "goal must be a number"}), 400

    if goal <= 0:
        return jsonify({"error": "goal must be > 0"}), 400

    raw_ms = body.get("milestones", _GOAL_DEFAULTS["milestones"])
    try:
        milestones = [float(x) for x in raw_ms]
    except (TypeError, ValueError):
        return jsonify({"error": "milestones must be a list of numbers"}), 400

    if len(milestones) != 4:
        return jsonify({"error": "milestones must have exactly 4 values"}), 400

    data = {"goal": goal, "milestones": milestones}
    save_goal(data)
    _clear_portfolio_cache()
    return jsonify(data)


# ── Startup ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print()
    print("  Salary 2045 Tracker running at http://localhost:5000")
    print()
    app.run(host="0.0.0.0", port=5000, debug=False)
