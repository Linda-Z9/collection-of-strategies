import json
import math
import os
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_CONFIG = {
    "start_date": "2021-01-01",
    "end_date": None,
    "top_n": 3,
    "rebalance": "W-MON",
    "max_weight_per_coin": 0.40,
    "risk_on_exposure": 1.00,
    "risk_off_exposure": 0.40,
    "trading_cost": 0.002,
    "use_btc_regime": True,
    "volume_threshold": 20_000_000,
    "coinbase_validation": True,
    "primary_provider": "coingecko",
    "fallback_provider": "auto",
    "force_download": False,
}

UNIVERSE = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "LINK": "chainlink",
    "AVAX": "avalanche-2",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
}

COINBASE_PRODUCTS = {symbol: f"{symbol}-USD" for symbol in UNIVERSE}

COINPAPRIKA_IDS = {
    "BTC": "btc-bitcoin",
    "ETH": "eth-ethereum",
    "SOL": "sol-solana",
    "XRP": "xrp-xrp",
    "ADA": "ada-cardano",
    "DOGE": "doge-dogecoin",
    "LINK": "link-chainlink",
    "AVAX": "avax-avalanche",
    "LTC": "ltc-litecoin",
    "BCH": "bch-bitcoin-cash",
}


def _folder(*parts):
    path = os.path.join(ROOT, "data", "crypto_trend_rotation", *parts)
    os.makedirs(path, exist_ok=True)
    return path


def _request_json(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        )
    }
    api_key = os.environ.get("COINGECKO_API_KEY")
    if api_key and "coingecko" in url:
        headers["x-cg-demo-api-key"] = api_key
        headers["x-cg-pro-api-key"] = api_key
    paprika_key = os.environ.get("COINPAPRIKA_API_KEY")
    if paprika_key and "coinpaprika" in url:
        headers["Authorization"] = paprika_key
    request = Request(
        url,
        headers=headers,
    )
    with urlopen(request, timeout=40) as response:
        return json.loads(response.read().decode("utf-8"))


def _date_to_timestamp(value):
    return int(pd.Timestamp(value, tz="UTC").timestamp())


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _clean_market_chart_rows(symbol, coin_id, payload):
    prices = pd.DataFrame(payload.get("prices") or [], columns=["time", "close"])
    market_caps = pd.DataFrame(payload.get("market_caps") or [], columns=["time", "market_cap"])
    volumes = pd.DataFrame(payload.get("total_volumes") or [], columns=["time", "volume"])
    if prices.empty:
        raise RuntimeError(f"CoinGecko returned no price rows for {symbol}.")
    frame = prices.merge(market_caps, on="time", how="left").merge(volumes, on="time", how="left")
    frame["date"] = pd.to_datetime(frame["time"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    frame = frame.drop(columns=["time"]).groupby("date", as_index=False).last()
    frame.insert(1, "symbol", symbol)
    frame.insert(2, "coin_id", coin_id)
    frame["source"] = "coingecko"
    return frame[["date", "symbol", "coin_id", "close", "market_cap", "volume", "source"]]


def fetch_coingecko(symbol, coin_id, start_date, end_date, force_download=False):
    raw_file = os.path.join(_folder("raw"), f"{symbol}_coingecko.csv")
    if os.path.exists(raw_file) and not force_download:
        return pd.read_csv(raw_file)

    params = urlencode(
        {
            "vs_currency": "usd",
            "from": _date_to_timestamp(start_date),
            "to": _date_to_timestamp(end_date) + 86399,
        }
    )
    base_url = os.environ.get("COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3").rstrip("/")
    url = f"{base_url}/coins/{quote(coin_id)}/market_chart/range?{params}"
    payload = _request_json(url)
    frame = _clean_market_chart_rows(symbol, coin_id, payload)
    frame.to_csv(raw_file, index=False)
    return frame


def _clean_coinpaprika_ticker_rows(symbol, coin_id, payload):
    frame = pd.DataFrame(payload or [])
    if frame.empty:
        raise RuntimeError(f"CoinPaprika returned no ticker-history rows for {symbol}.")
    if "timestamp" not in frame.columns or "price" not in frame.columns:
        raise RuntimeError(f"CoinPaprika ticker-history response for {symbol} did not include timestamp and price.")
    frame["date"] = pd.to_datetime(frame["timestamp"], utc=True).dt.strftime("%Y-%m-%d")
    frame["close"] = pd.to_numeric(frame["price"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame.get("volume_24h"), errors="coerce")
    frame["market_cap"] = pd.to_numeric(frame.get("market_cap"), errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).groupby("date", as_index=False).last()
    frame.insert(1, "symbol", symbol)
    frame.insert(2, "coin_id", coin_id)
    frame["source"] = "coinpaprika_ticker_history"
    return frame[["date", "symbol", "coin_id", "close", "market_cap", "volume", "source"]]


def fetch_coinpaprika(symbol, coin_id, start_date, end_date, force_download=False):
    raw_file = os.path.join(_folder("raw"), f"{symbol}_coinpaprika.csv")
    if os.path.exists(raw_file) and not force_download:
        return pd.read_csv(raw_file)

    params = urlencode({"start": start_date, "end": end_date, "interval": "1d", "quote": "usd"})
    base_url = os.environ.get("COINPAPRIKA_BASE_URL", "https://api.coinpaprika.com/v1").rstrip("/")
    url = f"{base_url}/tickers/{quote(coin_id)}/historical?{params}"
    try:
        payload = _request_json(url)
    except HTTPError as error:
        if error.code != 402:
            raise
        limited_start = max(pd.Timestamp(start_date), pd.Timestamp(end_date) - pd.Timedelta(days=365))
        limited_params = urlencode(
            {
                "start": limited_start.strftime("%Y-%m-%d"),
                "end": end_date,
                "interval": "1d",
                "quote": "usd",
            }
        )
        limited_url = f"{base_url}/tickers/{quote(coin_id)}/historical?{limited_params}"
        payload = _request_json(limited_url)
    frame = _clean_coinpaprika_ticker_rows(symbol, coin_id, payload)
    frame.to_csv(raw_file, index=False)
    return frame


def fetch_coinbase(symbol, product_id, start_date, end_date, force_download=False):
    raw_file = os.path.join(_folder("raw"), f"{symbol}_coinbase.csv")
    if os.path.exists(raw_file) and not force_download:
        return pd.read_csv(raw_file)

    rows = []
    chunks = pd.date_range(pd.Timestamp(start_date), pd.Timestamp(end_date) + pd.Timedelta(days=1), freq="250D")
    if chunks[-1] < pd.Timestamp(end_date) + pd.Timedelta(days=1):
        chunks = chunks.append(pd.DatetimeIndex([pd.Timestamp(end_date) + pd.Timedelta(days=1)]))

    for start, stop in zip(chunks[:-1], chunks[1:]):
        params = urlencode(
            {
                "granularity": 86400,
                "start": start.strftime("%Y-%m-%dT00:00:00Z"),
                "end": stop.strftime("%Y-%m-%dT00:00:00Z"),
            }
        )
        url = f"https://api.exchange.coinbase.com/products/{quote(product_id)}/candles?{params}"
        payload = _request_json(url)
        rows.extend(payload or [])

    if not rows:
        raise RuntimeError(f"Coinbase returned no candle rows for {product_id}.")

    frame = pd.DataFrame(rows, columns=["time", "low", "high", "open", "coinbase_close", "coinbase_volume"])
    frame["date"] = pd.to_datetime(frame["time"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    frame = frame.drop(columns=["time"]).groupby("date", as_index=False).last().sort_values("date")
    frame.to_csv(raw_file, index=False)
    return frame


def fetch_coinbase_panel(symbol, product_id, start_date, end_date, force_download=False):
    frame = fetch_coinbase(symbol, product_id, start_date, end_date, force_download)
    panel = frame.rename(columns={"coinbase_close": "close", "coinbase_volume": "volume"}).copy()
    panel.insert(1, "symbol", symbol)
    panel.insert(2, "coin_id", product_id)
    panel["market_cap"] = np.nan
    panel["source"] = "coinbase_daily_candles"
    panel["coinbase_close"] = panel["close"]
    panel["coinbase_volume"] = panel["volume"]
    return panel[["date", "symbol", "coin_id", "close", "market_cap", "volume", "source", "open", "high", "low", "coinbase_close", "coinbase_volume"]]


def load_panel(config):
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    end_date = cfg["end_date"] or _today()
    frames = []
    coinbase_frames = {}
    errors = []

    for symbol, coin_id in UNIVERSE.items():
        frame = None
        provider_attempts = []
        if cfg.get("primary_provider") == "coinbase":
            provider_attempts = ["coinbase", "coingecko", "coinpaprika"]
        elif cfg.get("primary_provider") == "coinpaprika":
            provider_attempts = ["coinpaprika", "coingecko", "coinbase"]
        else:
            provider_attempts = ["coingecko", "coinpaprika", "coinbase"]
        if not cfg.get("fallback_provider"):
            provider_attempts = provider_attempts[:1]

        for provider in provider_attempts:
            try:
                if provider == "coinpaprika":
                    frame = fetch_coinpaprika(
                        symbol,
                        COINPAPRIKA_IDS[symbol],
                        cfg["start_date"],
                        end_date,
                        cfg["force_download"],
                    )
                elif provider == "coinbase":
                    frame = fetch_coinbase_panel(
                        symbol,
                        COINBASE_PRODUCTS[symbol],
                        cfg["start_date"],
                        end_date,
                        cfg["force_download"],
                    )
                else:
                    frame = fetch_coingecko(symbol, coin_id, cfg["start_date"], end_date, cfg["force_download"])
                break
            except (HTTPError, URLError, RuntimeError, TimeoutError) as error:
                errors.append(f"{symbol} {provider}: {error}")

        if frame is None:
            continue

        if cfg["coinbase_validation"] and not str(frame.get("source", pd.Series([""])).iloc[0]).startswith("coinbase"):
            try:
                coinbase = fetch_coinbase(symbol, COINBASE_PRODUCTS[symbol], cfg["start_date"], end_date, cfg["force_download"])
                coinbase_frames[symbol] = coinbase
                frame = frame.merge(coinbase, on="date", how="left")
            except (HTTPError, URLError, RuntimeError, TimeoutError) as error:
                errors.append(f"{symbol} Coinbase validation: {error}")

        frames.append(frame)

    if not frames:
        detail = "; ".join(errors[:6])
        suffix = f" Details: {detail}" if detail else ""
        raise RuntimeError(f"No crypto data available. CoinGecko/CoinPaprika downloads failed and no usable cache was found.{suffix}")

    panel = pd.concat(frames, ignore_index=True)
    panel["date"] = pd.to_datetime(panel["date"])
    panel = panel.sort_values(["symbol", "date"]).reset_index(drop=True)

    processed = _folder("processed")
    panel.to_csv(os.path.join(processed, "crypto_daily_panel.csv"), index=False)
    try:
        panel.to_parquet(os.path.join(processed, "crypto_daily_panel.parquet"), index=False)
    except Exception:
        pass

    return panel, data_health_report(panel, errors)


def data_health_report(panel, errors=None):
    rows = []
    for symbol, frame in panel.groupby("symbol"):
        frame = frame.sort_values("date")
        expected = pd.date_range(frame["date"].min(), frame["date"].max(), freq="D")
        observed = pd.DatetimeIndex(frame["date"])
        missing = len(expected.difference(observed))
        diff = None
        if "coinbase_close" in frame.columns:
            valid = frame.dropna(subset=["close", "coinbase_close"])
            if not valid.empty:
                diff = float(((valid["close"] / valid["coinbase_close"]) - 1).abs().median())
        rows.append(
            {
                "symbol": symbol,
                "source": ", ".join(sorted(frame["source"].dropna().astype(str).unique())),
                "start_date": frame["date"].min().strftime("%Y-%m-%d"),
                "end_date": frame["date"].max().strftime("%Y-%m-%d"),
                "observations": int(len(frame)),
                "missing_days": int(missing),
                "coinbase_close_median_abs_diff": diff,
            }
        )
    health = pd.DataFrame(rows).sort_values("symbol")
    health.to_csv(os.path.join(_folder("results"), "data_health_report.csv"), index=False)
    return {"rows": rows, "errors": errors or []}


def add_features(panel, volume_threshold):
    panel = panel.sort_values(["symbol", "date"]).copy()
    parts = []
    for symbol, frame in panel.groupby("symbol"):
        frame = frame.set_index("date").asfreq("D")
        frame[["symbol", "coin_id", "source"]] = frame[["symbol", "coin_id", "source"]].ffill(limit=1)
        frame[["close", "market_cap", "volume"]] = frame[["close", "market_cap", "volume"]].ffill(limit=1)
        frame["symbol"] = symbol
        frame["ret_1d"] = frame["close"].pct_change()
        frame["mom_20"] = frame["close"] / frame["close"].shift(20) - 1
        frame["mom_60"] = frame["close"] / frame["close"].shift(60) - 1
        frame["mom_120"] = frame["close"] / frame["close"].shift(120) - 1
        frame["sma_200"] = frame["close"].rolling(200).mean()
        frame["trend_ok"] = frame["close"] > frame["sma_200"]
        frame["vol_20"] = frame["ret_1d"].rolling(20).std() * math.sqrt(365)
        frame["vol_60"] = frame["ret_1d"].rolling(60).std() * math.sqrt(365)
        frame["dollar_volume"] = frame["close"] * frame["volume"]
        frame["adv_30"] = frame["dollar_volume"].rolling(30).mean()
        frame["volume_ok"] = frame["adv_30"] > volume_threshold
        parts.append(frame.reset_index())

    features = pd.concat(parts, ignore_index=True)
    for column in ["mom_60", "mom_120", "vol_60"]:
        features[f"z_{column}"] = features.groupby("date")[column].transform(_zscore)
    features["score"] = 0.50 * features["z_mom_60"] + 0.30 * features["z_mom_120"] - 0.20 * features["z_vol_60"]
    features["eligible"] = (
        features["trend_ok"]
        & (features["mom_60"] > 0)
        & (features["mom_120"] > 0)
        & features["volume_ok"]
        & features["vol_20"].notna()
        & features["vol_60"].notna()
    )
    return features.dropna(subset=["sma_200"])


def _zscore(series):
    std = series.std(ddof=0)
    if not std or np.isnan(std):
        return pd.Series(0, index=series.index)
    return (series - series.mean()) / std


def _capped_inverse_vol_weights(selected, max_weight):
    weights = pd.Series(0.0, index=selected["symbol"])
    if selected.empty:
        return weights
    raw = (1 / selected.set_index("symbol")["vol_20"].clip(lower=1e-9)).astype(float)
    weights = raw / raw.sum()
    uncapped = set(weights.index)
    capped = pd.Series(0.0, index=weights.index)
    while uncapped:
        current = weights.loc[list(uncapped)]
        scaled = current / current.sum() * (1 - capped.sum())
        over = scaled[scaled > max_weight].index
        if len(over) == 0:
            capped.loc[scaled.index] = scaled
            break
        capped.loc[over] = max_weight
        uncapped -= set(over)
    return capped


def _rebalance_dates(index, rule):
    dates = pd.DatetimeIndex(index).sort_values()
    if rule == "M":
        return set(pd.Series(dates, index=dates).groupby(dates.to_period("M")).tail(1).index)
    return set(pd.Series(dates, index=dates).groupby(dates.to_period("W-MON")).tail(1).index)


def run_model(features, capital, config):
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    symbols = list(UNIVERSE)
    prices = features.pivot(index="date", columns="symbol", values="close").sort_index()
    returns = prices.pct_change().fillna(0)
    btc = features[features["symbol"] == "BTC"].set_index("date")
    rebalance_dates = _rebalance_dates(prices.index, "M" if cfg["rebalance"] == "M" else "W-MON")

    weights = pd.Series(0.0, index=symbols)
    equity = capital
    values = []
    daily_returns = []
    weights_by_date = {}
    weight_history = []
    turnover_total = 0.0
    cost_total = 0.0
    regime_on_count = 0

    for index in range(1, len(prices)):
        today = prices.index[index]
        ret = float((weights * returns.loc[today, symbols]).sum())
        cost_ret = 0.0

        if today in rebalance_dates:
            day = features[features["date"] == today].copy()
            eligible = day[day["eligible"]].sort_values("score", ascending=False).head(cfg["top_n"])
            target = pd.Series(0.0, index=symbols)
            if not eligible.empty:
                target.loc[eligible["symbol"]] = _capped_inverse_vol_weights(eligible, cfg["max_weight_per_coin"])
            btc_regime_ok = bool(btc.loc[today, "close"] > btc.loc[today, "sma_200"]) if today in btc.index else False
            exposure_cap = cfg["risk_on_exposure"] if (btc_regime_ok or not cfg["use_btc_regime"]) else cfg["risk_off_exposure"]
            crypto_exposure = float(target.sum())
            if crypto_exposure > exposure_cap and crypto_exposure > 0:
                target *= exposure_cap / crypto_exposure
            turnover = float((target - weights).abs().sum())
            cost_ret = turnover * cfg["trading_cost"]
            turnover_total += turnover
            cost_total += equity * cost_ret
            weights = target
            if btc_regime_ok:
                regime_on_count += 1
            weight_history.append(
                {
                    "time": int(today.timestamp() * 1000),
                    "date": today.strftime("%Y-%m-%d"),
                    "weights": {symbol: float(weights[symbol]) for symbol in symbols},
                    "cashAllocation": float(1 - weights.sum()),
                    "turnover": turnover,
                    "eligible": eligible["symbol"].tolist(),
                    "regime": "risk_on" if btc_regime_ok else "risk_off",
                    "equityCap": float(exposure_cap),
                    "cryptoExposure": float(weights.sum()),
                }
            )

        equity *= 1 + ret - cost_ret
        values.append((today, equity))
        daily_returns.append({"date": today, "strategy": ret - cost_ret})
        weights_by_date[today] = {**{symbol: float(weights[symbol]) for symbol in symbols}, "CASH": float(1 - weights.sum())}

    equity_series = pd.Series(dict(values))
    points = _as_points(equity_series, weights_by_date)
    metrics = summarize_equity(equity_series, capital)
    metrics.update(
        {
            "pnl": float(equity_series.iloc[-1] - capital) if len(equity_series) else 0,
            "turnover": float(turnover_total),
            "avgTurnover": float(turnover_total / max(1, len(weight_history))),
            "costs": float(cost_total),
            "averageNumberOfHoldings": float(np.mean([sum(v > 0 for v in row["weights"].values()) for row in weight_history]))
            if weight_history
            else 0,
            "percentTimeInCash": float(np.mean([point["cashAllocation"] > 0.01 for point in points])) if points else 1,
            "percentTimeBtcRegimeRiskOn": float(regime_on_count / max(1, len(weight_history))),
        }
    )
    return {
        "points": points,
        "metrics": metrics,
        "weightHistory": weight_history,
        "latestRegime": weight_history[-1] if weight_history else None,
        "latestWeights": points[-1]["weights"] if points else {},
        "dailyReturns": daily_returns,
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
                "cashAllocation": float(weights.get("CASH", 0)) if weights else 0,
            }
        )
    return points


def summarize_equity(equity, capital):
    if len(equity) < 2:
        return {"annReturn": 0, "annVol": 0, "maxDrawdown": 0, "sharpe": 0}
    returns = equity.pct_change().dropna()
    years = max(1 / 365, (equity.index[-1] - equity.index[0]).days / 365)
    cagr = (equity.iloc[-1] / capital) ** (1 / years) - 1
    ann_vol = returns.std(ddof=1) * math.sqrt(365) if len(returns) > 1 else 0
    downside = returns[returns < 0]
    downside_vol = math.sqrt((downside.pow(2).sum() / max(1, len(downside) - 1))) * math.sqrt(365)
    drawdown = equity / equity.cummax() - 1
    monthly = equity.resample("ME").last().pct_change().dropna()
    return {
        "annReturn": float(cagr),
        "cagr": float(cagr),
        "annVol": float(ann_vol),
        "maxDrawdown": float(drawdown.min()),
        "sharpe": 0 if ann_vol == 0 else float(cagr / ann_vol),
        "sortino": 0 if downside_vol == 0 else float(cagr / downside_vol),
        "calmar": 0 if drawdown.min() == 0 else float(cagr / abs(drawdown.min())),
        "bestMonth": float(monthly.max()) if len(monthly) else 0,
        "worstMonth": float(monthly.min()) if len(monthly) else 0,
        "monthlyWinRate": float((monthly > 0).mean()) if len(monthly) else 0,
        "averageMonthlyReturn": float(monthly.mean()) if len(monthly) else 0,
    }


def benchmark_from_weights(name, prices, capital, target_weights, rebalance="none", cost=0.0):
    symbols = list(prices.columns)
    weights = pd.Series(target_weights, dtype=float).reindex(symbols).fillna(0)
    if weights.sum() > 0:
        weights /= weights.sum()
    rebalance_dates = _rebalance_dates(prices.index, "M" if rebalance == "M" else "W-MON")
    equity = capital
    values = []
    current = weights.copy()
    for index in range(1, len(prices)):
        today = prices.index[index]
        yesterday = prices.index[index - 1]
        ret = float((current * (prices.loc[today, symbols] / prices.loc[yesterday, symbols] - 1)).sum())
        cost_ret = 0.0
        if rebalance != "none" and today in rebalance_dates:
            turnover = float((weights - current).abs().sum())
            cost_ret = turnover * cost
            current = weights.copy()
        equity *= 1 + ret - cost_ret
        values.append((today, equity))
    equity_series = pd.Series(dict(values))
    return {"name": name, "metrics": summarize_equity(equity_series, capital) | {"pnl": float(equity_series.iloc[-1] - capital)}, "points": _as_points(equity_series)}


def robustness_tests(features, capital, base_config):
    variants = [
        ("A Trend Only", {"top_n": len(UNIVERSE), "use_btc_regime": False}),
        ("B Trend + Momentum", {"top_n": 3, "use_btc_regime": False}),
        ("C Full Model", {}),
        ("D No BTC Regime Cap", {"use_btc_regime": False}),
        ("E Monthly Rebalance", {"rebalance": "M"}),
        ("F Top 5", {"top_n": 5}),
        ("G 50 bps Cost", {"trading_cost": 0.005}),
    ]
    rows = []
    for name, overrides in variants:
        cfg = {**base_config, **overrides}
        variant_features = features.copy()
        if name == "A Trend Only":
            variant_features["eligible"] = variant_features["trend_ok"] & variant_features["volume_ok"] & variant_features["vol_20"].notna()
            variant_features["score"] = 0
        if name == "B Trend + Momentum":
            variant_features["eligible"] = (
                variant_features["trend_ok"]
                & (variant_features["mom_60"] > 0)
                & (variant_features["mom_120"] > 0)
                & variant_features["volume_ok"]
                & variant_features["vol_20"].notna()
            )
        result = run_model(variant_features, capital, cfg)
        rows.append({"variant": name, **result["metrics"]})
    return rows


def save_outputs(result, features, panel):
    results = _folder("results")
    pd.DataFrame(result["points"]).to_csv(os.path.join(results, "equity_curves.csv"), index=False)
    pd.DataFrame(result["dailyReturns"]).to_csv(os.path.join(results, "daily_returns.csv"), index=False)
    pd.DataFrame(result["weightHistory"]).to_csv(os.path.join(results, "weights.csv"), index=False)
    pd.DataFrame([result["metrics"]]).to_csv(os.path.join(results, "metrics.csv"), index=False)
    equity = pd.Series({pd.Timestamp(point["date"]): point["equity"] for point in result["points"]})
    equity.resample("ME").last().pct_change().dropna().rename("monthly_return").to_csv(
        os.path.join(results, "monthly_returns.csv")
    )


def run_backtest(capital=100000, config=None):
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    panel, health = load_panel(cfg)
    features = add_features(panel, cfg["volume_threshold"])
    if features.empty:
        raise RuntimeError("Crypto panel has no rows after the 200-day SMA warmup. Check downloaded data coverage.")

    result = run_model(features, capital, cfg)
    prices = features.pivot(index="date", columns="symbol", values="close").dropna(how="all")
    benchmarks = [
        benchmark_from_weights("BTC Buy & Hold", prices, capital, {"BTC": 1}),
        benchmark_from_weights("ETH Buy & Hold", prices, capital, {"ETH": 1}),
        benchmark_from_weights("50/50 BTC ETH Monthly", prices, capital, {"BTC": 0.5, "ETH": 0.5}, "M", cfg["trading_cost"]),
        benchmark_from_weights(
            "Equal-Weight Universe Weekly",
            prices,
            capital,
            {symbol: 1 for symbol in UNIVERSE},
            "W",
            cfg["trading_cost"],
        ),
        benchmark_from_weights("Cash", prices, capital, {}),
    ]
    robustness = robustness_tests(features, capital, cfg)
    result.update(
        {
            "mode": "streamlit-python-crypto-trend-rotation",
            "strategy": "Crypto Trend Rotation",
            "symbols": list(UNIVERSE),
            "frequency": "Daily spot data, weekly Monday-close rebalance",
            "source": "CoinGecko market_chart/range, CoinPaprika ticker history fallback, Coinbase USD candle validation",
            "rows": {
                "aligned": int(len(features)),
                "start": features["date"].min().strftime("%Y-%m-%d"),
                "end": features["date"].max().strftime("%Y-%m-%d"),
                "backtestStart": result["points"][0]["date"] if result["points"] else None,
                "backtestEnd": result["points"][-1]["date"] if result["points"] else None,
            },
            "assumptions": cfg,
            "benchmarks": benchmarks,
            "benchmark": benchmarks[2],
            "dataHealth": health,
            "latestEligible": result["latestRegime"]["eligible"] if result.get("latestRegime") else [],
            "robustness": robustness,
            "metrics": result["metrics"],
        }
    )
    save_outputs(result, features, panel)
    pd.DataFrame(robustness).to_csv(os.path.join(_folder("results"), "robustness_metrics.csv"), index=False)
    return result


def _print_report(payload):
    print("1. data health summary")
    print(pd.DataFrame(payload.get("dataHealth", {}).get("rows", [])).to_string(index=False))
    for warning in payload.get("dataHealth", {}).get("errors", []):
        print(f"WARNING: {warning}")
    print("\n2. strategy metrics")
    print(pd.DataFrame([payload["metrics"]]).to_string(index=False))
    print("\n3. benchmark metrics")
    print(pd.DataFrame([{"benchmark": b["name"], **b["metrics"]} for b in payload["benchmarks"]]).to_string(index=False))
    print("\n4. current recommended portfolio")
    latest = payload.get("latestWeights") or {}
    print(pd.DataFrame([{"symbol": k, "weight": v} for k, v in latest.items() if v > 0.0001]).to_string(index=False))
    print("\n5. warning if data is missing or stale")
    end = pd.Timestamp(payload["rows"]["end"])
    stale = (pd.Timestamp(_today()) - end).days > 3
    if stale:
        print(f"WARNING: latest data ends on {payload['rows']['end']}.")
    elif payload.get("dataHealth", {}).get("errors"):
        print("WARNING: validation/download issues were reported above.")
    else:
        print("No stale-data warning.")


if __name__ == "__main__":
    _print_report(run_backtest())
