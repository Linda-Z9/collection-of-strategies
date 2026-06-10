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

from strategies.regime_aware_etf_momentum import _benchmark, _fetch_yahoo_daily, _load_macro, _load_prices, _metrics, _points


DEFAULT_CONFIG = {
    "symbols": ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "TIP", "LQD", "HYG", "DBC", "GLD", "SHY"],
    "equity_symbols": ["SPY", "QQQ", "IWM", "EFA", "EEM"],
    "risky_symbols": ["SPY", "QQQ", "IWM", "EFA", "EEM", "TIP", "LQD", "HYG", "DBC", "GLD"],
    "start_date": "2007-01-01",
    "price_warmup_start": "2003-01-01",
    "factor_lookback_months": 36,
    "lambda_turnover": 0.25,
    "lambda_concentration": 0.10,
    "max_weight": 0.35,
    "target_vol": 0.10,
    "vol_trigger": 0.12,
    "vol_window": 60,
    "hy_spread_change_cap": 1.0,
    "hyg_weight_cap": 0.05,
    "equity_weight_cap": 0.25,
    "drawdown_trigger": -0.12,
    "recovery_ma_window": 100,
    "transaction_cost_bps": 5,
    "optimizer_iterations": 600,
}

FACTOR_NAMES = ["Growth", "Duration", "Inflation", "Credit", "Commodity"]

TARGET_FACTORS = {
    "Recovery": {"Growth": 0.35, "Credit": 0.30, "Inflation": 0.10, "Duration": 0.15, "Commodity": 0.10},
    "Expansion": {"Growth": 0.50, "Credit": 0.20, "Inflation": 0.10, "Duration": 0.05, "Commodity": 0.15},
    "Slowdown": {"Growth": 0.20, "Credit": 0.10, "Inflation": 0.15, "Duration": 0.35, "Commodity": 0.20},
    "Contraction": {"Growth": 0.05, "Credit": 0.00, "Inflation": 0.10, "Duration": 0.55, "Commodity": 0.10},
}

TARGET_CASH = {"Recovery": 0.0, "Expansion": 0.0, "Slowdown": 0.0, "Contraction": 0.20}

RULE_WEIGHTS = {
    "Recovery": {"SPY": 0.25, "QQQ": 0.15, "HYG": 0.20, "LQD": 0.15, "TLT": 0.15, "GLD": 0.10},
    "Expansion": {"SPY": 0.35, "QQQ": 0.20, "IWM": 0.15, "HYG": 0.10, "DBC": 0.10, "GLD": 0.10},
    "Slowdown": {"SPY": 0.20, "QQQ": 0.10, "TLT": 0.25, "IEF": 0.20, "GLD": 0.15, "SHY": 0.10},
    "Contraction": {"TLT": 0.45, "IEF": 0.25, "GLD": 0.15, "SHY": 0.15},
}


def _factor_returns(monthly_returns):
    factors = pd.DataFrame(index=monthly_returns.index)
    factors["Growth"] = monthly_returns["SPY"]
    factors["Duration"] = monthly_returns["TLT"]
    factors["Inflation"] = 0.5 * monthly_returns["TIP"] + 0.5 * monthly_returns["DBC"]
    factors["Credit"] = monthly_returns["HYG"] - monthly_returns["IEF"]
    factors["Commodity"] = monthly_returns["DBC"]
    return factors


def _rolling_factor_exposures(monthly_returns, lookback):
    factors = _factor_returns(monthly_returns)
    exposures = {}
    for index in range(lookback, len(monthly_returns)):
        date = monthly_returns.index[index]
        x = factors.iloc[index - lookback : index].dropna()
        if len(x) < lookback:
            continue
        matrix = np.column_stack([np.ones(len(x)), x[FACTOR_NAMES].values])
        betas = pd.DataFrame(index=monthly_returns.columns, columns=FACTOR_NAMES, dtype=float)
        for symbol in monthly_returns.columns:
            y = monthly_returns.loc[x.index, symbol]
            valid = y.notna()
            if valid.sum() < lookback:
                continue
            coefficients = np.linalg.lstsq(matrix[valid], y[valid].values, rcond=None)[0]
            betas.loc[symbol] = coefficients[1:]
        exposures[date.to_period("M")] = betas.fillna(0)
    return exposures


def _project_capped_simplex(values, total=1.0, cap=0.35):
    values = np.asarray(values, dtype=float)
    total = min(float(total), cap * len(values))
    low = float(values.min() - cap)
    high = float(values.max())
    for _ in range(80):
        middle = (low + high) / 2
        projected = np.clip(values - middle, 0, cap)
        if projected.sum() > total:
            low = middle
        else:
            high = middle
    projected = np.clip(values - high, 0, cap)
    if projected.sum() > 0:
        projected *= total / projected.sum()
    return np.clip(projected, 0, cap)


def _optimizer_weights(exposures, target_factors, previous, symbols, config, target_cash=0.0):
    b = exposures.reindex(index=symbols, columns=FACTOR_NAMES).fillna(0).values
    target = pd.Series(target_factors).reindex(FACTOR_NAMES).fillna(0).values
    previous_values = previous.reindex(symbols).fillna(0).values
    shy_index = symbols.index("SHY")
    weights = previous_values.copy()
    if weights.sum() <= 0:
        weights[shy_index] = 1

    lipschitz = (
        2 * np.linalg.norm(b @ b.T, ord=2)
        + 2 * config["lambda_turnover"]
        + 2 * config["lambda_concentration"]
        + 2
    )
    step = 1 / max(lipschitz, 1)
    for _ in range(config["optimizer_iterations"]):
        factor_error = b.T @ weights - target
        gradient = (
            2 * b @ factor_error
            + 2 * config["lambda_turnover"] * (weights - previous_values)
            + 2 * config["lambda_concentration"] * weights
        )
        gradient[shy_index] += 2 * (weights[shy_index] - target_cash)
        updated = _project_capped_simplex(weights - step * gradient, 1.0, config["max_weight"])
        if np.max(np.abs(updated - weights)) < 1e-9:
            weights = updated
            break
        weights = updated
    return pd.Series(weights, index=symbols)


def _rule_weights(regime, symbols):
    return pd.Series(RULE_WEIGHTS[regime], dtype=float).reindex(symbols).fillna(0)


def _move_to_shy(weights, amount, symbols_to_reduce):
    amount = max(0.0, min(float(amount), float(weights.reindex(symbols_to_reduce).sum())))
    if amount <= 0:
        return weights
    base = float(weights.reindex(symbols_to_reduce).sum())
    if base > 0:
        weights.loc[symbols_to_reduce] *= (base - amount) / base
        weights["SHY"] += amount
    return weights


def _apply_risk_controls(target, macro_row, realized_vol, equity, equity_ma, risk_reduction_active, config):
    target = target.copy()
    controls = []
    if macro_row["CREDIT_CHANGE"] > config["hy_spread_change_cap"] and target["HYG"] > config["hyg_weight_cap"]:
        excess = target["HYG"] - config["hyg_weight_cap"]
        target["HYG"] = config["hyg_weight_cap"]
        target["SHY"] += excess
        controls.append("HYG cap")

    if macro_row["YC"] < 0 and macro_row["CLI_MOMENTUM"] < 0:
        equity_weight = float(target.reindex(config["equity_symbols"]).sum())
        if equity_weight > config["equity_weight_cap"]:
            target = _move_to_shy(target, equity_weight - config["equity_weight_cap"], config["equity_symbols"])
            controls.append("Equity cap")

    if realized_vol > config["vol_trigger"]:
        risky_weight = float(target.reindex(config["risky_symbols"]).sum())
        scale = min(1.0, config["target_vol"] / realized_vol)
        target = _move_to_shy(target, risky_weight * (1 - scale), config["risky_symbols"])
        controls.append("Vol target")

    if risk_reduction_active and equity <= equity_ma:
        risky_weight = float(target.reindex(config["risky_symbols"]).sum())
        target = _move_to_shy(target, risky_weight * 0.5, config["risky_symbols"])
        controls.append("Drawdown recovery")
    return target, controls


def _risk_contribution(weights, returns, window=60):
    covariance = returns.tail(window).cov() * 252
    weights = weights.reindex(covariance.index).fillna(0)
    portfolio_variance = float(weights.values @ covariance.values @ weights.values)
    if portfolio_variance <= 0:
        return []
    marginal = covariance.values @ weights.values
    contribution = weights.values * marginal / portfolio_variance
    return [
        {"symbol": symbol, "weight": float(weights[symbol]), "riskContribution": float(contribution[index])}
        for index, symbol in enumerate(covariance.index)
        if weights[symbol] > 0
    ]


def _series_analytics(equity, weight_history, factor_history, turnover_history, regime_history):
    monthly = equity.resample("ME").last().pct_change().dropna()
    heatmap = [
        {"year": int(date.year), "month": int(date.month), "return": float(value)}
        for date, value in monthly.items()
    ]
    drawdown = equity / equity.cummax() - 1
    return {
        "drawdown": [{"date": date.strftime("%Y-%m-%d"), "value": float(value)} for date, value in drawdown.items()],
        "monthlyReturnsHeatmap": heatmap,
        "regimeTimeline": regime_history,
        "factorExposureHistory": factor_history,
        "weightHistory": weight_history,
        "turnoverHistory": turnover_history,
    }


def _run_version(name, prices, macro, exposures_by_month, capital, config, optimizer):
    symbols = config["symbols"]
    returns = prices.pct_change().fillna(0)
    month_ends = set(prices.groupby(prices.index.to_period("M")).tail(1).index)
    start = max(pd.Timestamp(config["start_date"]), prices.index[config["factor_lookback_months"] * 21])
    weights = pd.Series(0.0, index=symbols)
    weights["SHY"] = 1.0
    equity = capital
    values = []
    portfolio_returns = []
    weights_by_date = {}
    weight_history = []
    factor_history = []
    turnover_history = []
    regime_history = []
    costs = 0.0
    turnover_total = 0.0
    peak = capital
    risk_reduction_active = False

    for index in range(1, len(prices)):
        today = prices.index[index]
        if today < start:
            continue
        daily_return = float((weights * returns.loc[today, symbols]).sum())
        equity *= 1 + daily_return
        portfolio_returns.append(daily_return)
        peak = max(peak, equity)
        if equity / peak - 1 <= config["drawdown_trigger"]:
            risk_reduction_active = True
        equity_ma = pd.Series([value for _, value in values[-config["recovery_ma_window"] :]] + [equity]).mean()
        if risk_reduction_active and equity > equity_ma:
            risk_reduction_active = False

        period = today.to_period("M")
        if today in month_ends and period in macro.index and (not optimizer or period in exposures_by_month):
            macro_row = macro.loc[period]
            regime = str(macro_row["regime"])
            exposures = exposures_by_month.get(period)
            if optimizer:
                target = _optimizer_weights(
                    exposures,
                    TARGET_FACTORS[regime],
                    weights,
                    symbols,
                    config,
                    TARGET_CASH[regime],
                )
            else:
                target = _rule_weights(regime, symbols)
            realized_vol = (
                pd.Series(portfolio_returns[-config["vol_window"] :]).std(ddof=1) * math.sqrt(252)
                if len(portfolio_returns) >= config["vol_window"]
                else 0
            )
            target, controls = _apply_risk_controls(
                target,
                macro_row,
                realized_vol,
                equity,
                equity_ma,
                risk_reduction_active,
                config,
            )
            target = target.clip(lower=0)
            if optimizer:
                target = target.clip(upper=config["max_weight"])
            target["SHY"] += max(0, 1 - target.sum())
            target /= target.sum()
            turnover = float((target - weights).abs().sum())
            cost = equity * turnover * config["transaction_cost_bps"] / 10000
            equity -= cost
            costs += cost
            turnover_total += turnover
            weights = target
            factor_exposure = exposures.T @ weights if exposures is not None else None
            row = {
                "date": today.strftime("%Y-%m-%d"),
                "regime": regime,
                "weights": {symbol: float(weights[symbol]) for symbol in symbols},
                "controls": controls,
                "realizedVol60d": float(realized_vol),
                "turnover": turnover,
            }
            weight_history.append(row)
            if factor_exposure is not None:
                factor_history.append(
                    {"date": row["date"], **{factor: float(factor_exposure[factor]) for factor in FACTOR_NAMES}}
                )
            turnover_history.append({"date": row["date"], "turnover": turnover})
            regime_history.append({"date": row["date"], "regime": regime, "stressOverride": bool(macro_row["stressOverride"])})

        weights_by_date[today] = {symbol: float(weight) for symbol, weight in weights.items() if weight > 0}
        values.append((today, equity))

    equity_series = pd.Series(dict(values), dtype=float)
    metrics = _metrics(equity_series, capital)
    metrics.update(
        {
            "turnover": float(turnover_total),
            "averageMonthlyTurnover": float(turnover_total / max(1, len(weight_history))),
            "costs": float(costs),
            "averageNumberOfHoldings": float(
                np.mean([sum(value > 0.001 for value in row["weights"].values()) for row in weight_history])
            )
            if weight_history
            else 0,
        }
    )
    latest_weights = pd.Series(weight_history[-1]["weights"]) if weight_history else weights
    return {
        "name": name,
        "metrics": metrics,
        "points": _points(equity_series, weights_by_date),
        "latestWeights": {symbol: float(weight) for symbol, weight in latest_weights.items()},
        "latestRegime": regime_history[-1] if regime_history else None,
        "latestFactorExposure": factor_history[-1] if factor_history else {},
        "riskContribution": _risk_contribution(latest_weights, returns.loc[equity_series.index]),
        "analytics": _series_analytics(equity_series, weight_history, factor_history, turnover_history, regime_history),
    }


def _benchmark_risk_parity(prices, capital, config):
    symbols = ["SPY", "TLT", "GLD", "DBC"]
    monthly_dates = set(prices.groupby(prices.index.to_period("M")).tail(1).index)
    returns = prices[symbols].pct_change().fillna(0)
    weights = pd.Series(0.25, index=symbols)
    equity = capital
    values = []
    for index in range(1, len(prices)):
        today = prices.index[index]
        equity *= 1 + float((weights * returns.loc[today]).sum())
        if today in monthly_dates and index >= 63:
            vol = returns.iloc[index - 63 : index].std() * math.sqrt(252)
            inverse = 1 / vol.replace(0, np.nan)
            target = (inverse / inverse.sum()).fillna(0.25)
            turnover = float((target - weights).abs().sum())
            equity -= equity * turnover * config["transaction_cost_bps"] / 10000
            weights = target
        values.append((today, equity))
    series = pd.Series(dict(values))
    return {"name": "Risk Parity SPY/TLT/GLD/DBC", "metrics": _metrics(series, capital), "points": _points(series)}


def _store_dgs10_proxy():
    frame = _fetch_yahoo_daily("^TNX")[["date", "close"]].rename(
        columns={"date": "observation_date", "close": "DGS10"}
    )
    folder = os.path.join(ROOT, "data", "fred_cache")
    os.makedirs(folder, exist_ok=True)
    frame.to_csv(os.path.join(folder, "DGS10.csv"), index=False)


def _save_outputs(result):
    folder = os.path.join(ROOT, "data", "dynamic_macro_factor_allocation")
    os.makedirs(folder, exist_ok=True)
    pd.DataFrame(result["performanceTable"]).to_csv(os.path.join(folder, "performance_table.csv"), index=False)
    pd.DataFrame(result["latestFactorMatrix"]).to_csv(os.path.join(folder, "latest_factor_matrix.csv"), index=False)
    pd.DataFrame(result["factorExposureMatrices"]).to_csv(os.path.join(folder, "factor_exposure_matrices.csv"), index=False)
    for version in result["versions"]:
        slug = "rule_based" if "Rule" in version["name"] else "optimizer_based"
        analytics = version["analytics"]
        pd.DataFrame(version["points"]).to_csv(os.path.join(folder, f"{slug}_equity_curve.csv"), index=False)
        pd.DataFrame(analytics["drawdown"]).to_csv(os.path.join(folder, f"{slug}_drawdown.csv"), index=False)
        pd.DataFrame(analytics["monthlyReturnsHeatmap"]).to_csv(
            os.path.join(folder, f"{slug}_monthly_returns_heatmap.csv"), index=False
        )
        pd.DataFrame(analytics["regimeTimeline"]).to_csv(
            os.path.join(folder, f"{slug}_regime_timeline.csv"), index=False
        )
        pd.DataFrame(analytics["factorExposureHistory"]).to_csv(
            os.path.join(folder, f"{slug}_factor_exposures.csv"), index=False
        )
        pd.DataFrame(analytics["weightHistory"]).to_csv(os.path.join(folder, f"{slug}_weights.csv"), index=False)
        pd.DataFrame(analytics["turnoverHistory"]).to_csv(os.path.join(folder, f"{slug}_turnover.csv"), index=False)
        pd.DataFrame(version["riskContribution"]).to_csv(
            os.path.join(folder, f"{slug}_risk_contribution.csv"), index=False
        )


def run_backtest(capital=100000, config=None):
    config = {**DEFAULT_CONFIG, **(config or {})}
    _store_dgs10_proxy()
    prices, price_counts, price_coverage = _load_prices(config["symbols"], config["price_warmup_start"])
    macro, macro_counts, macro_coverage = _load_macro({"macro_lag_months": 1})
    monthly_prices = prices.groupby(prices.index.to_period("M")).tail(1)
    monthly_returns = monthly_prices.pct_change()
    exposures = _rolling_factor_exposures(monthly_returns, config["factor_lookback_months"])
    if not exposures:
        raise RuntimeError("No rolling factor exposure matrices were produced.")

    rule_based = _run_version("Version A: Rule-Based", prices, macro, exposures, capital, config, optimizer=False)
    optimized = _run_version("Version B: Optimizer-Based", prices, macro, exposures, capital, config, optimizer=True)
    start = pd.Timestamp(optimized["points"][0]["date"])
    benchmark_prices = prices.loc[start:, config["symbols"]]
    benchmarks = [
        _benchmark("SPY Buy & Hold", benchmark_prices, capital, {"SPY": 1}),
        _benchmark("60/40 SPY/IEF", benchmark_prices, capital, {"SPY": 0.6, "IEF": 0.4}, True, config["transaction_cost_bps"]),
        _benchmark(
            "Static Equal-Weight ETF Basket",
            benchmark_prices,
            capital,
            {symbol: 1 for symbol in config["symbols"]},
            True,
            config["transaction_cost_bps"],
        ),
        _benchmark_risk_parity(benchmark_prices, capital, config),
    ]
    performance_table = [
        {"series": result["name"], **result["metrics"]} for result in [rule_based, optimized, *benchmarks]
    ]
    factor_matrix_rows = []
    for period, matrix in exposures.items():
        for symbol, row in matrix.iterrows():
            factor_matrix_rows.append(
                {"date": str(period), "symbol": symbol, **{factor: float(row[factor]) for factor in FACTOR_NAMES}}
            )
    latest_period = max(exposures)
    latest_factor_matrix = [
        {"symbol": symbol, **{factor: float(row[factor]) for factor in FACTOR_NAMES}}
        for symbol, row in exposures[latest_period].iterrows()
    ]
    result = {
        "mode": "python-dynamic-macro-factor-allocation",
        "strategy": "Dynamic Macro Factor Allocation",
        "frequency": "Daily ETF returns, monthly 36-month factor regressions and rebalance",
        "source": "Yahoo adjusted closes; Strategy 1 durable FRED macro regime cache",
        "assumptions": config,
        "rows": {
            "aligned": len(prices),
            "start": prices.index[0].strftime("%Y-%m-%d"),
            "end": prices.index[-1].strftime("%Y-%m-%d"),
            "backtestStart": optimized["points"][0]["date"],
            "backtestEnd": optimized["points"][-1]["date"],
            "bySymbol": price_counts,
            "byMacroSeries": macro_counts,
        },
        "versions": [rule_based, optimized],
        "ruleBased": rule_based,
        "optimized": optimized,
        "metrics": optimized["metrics"],
        "points": optimized["points"],
        "latestWeights": optimized["latestWeights"],
        "latestRegime": optimized["latestRegime"],
        "currentRecommendedAllocation": optimized["latestWeights"],
        "benchmarks": benchmarks,
        "performanceTable": performance_table,
        "latestFactorMatrix": latest_factor_matrix,
        "factorExposureMatrices": factor_matrix_rows,
        "dataHealth": {"priceCoverage": price_coverage, "macroCoverage": macro_coverage},
        "dataLimitations": [
            "Version B remains in SHY until May 2010 because its 36-month factor regression requires HYG history beginning in April 2007.",
            "Version A does not require factor regression and rotates from January 2007.",
            "Factor exposures use correlated ETF proxies and revised historical returns.",
            "The latest macro regime inherits the stale January 2024 OECD CLI limitation.",
        ],
    }
    output_folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "dynamic_macro_factor_allocation")
    os.makedirs(output_folder, exist_ok=True)
    with open(os.path.join(output_folder, "latest_result_summary.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "rows": result["rows"],
                "performanceTable": performance_table,
                "latestRegime": result["latestRegime"],
                "currentRecommendedAllocation": result["currentRecommendedAllocation"],
            },
            handle,
            indent=2,
        )
    _save_outputs(result)
    return result


if __name__ == "__main__":
    payload = run_backtest()
    print(pd.DataFrame(payload["performanceTable"]).to_string(index=False))
    print("\nCurrent optimizer allocation")
    print(pd.Series(payload["currentRecommendedAllocation"]).sort_values(ascending=False).to_string())
