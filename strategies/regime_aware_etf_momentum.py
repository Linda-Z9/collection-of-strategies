import io
import json
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen

import pandas as pd


DEFAULT_CONFIG = {
    "risk_assets": ["SPY", "QQQ", "IWM", "EFA", "EEM", "HYG"],
    "defensive_assets": ["TLT", "IEF", "GLD", "SHY"],
    "start_date": "2007-01-01",
    "price_warmup_start": "2005-01-01",
    "trend_window": 200,
    "momentum_6m_window": 126,
    "momentum_12m_window": 252,
    "skip_1m_window": 21,
    "portfolio_vol_window": 60,
    "vol_trigger": 0.15,
    "vol_target": 0.12,
    "max_weight": 0.40,
    "transaction_cost_bps": 5,
    "macro_lag_months": 1,
}

FRED_SERIES = {
    "CLI": "USALOLITONOSTSAM",
    "YC": "T10Y3M",
    "CREDIT": "BAMLH0A0HYM2",
    "CPI": "CPIAUCSL",
    "UNRATE": "UNRATE",
}

OAS_HISTORY_URL = (
    "https://raw.githubusercontent.com/fagan2888/GMS_VAAS/"
    "183b1c5e4cf874a43bd4145275fbb60986e05a33/IR_txt_2/data/BAMLH0A0HYM2.txt"
)

REGIME_RULES = {
    "Recovery": ["SPY", "QQQ", "IWM", "EFA", "EEM", "HYG", "GLD"],
    "Expansion": ["SPY", "QQQ", "IWM", "EFA", "EEM", "HYG"],
    "Slowdown": ["SPY", "QQQ", "TLT", "IEF", "GLD", "SHY"],
    "Contraction": ["TLT", "IEF", "GLD", "SHY"],
}


def _root_folder(*parts):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def _request_text(url, timeout=40, attempts=1):
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            )
        },
    )
    last_error = None
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
    raise last_error


def _fetch_yahoo_daily(symbol):
    cache_file = os.path.join(_root_folder("data", "etf_cache"), f"{symbol}.json")
    rows = []
    if os.path.exists(cache_file):
        age_seconds = datetime.now(timezone.utc).timestamp() - os.path.getmtime(cache_file)
        with open(cache_file, "r", encoding="utf-8") as handle:
            rows = json.load(handle)
        if len(rows) >= 1000 and age_seconds < 24 * 60 * 60:
            return pd.DataFrame(rows)

    now = int(datetime.now(timezone.utc).timestamp())
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol)}?period1=0&period2={now}&interval=1d&events=history&includeAdjustedClose=true"
    )
    try:
        payload = json.loads(_request_text(url))
    except Exception:
        if os.path.exists(cache_file):
            return pd.DataFrame(rows)
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


def _load_prices(symbols, warmup_start):
    frames = []
    row_counts = {}
    coverage = []
    for symbol in symbols:
        frame = _fetch_yahoo_daily(symbol)
        row_counts[symbol] = len(frame)
        coverage.append(
            {
                "series": symbol,
                "role": "ETF adjusted close",
                "start": str(frame["date"].min()),
                "end": str(frame["date"].max()),
                "rows": len(frame),
            }
        )
        frames.append(frame[["date", "close"]].rename(columns={"close": symbol}))
    prices = frames[0]
    for frame in frames[1:]:
        prices = prices.merge(frame, on="date", how="outer")
    prices["date"] = pd.to_datetime(prices["date"])
    prices = prices[prices["date"] >= pd.Timestamp(warmup_start)].sort_values("date").set_index("date").ffill()
    if prices.empty:
        raise RuntimeError("No overlapping ETF price history was available.")
    return prices, row_counts, coverage


def _fetch_fred_series(series_id):
    cache_file = os.path.join(_root_folder("data", "fred_cache"), f"{series_id}.csv")
    cached_frame = pd.read_csv(cache_file) if os.path.exists(cache_file) else None
    if os.path.exists(cache_file):
        age_seconds = datetime.now(timezone.utc).timestamp() - os.path.getmtime(cache_file)
        if age_seconds < 24 * 60 * 60:
            return cached_frame
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={quote(series_id)}&cosd=2000-01-01"
    try:
        text = _request_text(url, timeout=15, attempts=1)
        frame = pd.read_csv(io.StringIO(text))
    except Exception as urllib_error:
        temporary_file = f"{cache_file}.tmp"
        try:
            subprocess.run(
                [
                    "curl.exe",
                    "-L",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--connect-timeout",
                    "10",
                    "--max-time",
                    "60",
                    "-o",
                    temporary_file,
                    url,
                ],
                check=True,
                timeout=75,
            )
            frame = pd.read_csv(temporary_file)
        except Exception as curl_error:
            if series_id == "T10Y3M":
                try:
                    ten_year = _fetch_yahoo_daily("^TNX").rename(columns={"close": "DGS10"})
                    three_month = _fetch_yahoo_daily("^IRX").rename(columns={"close": "DGS3MO"})
                    frame = ten_year.merge(three_month, on="date", how="inner")
                    frame[series_id] = frame["DGS10"] - frame["DGS3MO"]
                    frame = frame[["date", series_id]].rename(columns={"date": "observation_date"})
                except Exception:
                    if cached_frame is not None:
                        return cached_frame
                    raise
            elif cached_frame is not None:
                return cached_frame
            else:
                raise RuntimeError(
                    f"FRED download failed for {series_id} through urllib and curl.exe: {curl_error}"
                ) from urllib_error
        finally:
            if os.path.exists(temporary_file):
                os.remove(temporary_file)
    if cached_frame is not None and not cached_frame.empty:
        date_column = frame.columns[0]
        frame = pd.concat([cached_frame, frame], ignore_index=True)
        frame = frame.drop_duplicates(subset=[date_column], keep="last").sort_values(date_column)
    frame.to_csv(cache_file, index=False)
    return frame


def _merge_oas_history():
    cache_file = os.path.join(_root_folder("data", "fred_cache"), "BAMLH0A0HYM2.csv")
    history_file = os.path.join(_root_folder("data", "fred_cache"), "BAMLH0A0HYM2-history.txt")
    if not os.path.exists(history_file):
        try:
            text = _request_text(OAS_HISTORY_URL, timeout=30, attempts=2)
            with open(history_file, "w", encoding="utf-8") as handle:
                handle.write(text)
        except Exception:
            return
    if not os.path.exists(cache_file):
        return
    history = pd.read_csv(
        history_file,
        skiprows=73,
        sep=r"\s+",
        names=["observation_date", "BAMLH0A0HYM2"],
        na_values=".",
    )
    history["observation_date"] = pd.to_datetime(history["observation_date"], format="%Y-%m-%d", errors="coerce")
    history["BAMLH0A0HYM2"] = pd.to_numeric(history["BAMLH0A0HYM2"], errors="coerce")
    history = history.dropna()
    current = pd.read_csv(cache_file)
    current["observation_date"] = pd.to_datetime(current["observation_date"], errors="coerce")
    merged = pd.concat([history, current], ignore_index=True)
    merged = merged.dropna().drop_duplicates("observation_date", keep="last").sort_values("observation_date")
    merged["observation_date"] = merged["observation_date"].dt.strftime("%Y-%m-%d")
    merged.to_csv(cache_file, index=False)


def _load_macro(config):
    _merge_oas_history()
    combined = None
    counts = {}
    coverage = []
    for name, series_id in FRED_SERIES.items():
        frame = _fetch_fred_series(series_id)
        date_column = frame.columns[0]
        value_column = frame.columns[-1]
        frame = frame[[date_column, value_column]].rename(columns={date_column: "date", value_column: name})
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
        frame = frame.dropna(subset=["date", name]).set_index("date")
        counts[series_id] = len(frame)
        coverage.append(
            {
                "series": series_id,
                "role": name,
                "start": frame.index.min().strftime("%Y-%m-%d"),
                "end": frame.index.max().strftime("%Y-%m-%d"),
                "rows": len(frame),
            }
        )
        combined = frame if combined is None else combined.join(frame, how="outer")

    monthly = combined.sort_index().resample("ME").last()
    credit_observed = monthly["CREDIT"].notna()
    try:
        hyg = _fetch_yahoo_daily("HYG")
        hyg["date"] = pd.to_datetime(hyg["date"])
        hyg_monthly = hyg.set_index("date")["close"].resample("ME").last()
        hyg_return_3m = hyg_monthly.pct_change(3).reindex(monthly.index)
        overlap = credit_observed & hyg_return_3m.notna()
        variance = float(hyg_return_3m[overlap].var())
        if overlap.sum() >= 24 and variance > 0:
            slope = float(hyg_return_3m[overlap].cov(monthly.loc[overlap, "CREDIT"]) / variance)
            intercept = float(monthly.loc[overlap, "CREDIT"].mean() - slope * hyg_return_3m[overlap].mean())
            credit_proxy = (intercept + slope * hyg_return_3m).clip(lower=0)
            monthly["CREDIT"] = monthly["CREDIT"].fillna(credit_proxy)
    except Exception:
        pass
    monthly["CREDIT_SOURCE"] = credit_observed.map({True: "BAMLH0A0HYM2", False: "HYG calibrated proxy"})
    monthly = monthly.ffill()
    monthly["CLI_TREND"] = monthly["CLI"] - monthly["CLI"].rolling(12).mean()
    monthly["CLI_MOMENTUM"] = monthly["CLI"] - monthly["CLI"].shift(3)
    monthly["CREDIT_CHANGE"] = monthly["CREDIT"] - monthly["CREDIT"].shift(3)
    monthly["UNRATE_CHANGE"] = monthly["UNRATE"] - monthly["UNRATE"].shift(3)
    monthly["CPI_YOY"] = monthly["CPI"].pct_change(12)
    monthly = monthly.shift(config["macro_lag_months"]).dropna(
        subset=["CLI_TREND", "CLI_MOMENTUM", "YC", "CREDIT_CHANGE", "UNRATE_CHANGE"]
    )

    def classify(row):
        if row["YC"] < 0 and row["CREDIT_CHANGE"] > 0.75 and row["UNRATE_CHANGE"] > 0.20:
            return "Contraction", True
        if row["CLI_TREND"] < 0 and row["CLI_MOMENTUM"] > 0:
            return "Recovery", False
        if row["CLI_TREND"] >= 0 and row["CLI_MOMENTUM"] > 0:
            return "Expansion", False
        if row["CLI_TREND"] >= 0 and row["CLI_MOMENTUM"] <= 0:
            return "Slowdown", False
        return "Contraction", False

    classifications = monthly.apply(classify, axis=1)
    monthly["regime"] = [value[0] for value in classifications]
    monthly["stressOverride"] = [value[1] for value in classifications]
    monthly.index = monthly.index.to_period("M")
    return monthly, counts, coverage


def _metrics(equity, capital):
    if len(equity) < 2:
        return {"pnl": 0, "annReturn": 0, "annVol": 0, "maxDrawdown": 0, "sharpe": 0}
    returns = equity.pct_change().dropna()
    years = max(1 / 365.25, (equity.index[-1] - equity.index[0]).days / 365.25)
    cagr = (equity.iloc[-1] / capital) ** (1 / years) - 1
    ann_vol = returns.std(ddof=1) * math.sqrt(252) if len(returns) > 1 else 0
    downside_deviation = math.sqrt(float((returns.clip(upper=0) ** 2).mean())) * math.sqrt(252)
    drawdown = equity / equity.cummax() - 1
    monthly = equity.resample("ME").last().pct_change().dropna()
    return {
        "pnl": float(equity.iloc[-1] - capital),
        "annReturn": float(cagr),
        "cagr": float(cagr),
        "annVol": float(ann_vol),
        "maxDrawdown": float(drawdown.min()),
        "sharpe": 0 if ann_vol == 0 else float(returns.mean() * 252 / ann_vol),
        "sortino": 0 if downside_deviation == 0 else float(returns.mean() * 252 / downside_deviation),
        "calmar": 0 if drawdown.min() == 0 else float(cagr / abs(drawdown.min())),
        "monthlyWinRate": float((monthly > 0).mean()) if len(monthly) else 0,
    }


def _points(equity, weights_by_date=None):
    rows = []
    for date, value in equity.items():
        weights = (weights_by_date or {}).get(date, {})
        rows.append(
            {
                "time": int(pd.Timestamp(date).timestamp() * 1000),
                "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                "equity": float(value),
                "weights": weights,
                "cashAllocation": float(max(0, 1 - sum(weights.values()))) if weights else 0,
            }
        )
    return rows


def _benchmark(name, prices, capital, target_weights, monthly_rebalance=False, cost_bps=0):
    symbols = list(prices.columns)
    target = pd.Series(target_weights, dtype=float).reindex(symbols).fillna(0)
    target = target / target.sum()
    weights = target.copy()
    month_ends = set(prices.groupby(prices.index.to_period("M")).tail(1).index)
    equity = capital
    values = []
    for index in range(1, len(prices)):
        today = prices.index[index]
        yesterday = prices.index[index - 1]
        equity *= 1 + float((weights * prices.loc[today].div(prices.loc[yesterday]).sub(1)).sum())
        if monthly_rebalance and today in month_ends:
            traded_notional = float((target - weights).abs().sum())
            equity -= equity * traded_notional * cost_bps / 10000
            weights = target.copy()
        values.append((today, equity))
    series = pd.Series(dict(values), dtype=float)
    return {"name": name, "metrics": _metrics(series, capital), "points": _points(series)}


def _regime_analytics(daily_returns):
    frame = pd.DataFrame(daily_returns)
    rows = []
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame[frame["regime"].isin(REGIME_RULES)]
        for regime, group in frame.groupby("regime"):
            returns = group["return"]
            years = len(returns) / 252
            rows.append(
                {
                    "regime": regime,
                    "days": int(len(group)),
                    "annualizedReturn": float((1 + returns).prod() ** (1 / max(years, 1 / 252)) - 1),
                    "annualizedVol": float(returns.std(ddof=1) * math.sqrt(252)) if len(returns) > 1 else 0,
                    "winRate": float((returns > 0).mean()),
                }
            )
    return rows


def run_backtest(capital=100000, config=None, prices=None, macro=None):
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    symbols = list(dict.fromkeys(cfg["risk_assets"] + cfg["defensive_assets"]))
    if prices is None:
        prices, price_counts, price_coverage = _load_prices(symbols, cfg["price_warmup_start"])
    else:
        prices = prices[symbols].sort_index().copy()
        price_counts = {symbol: int(prices[symbol].notna().sum()) for symbol in symbols}
        price_coverage = []
    if macro is None:
        macro, macro_counts, macro_coverage = _load_macro(cfg)
    else:
        macro = macro.copy()
        if not isinstance(macro.index, pd.PeriodIndex):
            macro.index = pd.to_datetime(macro.index).to_period("M")
        macro_counts = {}
        macro_coverage = []

    returns = prices.pct_change()
    ma_200 = prices.rolling(cfg["trend_window"]).mean()
    momentum_6m = prices / prices.shift(cfg["momentum_6m_window"]) - 1
    momentum_12m_skip = prices.shift(cfg["skip_1m_window"]) / prices.shift(cfg["momentum_12m_window"]) - 1
    month_ends = set(prices.groupby(prices.index.to_period("M")).tail(1).index)
    start = max(
        pd.Timestamp(cfg["start_date"]),
        prices.index[cfg["momentum_12m_window"]],
        macro.index.min().to_timestamp("M"),
    )

    weights = pd.Series(0.0, index=symbols)
    equity = capital
    equity_values = []
    portfolio_returns = []
    daily_returns = []
    weights_by_date = {}
    history = []
    total_turnover = 0.0
    total_costs = 0.0
    active_regime = None

    for index in range(1, len(prices)):
        today = prices.index[index]
        if today < start:
            continue
        daily_return = float((weights * returns.loc[today, symbols].fillna(0)).sum())
        equity *= 1 + daily_return
        portfolio_returns.append(daily_return)
        daily_returns.append({"date": today.strftime("%Y-%m-%d"), "return": daily_return, "regime": active_regime or "Unallocated"})

        if today in month_ends and today.to_period("M") in macro.index:
            macro_row = macro.loc[today.to_period("M")]
            regime = str(macro_row["regime"])
            eligible = REGIME_RULES[regime]
            rank_6m = momentum_6m.loc[today, eligible].rank(pct=True)
            rank_12m = momentum_12m_skip.loc[today, eligible].rank(pct=True)
            scores = (0.5 * rank_6m + 0.5 * rank_12m).dropna().sort_values(ascending=False)
            top_n = 2 if regime == "Contraction" else 3
            selected = list(scores.head(top_n).index)
            target = pd.Series(0.0, index=symbols)
            if selected:
                target.loc[selected] = 1 / len(selected)
                below_ma = prices.loc[today, selected] < ma_200.loc[today, selected]
                target.loc[below_ma[below_ma].index] *= 0.5
                target = target.clip(upper=cfg["max_weight"])

            realized_vol = (
                pd.Series(portfolio_returns[-cfg["portfolio_vol_window"] :]).std(ddof=1) * math.sqrt(252)
                if len(portfolio_returns) >= cfg["portfolio_vol_window"]
                else 0
            )
            vol_scale = 1.0
            if realized_vol > cfg["vol_trigger"]:
                vol_scale = min(1.0, cfg["vol_target"] / realized_vol)
                target *= vol_scale

            traded_notional = float((target - weights).abs().sum())
            cost = equity * traded_notional * cfg["transaction_cost_bps"] / 10000
            equity -= cost
            total_turnover += traded_notional
            total_costs += cost
            weights = target
            active_regime = regime
            history.append(
                {
                    "time": int(today.timestamp() * 1000),
                    "date": today.strftime("%Y-%m-%d"),
                    "regime": regime,
                    "stressOverride": bool(macro_row["stressOverride"]),
                    "eligible": eligible,
                    "selected": selected,
                    "weights": {symbol: float(weights[symbol]) for symbol in symbols},
                    "scores": {symbol: float(scores[symbol]) for symbol in scores.index},
                    "momentum6m": {symbol: float(momentum_6m.loc[today, symbol]) for symbol in selected},
                    "momentum12mSkip1m": {symbol: float(momentum_12m_skip.loc[today, symbol]) for symbol in selected},
                    "above200dma": {symbol: bool(prices.loc[today, symbol] >= ma_200.loc[today, symbol]) for symbol in selected},
                    "realizedVol60d": float(realized_vol),
                    "volScale": float(vol_scale),
                    "cashAllocation": float(max(0, 1 - weights.sum())),
                    "turnover": traded_notional,
                }
            )

        weights_by_date[today] = {symbol: float(weights[symbol]) for symbol in symbols if weights[symbol] > 0}
        equity_values.append((today, equity))

    equity_series = pd.Series(dict(equity_values), dtype=float)
    if equity_series.empty:
        raise RuntimeError("No backtest observations were produced after aligning ETF and macro history.")
    metrics = _metrics(equity_series, capital)
    metrics.update(
        {
            "turnover": float(total_turnover),
            "averageMonthlyTurnover": float(total_turnover / max(1, len(history))),
            "averageNumberOfHoldings": float(
                sum(sum(weight > 0 for weight in row["weights"].values()) for row in history) / max(1, len(history))
            ),
            "costs": float(total_costs),
            "rebalanceCount": len(history),
        }
    )

    drawdown = equity_series / equity_series.cummax() - 1
    equity_returns = equity_series.pct_change()
    rolling_sharpe = equity_returns.rolling(252).mean() / equity_returns.rolling(252).std() * math.sqrt(252)
    analytics = {
        "drawdown": [{"date": date.strftime("%Y-%m-%d"), "value": float(value)} for date, value in drawdown.items()],
        "rolling12mSharpe": [
            {"date": date.strftime("%Y-%m-%d"), "value": float(value)}
            for date, value in rolling_sharpe.dropna().items()
        ],
        "regimeReturns": _regime_analytics(daily_returns),
    }
    latest = history[-1] if history else None
    current_holdings = []
    if latest:
        for symbol, weight in latest["weights"].items():
            if weight > 0:
                current_holdings.append(
                    {
                        "symbol": symbol,
                        "weight": weight,
                        "score": latest["scores"].get(symbol),
                        "momentum6m": latest["momentum6m"].get(symbol),
                        "momentum12mSkip1m": latest["momentum12mSkip1m"].get(symbol),
                        "above200dma": latest["above200dma"].get(symbol),
                    }
                )

    benchmark_prices = prices.loc[equity_series.index[0] : equity_series.index[-1], symbols]
    benchmarks = [
        _benchmark("SPY Buy & Hold", benchmark_prices, capital, {"SPY": 1}),
        _benchmark("60/40 SPY/IEF", benchmark_prices, capital, {"SPY": 0.6, "IEF": 0.4}, True, cfg["transaction_cost_bps"]),
        _benchmark(
            "Equal-Weight All ETFs Monthly",
            benchmark_prices,
            capital,
            {symbol: 1 for symbol in symbols},
            True,
            cfg["transaction_cost_bps"],
        ),
    ]
    source_notes = [
        "ETF adjusted closes: Yahoo chart API with durable local JSON cache.",
        "CLI, CPI, and unemployment: FRED CSV with stale-cache fallback.",
        "Yield curve: FRED T10Y3M; Yahoo ^TNX minus ^IRX when FRED export is unavailable.",
        "High-yield OAS: current FRED export merged with a public historical FRED snapshot.",
        "High-yield OAS gaps: HYG three-month adjusted-price return calibrated to overlapping actual OAS observations.",
        "FRED USALOLITONOSTSAM currently ends in January 2024; later regime calculations forward-fill its latest observation.",
    ]
    data_health = {
        "priceCoverage": price_coverage,
        "macroCoverage": macro_coverage,
        "sourceNotes": source_notes,
        "creditSourceMonths": macro["CREDIT_SOURCE"].value_counts().to_dict() if "CREDIT_SOURCE" in macro else {},
    }
    manifest = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "strategy": "Regime-Aware ETF Momentum",
        **data_health,
    }
    manifest_file = os.path.join(_root_folder("data", "regime_aware_etf_momentum"), "data_manifest.json")
    with open(manifest_file, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    return {
        "mode": "streamlit-python-regime-aware-etf-momentum",
        "strategy": "Regime-Aware ETF Momentum",
        "frequency": "Daily adjusted ETF closes, monthly macro regime and rebalance",
        "source": "Yahoo adjusted closes and FRED macro series",
        "symbols": symbols,
        "rows": {
            "aligned": len(prices),
            "start": prices.index[0].strftime("%Y-%m-%d"),
            "end": prices.index[-1].strftime("%Y-%m-%d"),
            "backtestStart": equity_series.index[0].strftime("%Y-%m-%d"),
            "backtestEnd": equity_series.index[-1].strftime("%Y-%m-%d"),
            "bySymbol": price_counts,
            "byMacroSeries": macro_counts,
        },
        "assumptions": cfg,
        "metrics": metrics,
        "benchmarks": benchmarks,
        "benchmark": benchmarks[1],
        "latestRegime": latest,
        "latestWeights": latest["weights"] if latest else {},
        "currentHoldings": current_holdings,
        "weightHistory": history,
        "analytics": analytics,
        "dataHealth": data_health,
        "points": _points(equity_series, weights_by_date),
    }
