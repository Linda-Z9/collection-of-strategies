import json
import math
import os
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pandas as pd


DEFAULT_CONFIG = {
    "risky": ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"],
    "cash": "BIL",
    "equity": ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ"],
    "defensive": ["TLT", "IEF", "GLD", "BIL"],
    "start_date": "2007-01-01",
    "trend_ma_window": 200,
    "momentum_window": 126,
    "vol_window": 63,
    "max_single_etf_weight": 0.25,
    "equity_cap_risk_on": 0.80,
    "equity_cap_risk_off": 0.30,
    "transaction_cost_bps": 10,
}


def _cache_path(symbol):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    folder = os.path.join(root, "data", "etf_cache")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{symbol}.json")


def _fetch_yahoo_daily(symbol):
    cache_file = _cache_path(symbol)
    if os.path.exists(cache_file):
        age_seconds = datetime.now(timezone.utc).timestamp() - os.path.getmtime(cache_file)
        if age_seconds < 6 * 60 * 60:
            with open(cache_file, "r", encoding="utf-8") as handle:
                rows = json.load(handle)
            if len(rows) >= 1000:
                return pd.DataFrame(rows)

    now = int(datetime.now(timezone.utc).timestamp())
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol)}?period1=0&period2={now}&interval=1d&events=history&includeAdjustedClose=true"
    )
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            )
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code == 429:
            raise RuntimeError(
                f"Yahoo throttled the request for {symbol}. Wait a minute and refresh, or use cached data."
            ) from error
        raise

    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError(f"No Yahoo chart result for {symbol}.")

    timestamps = result.get("timestamp") or []
    quote_data = (result.get("indicators", {}).get("quote") or [{}])[0]
    adjusted = ((result.get("indicators", {}).get("adjclose") or [{}])[0]).get("adjclose") or []
    close = quote_data.get("close") or []
    rows = []
    for index, timestamp in enumerate(timestamps):
        price = adjusted[index] if index < len(adjusted) and adjusted[index] is not None else close[index]
        if price is None or not math.isfinite(float(price)) or float(price) <= 0:
            continue
        rows.append(
            {
                "date": datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d"),
                "close": float(price),
            }
        )

    with open(cache_file, "w", encoding="utf-8") as handle:
        json.dump(rows, handle)
    return pd.DataFrame(rows)


def _price_frame(symbols, start_date):
    frames = []
    row_counts = {}
    for symbol in symbols:
        frame = _fetch_yahoo_daily(symbol)
        row_counts[symbol] = len(frame)
        frame = frame[["date", "close"]].rename(columns={"close": symbol})
        frames.append(frame)

    prices = frames[0]
    for frame in frames[1:]:
        prices = prices.merge(frame, on="date", how="inner")
    prices["date"] = pd.to_datetime(prices["date"])
    prices = prices[prices["date"] >= pd.Timestamp(start_date)].sort_values("date").reset_index(drop=True)
    if prices.empty:
        raise RuntimeError("No overlapping ETF history after date alignment.")
    prices = prices.set_index("date")
    return prices, row_counts


def _summarize(points, capital):
    if len(points) < 2:
        return {"pnl": 0, "annReturn": 0, "annVol": 0, "maxDrawdown": 0, "sharpe": 0}
    equity = pd.Series([point["equity"] for point in points], index=pd.to_datetime([point["date"] for point in points]))
    returns = equity.pct_change().dropna()
    years = max(1 / 365, (equity.index[-1] - equity.index[0]).days / 365)
    cagr = (equity.iloc[-1] / capital) ** (1 / years) - 1
    ann_vol = returns.std(ddof=1) * math.sqrt(252) if len(returns) > 1 else 0
    drawdown = equity / equity.cummax() - 1
    downside = returns[returns < 0]
    downside_vol = math.sqrt((downside.pow(2).sum() / max(1, len(downside) - 1))) * math.sqrt(252)
    try:
        monthly = equity.resample("ME").last().pct_change().dropna()
        yearly = equity.resample("YE").last().pct_change().dropna()
    except ValueError:
        monthly = equity.resample("M").last().pct_change().dropna()
        yearly = equity.resample("Y").last().pct_change().dropna()
    return {
        "pnl": float(equity.iloc[-1] - capital),
        "annReturn": float(cagr),
        "cagr": float(cagr),
        "annVol": float(ann_vol),
        "maxDrawdown": float(drawdown.min()),
        "sharpe": 0 if ann_vol == 0 else float(cagr / ann_vol),
        "sortino": 0 if downside_vol == 0 else float(cagr / downside_vol),
        "calmar": 0 if drawdown.min() == 0 else float(cagr / abs(drawdown.min())),
        "winRateByMonth": float((monthly > 0).mean()) if len(monthly) else 0,
        "worstMonths": [{"month": str(index)[:7], "ret": float(value)} for index, value in monthly.nsmallest(10).items()],
        "performanceByYear": [{"year": str(index)[:4], "ret": float(value)} for index, value in yearly.items()],
    }


def _as_points(equity, weights_by_date=None):
    points = []
    for date, value in equity.items():
        weights = weights_by_date.get(date, {}) if weights_by_date else {}
        points.append(
            {
                "time": int(pd.Timestamp(date).timestamp() * 1000),
                "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                "equity": float(value),
                "weights": weights,
                "cashAllocation": float(weights.get("BIL", 0)) if weights else 0,
            }
        )
    return points


def _benchmark(name, prices, capital, symbols, target_weights, cost_bps=0):
    monthly_dates = set(prices.groupby(prices.index.to_period("M")).tail(1).index)
    weights = pd.Series(target_weights, dtype=float).reindex(symbols).fillna(0)
    weights = weights / weights.sum()
    equity = capital
    values = []
    for index in range(1, len(prices)):
        today = prices.index[index]
        yesterday = prices.index[index - 1]
        equity *= 1 + float((weights * (prices.loc[today, symbols] / prices.loc[yesterday, symbols] - 1)).sum())
        if today in monthly_dates and cost_bps:
            target = pd.Series(target_weights, dtype=float).reindex(symbols).fillna(0)
            target = target / target.sum()
            turnover = float((target - weights).abs().sum() / 2)
            equity -= equity * turnover * cost_bps / 10000
            weights = target
        values.append((today, equity))
    series = pd.Series(dict(values))
    points = _as_points(series)
    return {"name": name, "metrics": _summarize(points, capital), "points": points}


def run_backtest(capital=100000, config=None):
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    risky = cfg["risky"]
    cash = cfg["cash"]
    symbols = list(dict.fromkeys(risky + [cash]))
    prices, row_counts = _price_frame(symbols, cfg["start_date"])

    max_lookback = max(cfg["trend_ma_window"], cfg["momentum_window"], cfg["vol_window"])
    ma = prices.rolling(cfg["trend_ma_window"]).mean()
    momentum = prices / prices.shift(cfg["momentum_window"]) - 1
    vol = prices.pct_change().rolling(cfg["vol_window"]).std() * math.sqrt(252)
    monthly_dates = set(prices.groupby(prices.index.to_period("M")).tail(1).index)

    weights = pd.Series({symbol: 0 for symbol in symbols}, dtype=float)
    weights[cash] = 1.0
    equity = capital
    equity_values = []
    weights_by_date = {}
    weight_history = []
    trades = 0
    costs = 0.0
    turnover_total = 0.0

    for index in range(max_lookback + 1, len(prices)):
        today = prices.index[index]
        yesterday = prices.index[index - 1]
        daily_return = float((weights * (prices.loc[today, symbols] / prices.loc[yesterday, symbols] - 1)).sum())
        equity *= 1 + daily_return

        if today in monthly_dates:
            eligible = [
                symbol
                for symbol in risky
                if prices.loc[today, symbol] > ma.loc[today, symbol]
                and momentum.loc[today, symbol] > 0
                and vol.loc[today, symbol] > 0
            ]
            if eligible:
                inv_vol = (1 / vol.loc[today, eligible]).astype(float)
                target = inv_vol / inv_vol.sum()
                target = target.clip(upper=cfg["max_single_etf_weight"])
                target = target.reindex(symbols).fillna(0)
            else:
                target = pd.Series({symbol: 0 for symbol in symbols}, dtype=float)

            target[cash] = max(0.0, 1.0 - float(target.drop(cash, errors="ignore").sum()))
            spy_risk_on = prices.loc[today, "SPY"] > ma.loc[today, "SPY"]
            equity_cap = cfg["equity_cap_risk_on"] if spy_risk_on else cfg["equity_cap_risk_off"]
            equity_exposure = float(target.reindex(cfg["equity"]).fillna(0).sum())
            if equity_exposure > equity_cap:
                scale = equity_cap / equity_exposure
                target.loc[cfg["equity"]] = target.reindex(cfg["equity"]).fillna(0) * scale
                target[cash] = max(0.0, 1.0 - float(target.drop(cash, errors="ignore").sum()))

            turnover = float((target - weights).abs().sum() / 2)
            cost = equity * turnover * cfg["transaction_cost_bps"] / 10000
            equity -= cost
            costs += cost
            turnover_total += turnover
            trades += 1
            weights = target
            history_row = {
                "time": int(today.timestamp() * 1000),
                "date": today.strftime("%Y-%m-%d"),
                "weights": {symbol: float(weights[symbol]) for symbol in symbols},
                "turnover": turnover,
                "eligible": eligible,
                "regime": "risk_on" if spy_risk_on else "risk_off",
                "equityCap": float(equity_cap),
                "equityExposure": float(weights.reindex(cfg["equity"]).fillna(0).sum()),
                "cashAllocation": float(weights[cash]),
            }
            weight_history.append(history_row)

        weights_by_date[today] = {symbol: float(weights[symbol]) for symbol in symbols}
        equity_values.append((today, equity))

    equity_series = pd.Series(dict(equity_values))
    points = _as_points(equity_series, weights_by_date)
    for point in points:
        weights_for_point = point.get("weights") or {}
        point["equityExposure"] = float(sum(weights_for_point.get(symbol, 0) for symbol in cfg["equity"]))

    metrics = _summarize(points, capital)
    metrics.update(
        {
            "trades": trades,
            "numberOfTrades": trades,
            "costs": float(costs),
            "turnover": float(turnover_total),
            "avgTurnover": float(turnover_total / trades) if trades else 0,
            "averageCashAllocation": float(sum(point.get("cashAllocation", 0) for point in points) / max(1, len(points))),
            "averageEquityExposure": float(sum(point.get("equityExposure", 0) for point in points) / max(1, len(points))),
        }
    )

    benchmarks = [
        _benchmark(
            "Equal-Weight Universe Monthly",
            prices.iloc[max_lookback:],
            capital,
            symbols,
            {symbol: 1 / len(symbols) for symbol in symbols},
            cfg["transaction_cost_bps"],
        ),
    ]

    return {
        "mode": "streamlit-python-defensive-trend-risk-parity-etf",
        "strategy": "Defensive Trend + Risk-Parity ETF Allocation",
        "symbols": symbols,
        "riskySymbols": risky,
        "cashSymbol": cash,
        "equitySymbols": cfg["equity"],
        "defensiveSymbols": cfg["defensive"],
        "frequency": "Yahoo adjusted daily closes, Python month-end rebalance",
        "rows": {
            "aligned": len(prices),
            "start": prices.index[0].strftime("%Y-%m-%d"),
            "end": prices.index[-1].strftime("%Y-%m-%d"),
            "backtestStart": points[0]["date"] if points else None,
            "backtestEnd": points[-1]["date"] if points else None,
            "bySymbol": row_counts,
        },
        "assumptions": cfg,
        "metrics": metrics,
        "benchmarks": benchmarks,
        "benchmark": benchmarks[0],
        "latestRegime": weight_history[-1] if weight_history else None,
        "latestWeights": points[-1]["weights"] if points else {},
        "weightHistory": weight_history,
        "points": points,
    }
