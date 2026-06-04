METADATA = {
    "id": "basis-carry",
    "name": "Futures Basis",
    "group": "Crypto",
    "type": "Spot + dated futures",
    "difficulty": "Medium",
    "data_burden": "Light",
    "runtime": "Few minutes",
    "capital_need": "Medium-high",
    "best_role": "Low frequency carry",
    "base_return": 0.118,
    "base_vol": 0.088,
    "tail_risk": 36,
    "carry_bias": 0.64,
    "public": True,
    "description": (
        "Buy spot and sell rich dated futures when annualized basis exceeds financing, fees, margin drag, and "
        "execution costs."
    ),
    "does": (
        "It buys spot BTC and sells a rich dated futures contract, aiming to capture convergence between futures and "
        "spot by expiry. The public route tracks annualized basis, days to expiry, and the modeled cost of holding "
        "and rolling the spread."
    ),
    "signals": [
        "Annualized front-month and second-month net basis",
        "Curve slope between spot, near future, and next future",
        "Volume and open-interest minimums by expiry",
        "Roll cost and margin utilization before trade entry",
    ],
    "checklist": [
        "Build a spot, near-month, next-month basis curve.",
        "Normalize every opportunity as annualized net basis.",
        "Backtest expiry-hold before dynamic roll rules.",
        "Trade only liquid expiries at first.",
    ],
    "risks": [
        "Margin pressure can arrive before expiry convergence is realized.",
        "Illiquid expiries can produce fake historical edge.",
        "Financing cost and collateral haircuts can move against the trade.",
    ],
    "sources": ["Deribit dated futures", "Coinbase BTC-USD spot candles", "CME or vendor futures data later"],
}


def run_backtest(fetch_json, capital=100000, lookback=365):
    return fetch_json(
        "/api/futures-basis/backtest",
        {"days": lookback, "capital": capital},
    )


def run_live(fetch_json):
    return fetch_json("/api/futures-basis/live")
