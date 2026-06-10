import json
import math
import os

import numpy as np
import pandas as pd


SHORT_NAMES = {
    "Strategy 1 - Regime-Aware ETF Momentum": "Regime ETF",
    "Strategy 2 - Dynamic Macro Factor Allocation": "Macro Factor",
    "Strategy 3 - Pure Business-Cycle Asset Rotation": "Business Cycle",
    "Strategy 4 - BlackRock Factor Replication": "BlackRock Replication",
}
FACTORS = ["Growth", "Inflation", "Credit", "Duration", "Commodity", "Cash/Defensive"]
RISKY_ETFS = {"SPY", "QQQ", "IWM", "EFA", "EEM", "HYG", "LQD", "DBC"}
HYPOTHETICAL_SHOCKS = {
    "Rates +100 bps": {"TLT": -0.15, "IEF": -0.07, "LQD": -0.08, "HYG": -0.06, "TIP": -0.06},
    "Equity -10%": {"SPY": -0.10, "QQQ": -0.13, "IWM": -0.12, "EFA": -0.10, "EEM": -0.12, "HYG": -0.04},
}
HISTORICAL_WINDOWS = {
    "2008 Crisis": ("2008-09-01", "2009-03-09"),
    "COVID Crash": ("2020-02-19", "2020-03-23"),
    "2022 Inflation Shock": ("2022-01-03", "2022-10-14"),
}


def _name(label):
    return SHORT_NAMES.get(label, label)


def _points(payload):
    frame = pd.DataFrame(payload.get("points") or [])
    if frame.empty or "date" not in frame or "equity" not in frame:
        return pd.Series(dtype=float)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.drop_duplicates("date").set_index("date")["equity"].astype(float).sort_index()


def _weights(payload):
    return payload.get("currentRecommendedAllocation") or payload.get("latestWeights") or {}


def _monthly_returns(payloads):
    series = {}
    for label, payload in payloads.items():
        equity = _points(payload)
        if not equity.empty:
            series[_name(label)] = equity.resample("ME").last().pct_change()
    return pd.DataFrame(series).dropna(how="all")


def _drawdowns(payloads):
    series = {}
    for label, payload in payloads.items():
        equity = _points(payload)
        if not equity.empty:
            series[_name(label)] = equity / equity.cummax() - 1
    return pd.DataFrame(series)


def _rolling_sharpe(monthly):
    return monthly.rolling(12).mean() / monthly.rolling(12).std() * math.sqrt(12)


def _status(payload):
    metrics = payload.get("metrics") or {}
    equity = _points(payload)
    current_dd = float(equity.iloc[-1] / equity.cummax().iloc[-1] - 1) if not equity.empty else 0
    rolling = equity.pct_change().rolling(252)
    recent_sharpe = float((rolling.mean() / rolling.std() * math.sqrt(252)).iloc[-1]) if len(equity) > 252 else np.nan
    stale_cli = "BlackRock" not in payload.get("strategy", "")
    if current_dd <= -0.10 or (np.isfinite(recent_sharpe) and recent_sharpe < 0):
        return "Alert"
    if current_dd <= -0.05 or metrics.get("averageMonthlyTurnover", 0) > 0.30 or stale_cli:
        return "Watch"
    return "Normal"


def _allocation_frame(payloads):
    rows = {}
    for label, payload in payloads.items():
        rows[_name(label)] = {symbol: float(weight) for symbol, weight in _weights(payload).items()}
    return pd.DataFrame(rows).fillna(0).sort_index()


def _load_json_prices(root, symbols):
    prices = {}
    for symbol in symbols:
        path = os.path.join(root, "data", "etf_cache", f"{symbol}.json")
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            frame = pd.DataFrame(json.load(handle))
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"])
            prices[symbol] = frame.drop_duplicates("date").set_index("date")["close"].astype(float)
    return pd.DataFrame(prices).sort_index().ffill()


def _latest_factor_matrix(root):
    path = os.path.join(root, "data", "blackrock_factor_replication", "latest_factor_matrix.csv")
    if not os.path.exists(path):
        return pd.DataFrame()
    frame = pd.read_csv(path).set_index("symbol")
    return frame[[column for column in frame if column in FACTORS]]


def _factor_exposures(payloads, root):
    matrix = _latest_factor_matrix(root)
    output = {}
    for label, payload in payloads.items():
        weights = pd.Series(_weights(payload), dtype=float)
        common = matrix.index.intersection(weights.index)
        exposure = matrix.loc[common].T @ weights.loc[common] if len(common) else pd.Series(dtype=float)
        exposure["Cash/Defensive"] = float(weights.get("SHY", 0))
        output[_name(label)] = exposure
    return pd.DataFrame(output).reindex(FACTORS).fillna(0)


def _risk_tables(payloads, root):
    allocation = _allocation_frame(payloads)
    prices = _load_json_prices(root, allocation.index)
    returns = prices.pct_change().dropna(how="all")
    factor_exposure = _factor_exposures(payloads, root)
    etf_risk = {}
    summary = []
    for strategy in allocation:
        weights = allocation[strategy].reindex(returns.columns).fillna(0)
        covariance = returns.tail(252).cov() * 252
        variance = float(weights @ covariance @ weights)
        marginal = covariance @ weights
        contributions = weights * marginal
        if variance > 0:
            contributions /= variance
        etf_risk[strategy] = contributions
        factors = factor_exposure[strategy].abs()
        concentration = float((factors / factors.sum()).pow(2).sum()) if factors.sum() else 0
        summary.append(
            {
                "Strategy": strategy,
                "Portfolio Volatility": math.sqrt(max(variance, 0)),
                "Factor Concentration Score": concentration,
            }
        )
    factor_risk = factor_exposure.abs().div(factor_exposure.abs().sum(axis=0), axis=1).fillna(0)
    return pd.DataFrame(etf_risk).fillna(0), factor_risk, pd.DataFrame(summary)


def _macro_frame(root):
    ids = ["USALOLITONOSTSAM", "CPIAUCSL", "BAMLH0A0HYM2", "T10Y3M", "UNRATE"]
    output = {}
    for series_id in ids:
        path = os.path.join(root, "data", "fred_cache", f"{series_id}.csv")
        if not os.path.exists(path):
            continue
        frame = pd.read_csv(path)
        dates = pd.to_datetime(frame.iloc[:, 0], errors="coerce")
        values = pd.to_numeric(frame.iloc[:, -1], errors="coerce")
        output[series_id] = pd.Series(values.values, index=dates).dropna()
    return pd.DataFrame(output).sort_index().resample("ME").last().ffill()


def _regime_probabilities(macro):
    if macro.empty or "USALOLITONOSTSAM" not in macro:
        return pd.Series(dtype=float)
    cli = macro["USALOLITONOSTSAM"].dropna()
    trend = float(cli.iloc[-1] - cli.rolling(12).mean().iloc[-1])
    momentum = float(cli.iloc[-1] - cli.shift(3).iloc[-1])
    scale = max(float(cli.diff().rolling(24).std().iloc[-1]), 0.05)
    level_high = 1 / (1 + math.exp(-trend / scale))
    rising = 1 / (1 + math.exp(-momentum / scale))
    probabilities = pd.Series(
        {
            "Recovery": (1 - level_high) * rising,
            "Expansion": level_high * rising,
            "Slowdown": level_high * (1 - rising),
            "Contraction": (1 - level_high) * (1 - rising),
        }
    )
    return probabilities / probabilities.sum()


def _regime_timeline(payloads):
    for label in [
        "Strategy 3 - Pure Business-Cycle Asset Rotation",
        "Strategy 2 - Dynamic Macro Factor Allocation",
        "Strategy 1 - Regime-Aware ETF Momentum",
    ]:
        payload = payloads.get(label) or {}
        analytics = payload.get("analytics") or {}
        rows = analytics.get("regimeTimeline") or payload.get("weightHistory") or []
        if rows:
            frame = pd.DataFrame(rows)
            if "date" not in frame and "time" in frame:
                frame["date"] = pd.to_datetime(frame["time"], unit="ms")
            if "date" in frame and "regime" in frame:
                frame["date"] = pd.to_datetime(frame["date"])
                return frame[["date", "regime"]]
    return pd.DataFrame()


def _stress_tests(payloads):
    rows = []
    for scenario, (start, end) in HISTORICAL_WINDOWS.items():
        row = {"Scenario": scenario, "Type": "Historical backtest window"}
        for label, payload in payloads.items():
            equity = _points(payload).loc[start:end]
            row[_name(label)] = float(equity.iloc[-1] / equity.iloc[0] - 1) if len(equity) > 1 else np.nan
        rows.append(row)
    for scenario, shocks in HYPOTHETICAL_SHOCKS.items():
        row = {"Scenario": scenario, "Type": "Current-weight linear shock"}
        for label, payload in payloads.items():
            row[_name(label)] = sum(float(weight) * shocks.get(symbol, 0) for symbol, weight in _weights(payload).items())
        rows.append(row)
    return pd.DataFrame(rows)


def _signal_health(payloads, monthly):
    rows = []
    rolling = _rolling_sharpe(monthly)
    for label, payload in payloads.items():
        name = _name(label)
        returns = monthly[name].dropna()
        recent = returns.tail(12)
        metrics = payload.get("metrics") or {}
        rows.append(
            {
                "Strategy": name,
                "Rolling 12M Sharpe": rolling[name].dropna().iloc[-1] if not rolling[name].dropna().empty else np.nan,
                "Rolling 12M Hit Rate": float((recent > 0).mean()) if len(recent) else np.nan,
                "Average Monthly Turnover": metrics.get("averageMonthlyTurnover"),
                "Factor Drift": "Available for factor strategies only",
                "Regime Accuracy": "Unavailable without point-in-time outcome definition",
            }
        )
    return pd.DataFrame(rows)


def _alerts(payloads, factor_exposure, risk_summary, macro):
    rows = []
    for label, payload in payloads.items():
        name = _name(label)
        equity = _points(payload)
        dd = float(equity.iloc[-1] / equity.cummax().iloc[-1] - 1) if not equity.empty else 0
        if dd <= -0.10:
            rows.append({"Alert": "Current drawdown below -10%", "Strategy": name, "Severity": "Red"})
        elif dd <= -0.05:
            rows.append({"Alert": "Current drawdown below -5%", "Strategy": name, "Severity": "Yellow"})
        turnover = (payload.get("metrics") or {}).get("averageMonthlyTurnover", 0)
        if turnover and turnover > 0.30:
            rows.append({"Alert": "Turnover unusually high", "Strategy": name, "Severity": "Yellow"})
    growth = factor_exposure.loc["Growth"].abs() if "Growth" in factor_exposure.index else pd.Series(dtype=float)
    if not growth.empty and growth.max() > 0.70:
        rows.append({"Alert": "Growth exposure above 70%", "Strategy": growth.idxmax(), "Severity": "Yellow"})
    probabilities = _regime_probabilities(macro)
    if probabilities.get("Contraction", 0) > 0.40:
        rows.append({"Alert": "Modeled contraction probability above 40%", "Strategy": "Macro", "Severity": "Red"})
    if macro.get("BAMLH0A0HYM2", pd.Series(dtype=float)).pct_change(3).iloc[-1:].gt(0.25).any():
        rows.append({"Alert": "Credit spread widening rapidly", "Strategy": "All strategies", "Severity": "Yellow"})
    if not rows:
        rows.append({"Alert": "No configured threshold breached", "Strategy": "All strategies", "Severity": "Green"})
    return pd.DataFrame(rows)


def _line_chart(frame, title, percent=False):
    import plotly.graph_objects as go
    import streamlit as st

    fig = go.Figure()
    for column in frame:
        fig.add_trace(go.Scatter(x=frame.index, y=frame[column], name=column))
    fig.update_layout(height=400, title=title, template="plotly_white", margin=dict(l=20, r=20, t=50, b=20))
    if percent:
        fig.update_yaxes(tickformat=".0%")
    st.plotly_chart(fig, use_container_width=True)


def render_aggregated_risk_dashboard(payloads, root):
    import plotly.graph_objects as go
    import streamlit as st

    monthly = _monthly_returns(payloads)
    allocations = _allocation_frame(payloads)
    macro = _macro_frame(root)
    factor_exposure = _factor_exposures(payloads, root)
    etf_risk, factor_risk, risk_summary = _risk_tables(payloads, root)

    tabs = st.tabs(
        [
            "Overview", "Performance", "Current Allocation", "Macro Regime", "Factor Exposure",
            "Risk Contribution", "Stress Tests", "Signal Health", "News & Events", "Alerts",
        ]
    )
    with tabs[0]:
        rows = []
        for label, payload in payloads.items():
            metrics = payload.get("metrics") or {}
            rows.append(
                {
                    "Strategy": _name(label), "CAGR": metrics.get("cagr"), "Sharpe": metrics.get("sharpe"),
                    "Max DD": metrics.get("maxDrawdown"), "Vol": metrics.get("annVol"),
                    "Current Weight": 0.25, "Status": _status(payload),
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption("Current Weight assumes an equal-weight allocation across the four strategy sleeves.")

    with tabs[1]:
        normalized = pd.DataFrame({_name(label): _points(payload) / _points(payload).iloc[0] for label, payload in payloads.items()})
        _line_chart(normalized, "Combined normalized equity curves")
        _line_chart(_drawdowns(payloads), "Drawdown comparison", True)
        _line_chart(_rolling_sharpe(monthly), "Rolling 12-month Sharpe")
        heat = monthly.tail(24).T
        fig = go.Figure(go.Heatmap(z=heat.values, x=[date.strftime("%Y-%m") for date in heat.columns], y=heat.index, colorscale="RdYlGn", zmid=0))
        fig.update_layout(height=340, title="Monthly return heatmap", template="plotly_white")
        st.plotly_chart(fig, use_container_width=True)

    with tabs[2]:
        st.dataframe(allocations, use_container_width=True)
        st.bar_chart(allocations)

    with tabs[3]:
        regimes = [{"Strategy": _name(label), "Current Regime": (payload.get("latestRegime") or {}).get("regime", "Static factor target")} for label, payload in payloads.items()]
        st.dataframe(pd.DataFrame(regimes), use_container_width=True, hide_index=True)
        probabilities = _regime_probabilities(macro)
        st.caption("Regime probability is a model-implied heuristic from CLI level and momentum, not a calibrated forecast.")
        st.bar_chart(probabilities)
        timeline = _regime_timeline(payloads)
        if not timeline.empty:
            mapping = {"Recovery": 1, "Expansion": 2, "Slowdown": 3, "Contraction": 4}
            _line_chart(timeline.set_index("date")["regime"].map(mapping).to_frame("Regime state"), "Regime timeline")
        trends = pd.DataFrame(index=macro.index)
        if "USALOLITONOSTSAM" in macro:
            trends["Growth trend"] = macro["USALOLITONOSTSAM"] - macro["USALOLITONOSTSAM"].rolling(12).mean()
        if "CPIAUCSL" in macro:
            trends["Inflation YoY"] = macro["CPIAUCSL"].pct_change(12)
        if "BAMLH0A0HYM2" in macro:
            trends["Credit spread"] = macro["BAMLH0A0HYM2"]
        if "T10Y3M" in macro:
            trends["Yield curve"] = macro["T10Y3M"]
        _line_chart(trends.tail(120), "Macro trends")

    with tabs[4]:
        st.dataframe(factor_exposure, use_container_width=True)
        st.bar_chart(factor_exposure)
        st.caption("Non-factor strategies use the latest BlackRock ETF regression matrix as a common factor-mapping approximation.")

    with tabs[5]:
        st.markdown("#### Risk Contribution by ETF")
        st.dataframe(etf_risk, use_container_width=True)
        st.markdown("#### Risk Contribution by Factor")
        st.dataframe(factor_risk, use_container_width=True)
        st.markdown("#### Portfolio Risk Summary")
        st.dataframe(risk_summary, use_container_width=True, hide_index=True)
        st.caption("ETF risk contribution uses the latest weights and trailing 252-day covariance.")

    with tabs[6]:
        st.dataframe(_stress_tests(payloads), use_container_width=True, hide_index=True)
        st.caption("Historical rows use realized strategy backtest windows. Hypothetical rows are current-weight linear shocks without correlation or second-order effects.")

    with tabs[7]:
        st.dataframe(_signal_health(payloads, monthly), use_container_width=True, hide_index=True)
        st.markdown("#### Strategy Correlation Matrix")
        st.dataframe(monthly.corr(), use_container_width=True)

    with tabs[8]:
        events = pd.DataFrame(
            [
                {"Monitor": "Upcoming CPI", "Status": "Unavailable", "Detail": "Live economic-calendar connector not configured"},
                {"Monitor": "Upcoming FOMC", "Status": "Unavailable", "Detail": "Live economic-calendar connector not configured"},
                {"Monitor": "Upcoming NFP", "Status": "Unavailable", "Detail": "Live economic-calendar connector not configured"},
                {"Monitor": "Latest macro surprise", "Status": "Unavailable", "Detail": "Consensus-estimate data source not configured"},
                {"Monitor": "Credit spread alert", "Status": "Available", "Detail": "Uses cached BAMLH0A0HYM2 history"},
                {"Monitor": "VIX alert", "Status": "Unavailable", "Detail": "VIX history is not in the current strategy dataset"},
                {"Monitor": "Yield curve alert", "Status": "Available", "Detail": "Uses cached T10Y3M history"},
            ]
        )
        st.dataframe(events, use_container_width=True, hide_index=True)
        st.caption("This is intentionally a risk monitor, not a news feed. Unavailable items are not inferred or fabricated.")

    with tabs[9]:
        alerts = _alerts(payloads, factor_exposure, risk_summary, macro)
        st.dataframe(alerts, use_container_width=True, hide_index=True)
        st.caption("Threshold alerts are research diagnostics, not investment advice.")
