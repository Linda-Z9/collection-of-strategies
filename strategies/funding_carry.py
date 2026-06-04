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


def run_backtest(fetch_json, capital=100000, lookback=365, symbol="BTCUSDT"):
    return fetch_json(
        "/api/funding-carry/backtest",
        {"symbol": symbol, "days": lookback, "capital": capital},
    )


def run_live(fetch_json, symbol="BTCUSDT"):
    return fetch_json("/api/funding-carry/live", {"symbol": symbol})
