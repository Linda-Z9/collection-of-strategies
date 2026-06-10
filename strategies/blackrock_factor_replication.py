import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from strategies.dynamic_macro_factor_allocation import (
    FACTOR_NAMES,
    _benchmark_risk_parity,
    _project_capped_simplex,
    _risk_contribution,
)
from strategies.regime_aware_etf_momentum import _benchmark, _load_prices, _metrics, _points


DEFAULT_CONFIG = {
    "symbols": ["SPY", "QQQ", "TLT", "IEF", "TIP", "LQD", "HYG", "DBC", "GLD", "SHY"],
    "start_date": "2007-01-01",
    "price_warmup_start": "2003-01-01",
    "factor_lookback_months": 36,
    "target_factors": {"Growth": 0.30, "Duration": 0.25, "Inflation": 0.15, "Credit": 0.20, "Commodity": 0.10},
    "lambda_turnover": 0.25,
    "lambda_concentration": 0.10,
    "max_weight": 0.30,
    "transaction_cost_bps": 5,
    "optimizer_iterations": 600,
}


def _factor_returns(monthly_returns):
    factors = pd.DataFrame(index=monthly_returns.index)
    factors["Growth"] = monthly_returns["SPY"]
    factors["Duration"] = monthly_returns["TLT"]
    factors["Inflation"] = monthly_returns["TIP"]
    factors["Credit"] = monthly_returns["HYG"] - monthly_returns["IEF"]
    factors["Commodity"] = monthly_returns["DBC"]
    return factors


def _rolling_exposures(monthly_returns, lookback):
    factors = _factor_returns(monthly_returns)
    results = {}
    for index in range(lookback, len(monthly_returns)):
        period = monthly_returns.index[index].to_period("M")
        x = factors.iloc[index - lookback : index].dropna()
        if len(x) < lookback:
            continue
        design = np.column_stack([np.ones(len(x)), x[FACTOR_NAMES].values])
        matrix = pd.DataFrame(index=monthly_returns.columns, columns=FACTOR_NAMES, dtype=float)
        for symbol in monthly_returns.columns:
            y = monthly_returns.loc[x.index, symbol]
            valid = y.notna()
            if valid.sum() == lookback:
                matrix.loc[symbol] = np.linalg.lstsq(design[valid], y[valid].values, rcond=None)[0][1:]
        results[period] = matrix.fillna(0)
    return results


def _optimize(exposures, previous, config):
    symbols = config["symbols"]
    b = exposures.reindex(index=symbols, columns=FACTOR_NAMES).fillna(0).values
    target = pd.Series(config["target_factors"]).reindex(FACTOR_NAMES).values
    prior = previous.reindex(symbols).fillna(0).values
    weights = prior.copy()
    if weights.sum() <= 0:
        weights[symbols.index("SHY")] = 1
    lipschitz = (
        2 * np.linalg.norm(b @ b.T, ord=2)
        + 2 * config["lambda_turnover"]
        + 2 * config["lambda_concentration"]
        + 1
    )
    step = 1 / max(lipschitz, 1)
    for _ in range(config["optimizer_iterations"]):
        gradient = (
            2 * b @ (b.T @ weights - target)
            + 2 * config["lambda_turnover"] * (weights - prior)
            + 2 * config["lambda_concentration"] * weights
        )
        updated = _project_capped_simplex(weights - step * gradient, 1.0, config["max_weight"])
        if np.max(np.abs(updated - weights)) < 1e-9:
            weights = updated
            break
        weights = updated
    return pd.Series(weights, index=symbols)


def _save_outputs(result):
    folder = os.path.join(ROOT, "data", "blackrock_factor_replication")
    os.makedirs(folder, exist_ok=True)
    pd.DataFrame(result["points"]).to_csv(os.path.join(folder, "equity_curve.csv"), index=False)
    pd.DataFrame(result["analytics"]["drawdown"]).to_csv(os.path.join(folder, "drawdown.csv"), index=False)
    pd.DataFrame(result["analytics"]["monthlyReturns"]).to_csv(os.path.join(folder, "monthly_returns.csv"), index=False)
    pd.DataFrame(result["analytics"]["factorExposureHistory"]).to_csv(
        os.path.join(folder, "factor_exposure_history.csv"), index=False
    )
    pd.DataFrame(result["weightHistory"]).to_csv(os.path.join(folder, "weights.csv"), index=False)
    pd.DataFrame(result["analytics"]["turnoverHistory"]).to_csv(os.path.join(folder, "turnover.csv"), index=False)
    pd.DataFrame(result["riskContribution"]).to_csv(os.path.join(folder, "risk_contribution.csv"), index=False)
    pd.DataFrame(result["latestFactorMatrix"]).to_csv(os.path.join(folder, "latest_factor_matrix.csv"), index=False)
    pd.DataFrame(result["performanceTable"]).to_csv(os.path.join(folder, "performance_table.csv"), index=False)
    with open(os.path.join(folder, "latest_result_summary.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "rows": result["rows"],
                "metrics": result["metrics"],
                "latestWeights": result["latestWeights"],
                "latestFactorExposure": result["latestFactorExposure"],
                "dataLimitations": result["dataLimitations"],
            },
            handle,
            indent=2,
        )


def run_backtest(capital=100000, config=None):
    config = {**DEFAULT_CONFIG, **(config or {})}
    symbols = config["symbols"]
    prices, price_counts, price_coverage = _load_prices(symbols, config["price_warmup_start"])
    returns = prices.pct_change().fillna(0)
    monthly_prices = prices.groupby(prices.index.to_period("M")).tail(1)
    exposures_by_month = _rolling_exposures(monthly_prices.pct_change(), config["factor_lookback_months"])
    if not exposures_by_month:
        raise RuntimeError("No 36-month factor exposure matrices were produced.")

    month_ends = set(monthly_prices.index)
    weights = pd.Series(0.0, index=symbols)
    weights["SHY"] = 1.0
    equity = capital
    values = []
    weights_by_date = {}
    weight_history = []
    factor_history = []
    turnover_history = []
    total_turnover = 0.0
    costs = 0.0

    for index in range(1, len(prices)):
        today = prices.index[index]
        if today < pd.Timestamp(config["start_date"]):
            continue
        equity *= 1 + float((weights * returns.loc[today, symbols]).sum())
        period = today.to_period("M")
        if today in month_ends and period in exposures_by_month:
            matrix = exposures_by_month[period]
            target = _optimize(matrix, weights, config)
            turnover = float((target - weights).abs().sum())
            cost = equity * turnover * config["transaction_cost_bps"] / 10000
            equity -= cost
            total_turnover += turnover
            costs += cost
            weights = target
            factor_exposure = matrix.T @ weights
            weight_history.append(
                {
                    "date": today.strftime("%Y-%m-%d"),
                    "turnover": turnover,
                    "weights": {symbol: float(weights[symbol]) for symbol in symbols},
                }
            )
            factor_history.append(
                {"date": today.strftime("%Y-%m-%d"), **{factor: float(factor_exposure[factor]) for factor in FACTOR_NAMES}}
            )
            turnover_history.append({"date": today.strftime("%Y-%m-%d"), "turnover": turnover})
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
                np.mean([sum(weight > 0.001 for weight in row["weights"].values()) for row in weight_history])
            ),
        }
    )
    latest_weights = pd.Series(weight_history[-1]["weights"])
    drawdown = equity_series / equity_series.cummax() - 1
    monthly = equity_series.resample("ME").last().pct_change().dropna()
    latest_period = max(exposures_by_month)
    latest_matrix = exposures_by_month[latest_period]
    benchmarks = [
        _benchmark("SPY Buy & Hold", prices.loc[equity_series.index[0] :], capital, {"SPY": 1}),
        _benchmark(
            "60/40 SPY/IEF",
            prices.loc[equity_series.index[0] :],
            capital,
            {"SPY": 0.6, "IEF": 0.4},
            True,
            config["transaction_cost_bps"],
        ),
        _benchmark_risk_parity(prices.loc[equity_series.index[0] :], capital, {**config, "symbols": symbols}),
    ]
    performance_table = [
        {"series": "BlackRock Factor Replication Portfolio", **metrics},
        *[{"series": benchmark["name"], **benchmark["metrics"]} for benchmark in benchmarks],
    ]
    data_limitations = [
        "The first valid 36-month factor matrix is May 2010 because HYG begins in April 2007; the portfolio remains in SHY before then.",
        "ETF proxy factors are correlated, so regression betas and optimized weights can be unstable.",
        "Factor targets are research assumptions expressed against raw ETF regression betas, not normalized institutional factor units.",
        "The NumPy projected-gradient optimizer approximates the constrained quadratic problem without a dedicated QP solver.",
        "Yahoo adjusted-close histories may be revised and include fund-specific tracking differences and fees.",
    ]
    result = {
        "mode": "python-blackrock-factor-replication",
        "strategy": "BlackRock Factor Replication Portfolio",
        "frequency": "Monthly rebalance using rolling 36-month ETF factor regressions",
        "source": "Yahoo adjusted ETF closes",
        "assumptions": config,
        "rows": {
            "aligned": len(prices),
            "start": prices.index[0].strftime("%Y-%m-%d"),
            "end": prices.index[-1].strftime("%Y-%m-%d"),
            "backtestStart": equity_series.index[0].strftime("%Y-%m-%d"),
            "backtestEnd": equity_series.index[-1].strftime("%Y-%m-%d"),
            "firstFactorRebalance": weight_history[0]["date"],
            "bySymbol": price_counts,
        },
        "metrics": metrics,
        "points": _points(equity_series, weights_by_date),
        "latestWeights": {symbol: float(weight) for symbol, weight in latest_weights.items()},
        "currentRecommendedAllocation": {symbol: float(weight) for symbol, weight in latest_weights.items()},
        "latestFactorExposure": factor_history[-1],
        "latestFactorMatrix": [
            {"symbol": symbol, **{factor: float(row[factor]) for factor in FACTOR_NAMES}}
            for symbol, row in latest_matrix.iterrows()
        ],
        "riskContribution": _risk_contribution(latest_weights, returns.loc[equity_series.index]),
        "weightHistory": weight_history,
        "analytics": {
            "drawdown": [{"date": date.strftime("%Y-%m-%d"), "value": float(value)} for date, value in drawdown.items()],
            "monthlyReturns": [
                {"year": int(date.year), "month": int(date.month), "return": float(value)}
                for date, value in monthly.items()
            ],
            "factorExposureHistory": factor_history,
            "turnoverHistory": turnover_history,
        },
        "benchmarks": benchmarks,
        "performanceTable": performance_table,
        "dataHealth": {"priceCoverage": price_coverage},
        "dataLimitations": data_limitations,
    }
    _save_outputs(result)
    return result


if __name__ == "__main__":
    payload = run_backtest()
    print(pd.DataFrame(payload["performanceTable"]).to_string(index=False))
    print("\nCurrent allocation")
    print(pd.Series(payload["latestWeights"]).sort_values(ascending=False).to_string())
