import json
import math
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from strategies.dynamic_macro_factor_allocation import _benchmark_risk_parity
from strategies.regime_aware_etf_momentum import _benchmark, _load_macro, _load_prices, _metrics, _points


DEFAULT_CONFIG = {
    "symbols": ["SPY", "QQQ", "HYG", "LQD", "TLT", "SHY", "DBC", "GLD"],
    "benchmark_symbols": ["IEF"],
    "start_date": "2007-01-01",
    "price_warmup_start": "2005-01-01",
    "transaction_cost_bps": 5,
}

REGIME_WEIGHTS = {
    "Recovery": {"SPY": 0.40, "HYG": 0.30, "LQD": 0.20, "GLD": 0.10},
    "Expansion": {"SPY": 0.60, "QQQ": 0.20, "HYG": 0.10, "GLD": 0.10},
    "Slowdown": {"SPY": 0.25, "TLT": 0.25, "GLD": 0.25, "SHY": 0.25},
    "Contraction": {"TLT": 0.60, "SHY": 0.20, "GLD": 0.20},
}


def _pure_cycle_regimes(macro):
    macro = macro.copy()
    macro["growthLevel"] = np.where(macro["CLI_TREND"] >= 0, "High", "Low")
    macro["growthDirection"] = np.where(macro["CLI_MOMENTUM"] > 0, "Rising", "Falling")
    macro["regime"] = np.select(
        [
            (macro["growthLevel"] == "Low") & (macro["growthDirection"] == "Rising"),
            (macro["growthLevel"] == "High") & (macro["growthDirection"] == "Rising"),
            (macro["growthLevel"] == "High") & (macro["growthDirection"] == "Falling"),
        ],
        ["Recovery", "Expansion", "Slowdown"],
        default="Contraction",
    )
    macro["stressOverride"] = False
    return macro


def _regime_performance(daily_rows):
    frame = pd.DataFrame(daily_rows)
    results = []
    for regime, group in frame.groupby("regime"):
        returns = group["return"]
        years = len(returns) / 252
        results.append(
            {
                "regime": regime,
                "days": int(len(returns)),
                "annualizedReturn": float((1 + returns).prod() ** (1 / max(years, 1 / 252)) - 1),
                "annualizedVol": float(returns.std(ddof=1) * math.sqrt(252)) if len(returns) > 1 else 0,
                "winRate": float((returns > 0).mean()),
            }
        )
    return results


def _run_rotation(prices, macro, capital, config):
    symbols = config["symbols"]
    returns = prices.pct_change().fillna(0)
    month_ends = set(prices.groupby(prices.index.to_period("M")).tail(1).index)
    weights = pd.Series(0.0, index=symbols)
    weights["SHY"] = 1.0
    equity = capital
    values = []
    weights_by_date = {}
    weight_history = []
    regime_timeline = []
    turnover_history = []
    daily_rows = []
    active_regime = None
    total_turnover = 0.0
    costs = 0.0

    for index in range(1, len(prices)):
        today = prices.index[index]
        if today < pd.Timestamp(config["start_date"]):
            continue
        daily_return = float((weights * returns.loc[today, symbols]).sum())
        equity *= 1 + daily_return
        if active_regime:
            daily_rows.append({"date": today.strftime("%Y-%m-%d"), "return": daily_return, "regime": active_regime})

        period = today.to_period("M")
        if today in month_ends and period in macro.index:
            row = macro.loc[period]
            regime = str(row["regime"])
            target = pd.Series(REGIME_WEIGHTS[regime], dtype=float).reindex(symbols).fillna(0)
            unavailable = prices.loc[today, symbols].isna()
            unavailable_weight = float(target.loc[unavailable[unavailable].index].sum())
            target.loc[unavailable[unavailable].index] = 0
            target["SHY"] += unavailable_weight
            turnover = float((target - weights).abs().sum())
            cost = equity * turnover * config["transaction_cost_bps"] / 10000
            equity -= cost
            costs += cost
            total_turnover += turnover
            weights = target
            active_regime = regime
            history = {
                "date": today.strftime("%Y-%m-%d"),
                "regime": regime,
                "growthLevel": row["growthLevel"],
                "growthDirection": row["growthDirection"],
                "cliTrend": float(row["CLI_TREND"]),
                "cliMomentum": float(row["CLI_MOMENTUM"]),
                "unemployment": float(row["UNRATE"]),
                "cpiYoY": float(row["CPI_YOY"]),
                "yieldCurve": float(row["YC"]),
                "turnover": turnover,
                "weights": {symbol: float(weights[symbol]) for symbol in symbols},
            }
            weight_history.append(history)
            turnover_history.append({"date": history["date"], "turnover": turnover})
            regime_timeline.append(
                {
                    "date": history["date"],
                    "regime": regime,
                    "growthLevel": row["growthLevel"],
                    "growthDirection": row["growthDirection"],
                }
            )
        weights_by_date[today] = {symbol: float(weight) for symbol, weight in weights.items() if weight > 0}
        values.append((today, equity))

    equity_series = pd.Series(dict(values), dtype=float)
    metrics = _metrics(equity_series, capital)
    metrics.update(
        {
            "turnover": float(total_turnover),
            "averageMonthlyTurnover": float(total_turnover / max(1, len(weight_history))),
            "costs": float(costs),
            "averageNumberOfHoldings": float(
                np.mean([sum(weight > 0 for weight in row["weights"].values()) for row in weight_history])
            ),
        }
    )
    monthly = equity_series.resample("ME").last().pct_change().dropna()
    drawdown = equity_series / equity_series.cummax() - 1
    return {
        "metrics": metrics,
        "points": _points(equity_series, weights_by_date),
        "latestRegime": weight_history[-1] if weight_history else None,
        "latestWeights": weight_history[-1]["weights"] if weight_history else {},
        "weightHistory": weight_history,
        "analytics": {
            "drawdown": [{"date": date.strftime("%Y-%m-%d"), "value": float(value)} for date, value in drawdown.items()],
            "monthlyReturns": [
                {"year": int(date.year), "month": int(date.month), "return": float(value)}
                for date, value in monthly.items()
            ],
            "regimeTimeline": regime_timeline,
            "turnoverHistory": turnover_history,
            "regimePerformance": _regime_performance(daily_rows),
        },
    }


def _save_outputs(result):
    folder = os.path.join(ROOT, "data", "pure_business_cycle_asset_rotation")
    os.makedirs(folder, exist_ok=True)
    pd.DataFrame(result["points"]).to_csv(os.path.join(folder, "equity_curve.csv"), index=False)
    pd.DataFrame(result["analytics"]["drawdown"]).to_csv(os.path.join(folder, "drawdown.csv"), index=False)
    pd.DataFrame(result["analytics"]["monthlyReturns"]).to_csv(os.path.join(folder, "monthly_returns.csv"), index=False)
    pd.DataFrame(result["analytics"]["regimeTimeline"]).to_csv(os.path.join(folder, "regime_timeline.csv"), index=False)
    pd.DataFrame(result["analytics"]["turnoverHistory"]).to_csv(os.path.join(folder, "turnover.csv"), index=False)
    pd.DataFrame(result["analytics"]["regimePerformance"]).to_csv(
        os.path.join(folder, "regime_performance.csv"), index=False
    )
    pd.DataFrame(result["weightHistory"]).to_csv(os.path.join(folder, "weights.csv"), index=False)
    pd.DataFrame(result["performanceTable"]).to_csv(os.path.join(folder, "performance_table.csv"), index=False)
    with open(os.path.join(folder, "latest_result_summary.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "rows": result["rows"],
                "latestRegime": result["latestRegime"],
                "latestWeights": result["latestWeights"],
                "performanceTable": result["performanceTable"],
                "dataLimitations": result["dataLimitations"],
            },
            handle,
            indent=2,
        )


def run_backtest(capital=100000, config=None):
    config = {**DEFAULT_CONFIG, **(config or {})}
    price_symbols = list(dict.fromkeys(config["symbols"] + config["benchmark_symbols"]))
    all_prices, price_counts, price_coverage = _load_prices(price_symbols, config["price_warmup_start"])
    prices = all_prices[config["symbols"]]
    macro, macro_counts, macro_coverage = _load_macro({"macro_lag_months": 1})
    macro = _pure_cycle_regimes(macro)
    model = _run_rotation(prices, macro, capital, config)
    benchmark_prices = all_prices.loc[pd.Timestamp(model["points"][0]["date"]) :, price_symbols]
    benchmarks = [
        _benchmark("SPY Buy & Hold", benchmark_prices, capital, {"SPY": 1}),
        _benchmark("60/40 SPY/IEF", benchmark_prices, capital, {"SPY": 0.6, "IEF": 0.4}, True, config["transaction_cost_bps"]),
        _benchmark_risk_parity(benchmark_prices, capital, {**config, "symbols": config["symbols"]}),
    ]
    performance_table = [
        {"series": "Pure Business-Cycle Asset Rotation", **model["metrics"]},
        *[{"series": benchmark["name"], **benchmark["metrics"]} for benchmark in benchmarks],
    ]
    data_limitations = [
        "The FRED OECD CLI series USALOLITONOSTSAM currently ends in January 2024; later months forward-fill its last observation, so recent growth level and direction are stale.",
        "FRED macro history is revised data, not point-in-time ALFRED vintages; the historical regime classification may contain revision look-ahead bias.",
        "Macro inputs are lagged one month, but publication timing differs by series and is only approximated.",
        "HYG began trading in April 2007; any prescribed HYG allocation before launch is moved to SHY.",
        "Yahoo adjusted-close data and public FRED exports can be revised or temporarily unavailable; durable local caches are used as fallback.",
        "The paper-inspired allocation mapping is a research interpretation, not a direct replication of a JPM investable index.",
    ]
    result = {
        **model,
        "mode": "python-pure-business-cycle-asset-rotation",
        "strategy": "Pure Business-Cycle Asset Rotation",
        "frequency": "Daily adjusted ETF closes, monthly CLI-only regime classification and rebalance",
        "source": "Yahoo adjusted closes; FRED CLI, unemployment, CPI, and yield curve",
        "assumptions": config,
        "rows": {
            "aligned": len(prices),
            "start": prices.index[0].strftime("%Y-%m-%d"),
            "end": prices.index[-1].strftime("%Y-%m-%d"),
            "backtestStart": model["points"][0]["date"],
            "backtestEnd": model["points"][-1]["date"],
            "bySymbol": price_counts,
            "byMacroSeries": macro_counts,
        },
        "benchmarks": benchmarks,
        "performanceTable": performance_table,
        "dataHealth": {"priceCoverage": price_coverage, "macroCoverage": macro_coverage},
        "dataLimitations": data_limitations,
    }
    _save_outputs(result)
    return result


if __name__ == "__main__":
    payload = run_backtest()
    print(pd.DataFrame(payload["performanceTable"]).to_string(index=False))
    print("\nCurrent regime and allocation")
    print(payload["latestRegime"]["regime"])
    print(pd.Series(payload["latestWeights"]).sort_values(ascending=False).to_string())
    print("\nData limitations")
    for limitation in payload["dataLimitations"]:
        print(f"- {limitation}")
