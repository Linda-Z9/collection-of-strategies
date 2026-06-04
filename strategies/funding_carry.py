import csv
import math
import os


METADATA = {
    "id": "funding-carry",
    "name": "Funding Carry",
    "group": "Crypto",
    "type": "Spot + perpetual hedge",
    "difficulty": "Medium",
    "data_burden": "Light",
    "runtime": "Minutes to 20 min",
    "capital_need": "Medium",
    "best_role": "Core carry sleeve",
    "base_return": 0.165,
    "base_vol": 0.112,
    "tail_risk": 42,
    "carry_bias": 0.72,
    "public": True,
    "description": (
        "Long spot and short BTC perpetuals when funding is meaningfully positive, then collect the funding spread "
        "after fees, slippage, and financing."
    ),
    "does": (
        "It opens a market-neutral crypto carry sleeve: long spot BTC, short BTC perpetuals, and earns positive "
        "funding when perp longs pay shorts. The backtest models funding income, hedge residuals, fees, and entry or "
        "exit thresholds."
    ),
    "signals": [
        "Funding rate percentile and annualized net funding after fees",
        "Mark-index premium, spot-perp basis, and recent funding reversals",
        "Open interest expansion as a crowding filter",
        "Depth and spread checks before opening both legs",
    ],
    "checklist": [
        "Start with one perpetual venue and one spot venue.",
        "Load 12 to 24 months of funding, mark price, spot candles, fees, and open interest.",
        "Model net carry as funding minus fees, slippage, financing, and missed fills.",
        "Paper trade and reconcile expected versus actual funding.",
    ],
    "risks": [
        "Leg execution mismatch can create real directional BTC exposure.",
        "Funding can mean-revert faster than the hedge can be unwound.",
        "Liquidation and exchange credit risk remain even when delta is near zero.",
    ],
    "sources": ["Binance or Deribit funding", "Coinbase or Kraken spot candles", "Perpetual futures pricing research"],
}


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "data", "funding_carry")


def _number(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        text = str(value).strip().lower()
        if text == "true":
            return True
        if text == "false":
            return False
        return value
    return numeric


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [{key: _number(value) for key, value in row.items()} for row in csv.DictReader(handle)]


def _summarize(points, capital):
    if len(points) < 2:
        return {"pnl": 0, "annReturn": 0, "annVol": 0, "maxDrawdown": 0, "sharpe": 0}
    returns = []
    peak = points[0]["equity"]
    max_drawdown = 0
    for index, point in enumerate(points):
        equity = point["equity"]
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
        if index:
            returns.append(equity / points[index - 1]["equity"] - 1)
    mean = sum(returns) / max(1, len(returns))
    variance = sum((value - mean) ** 2 for value in returns) / max(1, len(returns) - 1)
    start_time = points[0].get("time")
    end_time = points[-1].get("time")
    days = max(1, (end_time - start_time) / 86400000) if start_time and end_time else max(1, len(points) / 24)
    ann_return = (points[-1]["equity"] / capital) ** (365 / days) - 1
    ann_vol = math.sqrt(variance) * math.sqrt(24 * 365)
    return {
        "pnl": points[-1]["equity"] - capital,
        "annReturn": ann_return,
        "annVol": ann_vol,
        "maxDrawdown": max_drawdown,
        "sharpe": 0 if ann_vol == 0 else ann_return / ann_vol,
    }


def _cached_payload(capital, lookback, symbol):
    points = _read_csv(os.path.join(CACHE_DIR, "backtest_points.csv"))
    if not points:
        return None, "Cached funding carry CSV is missing."

    max_time = max(point["time"] for point in points if point.get("time") is not None)
    cutoff = max_time - lookback * 86400000
    points = [point for point in points if point.get("time") is not None and point["time"] >= cutoff]
    if not points:
        return None, "Cached funding carry CSV has no rows for the selected lookback."

    base_equity = points[0]["equity"] or capital
    scaled = []
    for point in points:
        item = dict(point)
        item["equity"] = capital * item["equity"] / base_equity
        scaled.append(item)

    metadata = (_read_csv(os.path.join(CACHE_DIR, "backtest_metadata.csv")) or [{}])[0]
    metrics = _summarize(scaled, capital)
    cached_metrics = (_read_csv(os.path.join(CACHE_DIR, "backtest_metrics.csv")) or [{}])[0]
    for key in ["trades", "fundingIncome", "hedgePnl", "costs"]:
        if key in cached_metrics:
            metrics[key] = cached_metrics[key]

    return {
        "mode": f"{metadata.get('mode') or 'cached-public-funding'}-csv-cache",
        "symbol": symbol,
        "days": min(lookback, int(metadata.get("days") or lookback)),
        "frequency": metadata.get("frequency") or "Cached public funding backtest CSV",
        "rows": {"points": len(scaled), "cachedDays": metadata.get("days")},
        "metrics": metrics,
        "points": scaled,
        "source": "data/funding_carry/backtest_points.csv",
    }, None


def run_backtest(fetch_json, capital=100000, lookback=365, symbol="BTCUSDT"):
    if os.environ.get("COLLECTION_STRATEGY_CACHE_ONLY") != "1" and callable(fetch_json):
        payload, error = fetch_json(
            "/api/funding-carry/backtest",
            {"symbol": symbol, "days": lookback, "capital": capital},
        )
        if payload:
            return payload, None

    return _cached_payload(capital, lookback, symbol)


def run_live(fetch_json, symbol="BTCUSDT"):
    return fetch_json("/api/funding-carry/live", {"symbol": symbol})
