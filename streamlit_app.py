import json
import math
import os
import shutil
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from strategies.defensive_trend_risk_parity import run_backtest as run_defensive_trend_risk_parity
from strategies.crypto_trend_rotation import run_backtest as run_crypto_trend_rotation
from strategies.funding_carry import METADATA as FUNDING_CARRY_METADATA
from strategies.funding_carry import run_backtest as run_funding_carry
from strategies.funding_carry import run_live as run_funding_carry_live
from strategies.futures_basis import METADATA as FUTURES_BASIS_METADATA
from strategies.futures_basis import run_backtest as run_futures_basis
from strategies.futures_basis import run_live as run_futures_basis_live


ROOT = os.path.dirname(os.path.abspath(__file__))
API_PORT = "8010"
API_BASE = f"http://127.0.0.1:{API_PORT}"


STRATEGIES = [
    FUNDING_CARRY_METADATA,
    FUTURES_BASIS_METADATA,
    {
        "id": "crypto-trend-rotation",
        "name": "Crypto Trend Rotation",
        "group": "Crypto",
        "type": "Daily spot trend and momentum rotation",
        "difficulty": "Medium",
        "data_burden": "Light-medium",
        "runtime": "Minutes",
        "capital_need": "Low-medium",
        "best_role": "Directional crypto allocation sleeve",
        "base_return": 0.135,
        "base_vol": 0.245,
        "tail_risk": 58,
        "carry_bias": 0.18,
        "public": True,
        "description": "Rotate across a fixed US-friendly crypto spot universe using daily spot data, momentum, trend, inverse-volatility sizing, transaction costs, and a BTC regime exposure cap.",
        "does": "It downloads and caches CoinGecko data first, tries CoinPaprika as an aggregate-market fallback, then can run from Coinbase USD spot candles when aggregate sources are unavailable. It ranks eligible coins by cross-sectional momentum and volatility, then rebalances weekly into the top names with cash when the opportunity set or BTC regime is weak.",
        "signals": [
            "60-day and 120-day momentum z-scores across BTC, ETH, SOL, XRP, ADA, DOGE, LINK, AVAX, LTC, and BCH",
            "200-day trend filter, positive momentum filters, 20-day and 60-day realized volatility",
            "30-day average dollar volume minimum using CoinGecko 24-hour volume",
            "BTC 200DMA regime cap that limits crypto exposure to 40% when BTC is risk-off",
        ],
        "checklist": [
            "Download CoinGecko market_chart/range daily price, market cap, and volume for the fixed universe.",
            "If CoinGecko is blocked or throttled, try CoinPaprika historical ticker data, then Coinbase USD spot candles.",
            "Cache raw CoinGecko, CoinPaprika, and Coinbase CSVs under data/crypto_trend_rotation/raw.",
            "Create the processed daily panel and data health report.",
            "Run weekly Monday-close backtest with inverse-volatility top-3 selection and 20 bps one-way trading cost.",
            "Compare against BTC, ETH, 50/50 BTC-ETH, equal-weight universe, and cash benchmarks.",
        ],
        "risks": [
            "CoinGecko and CoinPaprika free API limits can interrupt or shorten first-time downloads.",
            "Coinbase fallback does not provide market cap and XRP has a long US listing gap.",
            "Spot crypto trend systems can underperform during sharp single-asset bull markets.",
            "Volume data is exchange-aggregated and may not perfectly represent executable US venue liquidity.",
        ],
        "sources": ["CoinGecko historical market chart/range", "CoinPaprika historical ticker/OHLCV", "Coinbase public daily product candles", "Daily spot-only trend-following research"],
    },
    {
        "id": "risk-parity-etf",
        "name": "Defensive Trend + Risk-Parity ETF",
        "group": "ETF",
        "type": "ETF allocation with trend defense",
        "difficulty": "Medium",
        "data_burden": "Light-medium",
        "runtime": "Monthly",
        "capital_need": "Low-medium",
        "best_role": "Core diversified ETF sleeve",
        "base_return": 0.094,
        "base_vol": 0.105,
        "tail_risk": 39,
        "carry_bias": 0.42,
        "public": True,
        "description": "Allocate across liquid ETFs with trend and momentum eligibility filters, inverse-volatility weights, single-ETF caps, and a SPY 200DMA equity-exposure defense rule.",
        "does": "It fetches daily adjusted ETF prices, keeps only ETFs above their long-term moving average with positive medium-term momentum, allocates eligible ETFs by inverse realized volatility, caps single positions, and moves unused weight into BIL.",
        "signals": [
            "200-day trend filter and 126-day momentum filter",
            "63-day realized volatility for inverse-volatility allocation",
            "25% single-ETF cap with unused allocation moved into BIL",
            "SPY 200DMA overlay capping total equity exposure at 80% risk-on or 30% risk-off",
        ],
        "checklist": [
            "Fetch Yahoo daily adjusted closes for SPY, QQQ, IWM, EFA, EEM, TLT, IEF, GLD, DBC, VNQ, and BIL.",
            "On each month-end, require price above the moving average and positive momentum.",
            "Allocate eligible ETFs by inverse realized volatility.",
            "Apply max ETF weight and SPY-based equity exposure caps.",
            "Track turnover, 10 bps traded-notional cost, BIL allocation, benchmarks, and current weights.",
        ],
        "risks": [
            "Bond-equity correlation can flip positive during inflation shocks.",
            "Trend filters can whipsaw when markets chop around moving averages.",
            "Inverse-volatility weights can concentrate in assets whose recent volatility is temporarily suppressed.",
        ],
        "sources": ["Yahoo daily adjusted ETF history", "Faber tactical allocation research", "AQR time-series momentum", "Risk parity and risk-budgeting research"],
    },
]


FEE_TIERS = {
    "Retail": {"drag": 0.032, "label": "Retail"},
    "Active trader": {"drag": 0.019, "label": "Active trader"},
    "Institutional": {"drag": 0.011, "label": "Institutional"},
}

RISK_MODES = {
    "Conservative": {"return_adj": 0.78, "vol_adj": 0.72, "risk_adj": -12},
    "Balanced": {"return_adj": 1.0, "vol_adj": 1.0, "risk_adj": 0},
    "Aggressive": {"return_adj": 1.27, "vol_adj": 1.34, "risk_adj": 11},
}


st.set_page_config(page_title="Collection Strategy Dashboard", layout="wide")

st.markdown(
    """
    <style>
      .block-container {padding-top: 1.5rem;}
      .metric-card {
        border: 1px solid rgba(128,128,128,.28);
        border-radius: .5rem;
        padding: .85rem 1rem;
        min-height: 6.6rem;
      }
      .metric-label {font-size: .8rem; opacity: .72; text-transform: uppercase; font-weight: 700;}
      .metric-value {
        font-size: 1.35rem;
        font-weight: 750;
        line-height: 1.2;
        margin-top: .45rem;
        overflow-wrap: anywhere;
        white-space: normal;
      }
      .info-card {
        border: 1px solid rgba(128,128,128,.22);
        border-radius: .45rem;
        padding: .75rem .85rem;
        min-height: 5.1rem;
      }
      .info-label {
        font-size: .78rem;
        opacity: .72;
        font-weight: 700;
        text-transform: uppercase;
      }
      .info-value {
        margin-top: .35rem;
        font-size: 1rem;
        font-weight: 700;
        line-height: 1.25;
        overflow-wrap: anywhere;
        white-space: normal;
      }
      .strategy-note {
        border-top: 1px solid rgba(128,128,128,.25);
        padding-top: .85rem;
        margin-top: .85rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


def strategy_by_id(strategy_id):
    return next(item for item in STRATEGIES if item["id"] == strategy_id)


def seeded_noise(seed):
    value = math.sin(seed * 91.17) * 10000
    return value - math.floor(value)


def daily_return(strategy, day_index, fee_tier, risk_mode):
    risk = RISK_MODES[risk_mode]
    fee_drag = FEE_TIERS[fee_tier]["drag"]
    gross_annual = strategy["base_return"] * risk["return_adj"]
    net_annual = max(-0.3, gross_annual - fee_drag)
    vol = strategy["base_vol"] * risk["vol_adj"]
    seasonal = math.sin(day_index / 24 + strategy["tail_risk"] / 17) * 0.0009
    crowding = math.cos(day_index / 57 + strategy["carry_bias"] * 2) * 0.00055
    shock = seeded_noise(day_index + strategy["tail_risk"] * 13)
    jump = -((strategy["tail_risk"] / 100) * 0.035 + seeded_noise(day_index * 7) * 0.018) if shock > 0.985 else 0
    centered = (seeded_noise(day_index * 3.7 + strategy["base_vol"] * 1000) - 0.5) * 2
    return net_annual / 365 + (vol / math.sqrt(365)) * centered + seasonal + crowding + jump


def summarize_points(points, initial_equity):
    if len(points) < 2:
        return {"pnl": 0, "annReturn": 0, "annVol": 0, "maxDrawdown": 0, "sharpe": 0}
    peak = initial_equity
    max_dd = 0
    returns = []
    for index, point in enumerate(points):
        peak = max(peak, point["equity"])
        max_dd = min(max_dd, point["equity"] / peak - 1)
        if index:
            returns.append(point["equity"] / points[index - 1]["equity"] - 1)
    mean = sum(returns) / max(1, len(returns))
    variance = sum((value - mean) ** 2 for value in returns) / max(1, len(returns) - 1)
    final_equity = points[-1]["equity"]
    ann_return = (final_equity / initial_equity) ** (365 / max(1, len(points))) - 1
    ann_vol = math.sqrt(variance) * math.sqrt(365)
    return {
        "pnl": final_equity - initial_equity,
        "annReturn": ann_return,
        "annVol": ann_vol,
        "maxDrawdown": max_dd,
        "sharpe": 0 if ann_vol == 0 else ann_return / ann_vol,
    }


def simulate_strategy(strategy, capital, lookback, fee_tier, risk_mode, allocation_count=None):
    allocation = capital / (allocation_count or len(STRATEGIES))
    equity = allocation
    points = []
    for day in range(lookback):
        equity *= 1 + daily_return(strategy, day, fee_tier, risk_mode)
        points.append({"day": day, "equity": equity})
    metrics = summarize_points(points, allocation)
    metrics["riskScore"] = max(1, min(100, round(strategy["tail_risk"] + RISK_MODES[risk_mode]["risk_adj"])))
    return {"mode": "synthetic-streamlit", "metrics": metrics, "points": points, "status": "Research model"}


def api_is_up():
    try:
        with urlopen(f"{API_BASE}/", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def start_api_server():
    node = shutil.which("node") or r"D:\Program Files\nodejs\node.exe"
    server = os.path.join(ROOT, "server.js")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    env = os.environ.copy()
    env["PORT"] = API_PORT
    subprocess.Popen(
        [node, server],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    for _ in range(12):
        time.sleep(0.5)
        if api_is_up():
            return True, None
    return False, "Started local API server, but it did not respond on port 8000."


def ensure_api_server():
    if api_is_up():
        return True, None
    return start_api_server()


def fetch_json(path, params=None, timeout=90):
    ok, error = ensure_api_server()
    if not ok:
        return None, error
    query = f"?{urlencode(params)}" if params else ""
    try:
        with urlopen(f"{API_BASE}{path}{query}", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), None
    except HTTPError as error:
        body = error.read().decode("utf-8")
        try:
            payload = json.loads(body)
            return None, payload.get("error", body)
        except Exception:
            return None, body or str(error)
    except URLError as error:
        return None, f"Local API unavailable: {error.reason}"
    except Exception as error:
        return None, str(error)


def load_public_payload(strategy_id, view_mode, capital, lookback, symbol):
    if strategy_id == "funding-carry":
        if view_mode == "Live":
            return run_funding_carry_live(fetch_json, symbol=symbol)
        return run_funding_carry(fetch_json, capital=capital, lookback=lookback, symbol=symbol)

    if strategy_id == "basis-carry":
        if view_mode == "Live":
            return run_futures_basis_live(fetch_json)
        return run_futures_basis(fetch_json, capital=capital, lookback=lookback)

    if strategy_id == "crypto-trend-rotation":
        if view_mode == "Live":
            return None, "Crypto Trend Rotation is daily backtest-only for now."
        try:
            return run_crypto_trend_rotation(capital=capital), None
        except Exception as error:
            return None, str(error)

    if strategy_id == "risk-parity-etf":
        if view_mode == "Live":
            return None, "This ETF strategy is monthly and backtest-only for now."
        try:
            return run_defensive_trend_risk_parity(capital=capital), None
        except Exception as error:
            return None, str(error)

    return None, None


def payload_to_result(strategy, payload, capital, lookback, fee_tier, risk_mode):
    if not payload:
        return simulate_strategy(strategy, capital, lookback, fee_tier, risk_mode)
    metrics = payload.get("metrics") or {}
    points = payload.get("points") or payload.get("equitySparkline") or []
    if not points and strategy["id"] == "funding-carry":
        ann = payload.get("annualizedFunding") or 0
        points = [{"day": 0, "equity": capital}, {"day": 1, "equity": capital * (1 + ann / 365)}]
        metrics = {"annReturn": ann, "pnl": None, "maxDrawdown": None, "sharpe": None}
    if not points and strategy["id"] == "basis-carry":
        ann = payload.get("annualizedBasis") or 0
        points = [{"day": 0, "equity": capital}, {"day": 1, "equity": capital * (1 + ann / 365)}]
        metrics = {"annReturn": ann, "pnl": None, "maxDrawdown": None, "sharpe": None}
    return {"mode": payload.get("mode"), "metrics": metrics, "points": points, "payload": payload, "status": "Public data"}


def result_frame(result):
    points = result.get("points") or []
    frame = pd.DataFrame(points)
    if frame.empty:
        return frame
    if "time" in frame.columns:
        frame["date"] = pd.to_datetime(frame["time"], unit="ms", errors="coerce")
    elif "day" in frame.columns:
        frame["date"] = frame["day"]
    else:
        frame["date"] = frame.index
    return frame


def format_money(value):
    if value is None:
        return "-"
    try:
        return f"${float(value):,.0f}"
    except Exception:
        return "-"


def format_pct(value):
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return "-"


def metric_cards(metrics):
    items = [
        ("Net PnL", format_money(metrics.get("pnl"))),
        ("Annualized Return", format_pct(metrics.get("annReturn") or metrics.get("return"))),
        ("Annualized Vol", format_pct(metrics.get("annVol"))),
        ("Max Drawdown", format_pct(metrics.get("maxDrawdown") or metrics.get("max_drawdown"))),
        ("Sharpe", "-" if metrics.get("sharpe") is None else f"{float(metrics.get('sharpe')):.2f}"),
    ]
    cols = st.columns(len(items))
    for col, (label, value) in zip(cols, items):
        col.markdown(
            f'<div class="metric-card"><div class="metric-label">{label}</div><div class="metric-value">{value}</div></div>',
            unsafe_allow_html=True,
        )


def info_card(label, value):
    st.markdown(
        f'<div class="info-card"><div class="info-label">{label}</div><div class="info-value">{value}</div></div>',
        unsafe_allow_html=True,
    )


def benchmark_series(payload):
    if not payload:
        return []
    raw = payload.get("benchmarks")
    if isinstance(raw, dict):
        raw = list(raw.values()) if raw.get("points") else []
    elif raw is None and payload.get("benchmark"):
        raw = [payload["benchmark"]]
    elif not isinstance(raw, list):
        raw = []
    return [item for item in raw if isinstance(item, dict) and item.get("points")]


def equity_chart(result, title):
    frame = result_frame(result)
    if frame.empty or "equity" not in frame.columns:
        st.info("No equity points available yet.")
        return
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["equity"],
            mode="lines",
            name="Equity",
            hovertemplate="%{x}<br>$%{y:,.0f}<extra></extra>",
        )
    )
    payload = result.get("payload") or {}
    for benchmark in benchmark_series(payload):
        benchmark_frame = pd.DataFrame(benchmark.get("points") or [])
        if not benchmark_frame.empty and "equity" in benchmark_frame.columns:
            if "time" in benchmark_frame.columns:
                benchmark_frame["date"] = pd.to_datetime(benchmark_frame["time"], unit="ms", errors="coerce")
            elif "day" in benchmark_frame.columns:
                benchmark_frame["date"] = benchmark_frame["day"]
            else:
                benchmark_frame["date"] = benchmark_frame.index
            fig.add_trace(
                go.Scatter(
                    x=benchmark_frame["date"],
                    y=benchmark_frame["equity"],
                    mode="lines",
                    name=benchmark.get("name", "Benchmark"),
                    line=dict(dash="dash"),
                    hovertemplate="%{x}<br>$%{y:,.0f}<extra></extra>",
                )
            )
    fig.update_layout(height=420, title=title, template="plotly_white", margin=dict(l=20, r=20, t=50, b=20))
    st.plotly_chart(fig, use_container_width=True)


def benchmark_table(payload):
    benchmarks = benchmark_series(payload)
    if not benchmarks:
        return
    metrics = payload.get("metrics", {})
    strategy_name = payload.get("strategy") or "Strategy"
    rows = [
        {
            "Series": strategy_name,
            "Net PnL": format_money(metrics.get("pnl")),
            "Ann. Return": format_pct(metrics.get("annReturn")),
            "Ann. Vol": format_pct(metrics.get("annVol")),
            "Max DD": format_pct(metrics.get("maxDrawdown")),
            "Sharpe": "-" if metrics.get("sharpe") is None else f"{float(metrics.get('sharpe')):.2f}",
        },
    ]
    for benchmark in benchmarks:
        benchmark_metrics = benchmark.get("metrics", {})
        rows.append(
            {
                "Series": benchmark.get("name", "Benchmark"),
                "Net PnL": format_money(benchmark_metrics.get("pnl")),
                "Ann. Return": format_pct(benchmark_metrics.get("annReturn")),
                "Ann. Vol": format_pct(benchmark_metrics.get("annVol")),
                "Max DD": format_pct(benchmark_metrics.get("maxDrawdown")),
                "Sharpe": "-"
                if benchmark_metrics.get("sharpe") is None
                else f"{float(benchmark_metrics.get('sharpe')):.2f}",
            }
        )
        rows.append(
            {
                "Series": f"Strategy minus {benchmark.get('name', 'benchmark')}",
                "Net PnL": format_money(metrics.get("pnl") - benchmark_metrics.get("pnl"))
                if metrics.get("pnl") is not None and benchmark_metrics.get("pnl") is not None
                else "-",
                "Ann. Return": format_pct(metrics.get("annReturn") - benchmark_metrics.get("annReturn"))
                if metrics.get("annReturn") is not None and benchmark_metrics.get("annReturn") is not None
                else "-",
                "Ann. Vol": "-",
                "Max DD": "-",
                "Sharpe": "-",
            }
        )
    st.markdown("#### Benchmark Comparison")
    st.caption("Benchmarks use the same data window as the strategy.")
    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
    )


def comparison_table(capital, lookback, fee_tier, risk_mode):
    rows = []
    for strategy in STRATEGIES:
        result = simulate_strategy(strategy, capital, lookback, fee_tier, risk_mode)
        metrics = result["metrics"]
        rows.append(
            {
                "Group": strategy["group"],
                "Strategy": strategy["name"],
                "Mode": "Public route" if strategy["public"] else "Research model",
                "Net PnL": metrics["pnl"],
                "Ann. Return": metrics["annReturn"],
                "Vol": metrics["annVol"],
                "Max DD": metrics["maxDrawdown"],
                "Sharpe": metrics["sharpe"],
                "Risk": metrics["riskScore"],
            }
        )
    return pd.DataFrame(rows)


def render_strategy_context(strategy):
    st.subheader(strategy["name"])
    st.caption(f'{strategy["group"]} | {strategy["type"]} | Difficulty: {strategy["difficulty"]}')
    st.write(strategy["description"])
    st.markdown('<div class="strategy-note"><strong>What this strategy does</strong></div>', unsafe_allow_html=True)
    st.write(strategy["does"])
    info_cols = st.columns(4)
    with info_cols[0]:
        info_card("Data burden", strategy["data_burden"])
    with info_cols[1]:
        info_card("Runtime", strategy["runtime"])
    with info_cols[2]:
        info_card("Capital need", strategy["capital_need"])
    with info_cols[3]:
        info_card("Best role", strategy["best_role"])


def render_lists(strategy):
    left, mid, right = st.columns(3)
    with left:
        st.markdown("#### Signals")
        for item in strategy["signals"]:
            st.write(f"- {item}")
    with mid:
        st.markdown("#### Build Sequence")
        for index, item in enumerate(strategy["checklist"], start=1):
            st.write(f"{index}. {item}")
    with right:
        st.markdown("#### Risks")
        for item in strategy["risks"]:
            st.write(f"- {item}")
        st.markdown("#### Sources")
        for item in strategy["sources"]:
            st.write(f"- {item}")


st.title("Collection Strategy Dashboard")
st.caption("Streamlit dashboard for crypto, ETF, and equity strategy research")

with st.sidebar:
    st.header("Scenario")
    grouped = {}
    for item in STRATEGIES:
        grouped.setdefault(item["group"], []).append(item)
    labels = []
    label_to_id = {}
    for group, items in grouped.items():
        for item in items:
            label = f'{group} - {item["name"]}'
            labels.append(label)
            label_to_id[label] = item["id"]
    selected_label = st.selectbox("Strategy", labels, index=0)
    selected_id = label_to_id[selected_label]
    view_mode = st.radio("View", ["Backtest", "Live"], horizontal=True)
    data_mode = st.radio("Data", ["Public if available", "Synthetic research"], horizontal=False)
    capital = st.number_input("Capital", min_value=1000, value=100000, step=1000)
    lookback = st.selectbox("Lookback", [180, 365, 730, 1095], index=1)
    fee_tier = st.selectbox("Fee tier", list(FEE_TIERS.keys()), index=0)
    risk_mode = st.selectbox("Risk mode", list(RISK_MODES.keys()), index=1)
    symbol = st.text_input("Crypto symbol", "BTCUSDT")
    run = st.button("Run / Refresh", type="primary")

selected = strategy_by_id(selected_id)
key = (selected_id, view_mode, data_mode, capital, lookback, fee_tier, risk_mode, symbol)

if run or st.session_state.get("last_key") != key:
    payload = None
    error = None
    if data_mode == "Public if available" and selected["public"]:
        with st.spinner("Loading public strategy data..."):
            payload, error = load_public_payload(selected_id, view_mode, capital, lookback, symbol)
    result = payload_to_result(selected, payload, capital, lookback, fee_tier, risk_mode) if payload else simulate_strategy(
        selected, capital, lookback, fee_tier, risk_mode
    )
    st.session_state.last_key = key
    st.session_state.last_result = result
    st.session_state.last_error = error

result = st.session_state.get("last_result") or simulate_strategy(selected, capital, lookback, fee_tier, risk_mode)
error = st.session_state.get("last_error")

if error and data_mode == "Public if available":
    st.warning(f"Public route unavailable, showing synthetic research model. Reason: {error}")

top_left, top_right = st.columns([1.25, 0.75])
with top_left:
    render_strategy_context(selected)
with top_right:
    payload = result.get("payload") or {}
    st.markdown("#### Current Public Snapshot")
    if selected_id == "crypto-trend-rotation" and payload.get("latestRegime"):
        latest = payload["latestRegime"]
        rows = payload.get("rows", {})
        info_card("BTC Regime", latest.get("regime", "-"))
        info_card("Crypto cap", format_pct(latest.get("equityCap")))
        info_card("Cash allocation", format_pct(latest.get("cashAllocation")))
        info_card("Backtest window", f"{rows.get('backtestStart', '-')} to {rows.get('backtestEnd', '-')}")
    elif selected_id == "risk-parity-etf" and payload.get("latestRegime"):
        latest = payload["latestRegime"]
        rows = payload.get("rows", {})
        info_card("SPY Regime", latest.get("regime", "-"))
        info_card("Equity cap", format_pct(latest.get("equityCap")))
        info_card("BIL allocation", format_pct(latest.get("cashAllocation")))
        info_card("Backtest window", f"{rows.get('backtestStart', '-')} to {rows.get('backtestEnd', '-')}")
    elif payload:
        st.json(
            {
                "mode": payload.get("mode"),
                "frequency": payload.get("frequency"),
                "rows": payload.get("rows"),
                "source": payload.get("source"),
            }
        )
    else:
        st.info("Using synthetic research model for this view.")

metric_cards(result.get("metrics", {}))
equity_chart(result, f'{selected["name"]} equity curve')

if selected_id == "crypto-trend-rotation" and result.get("payload"):
    payload = result["payload"]
    benchmark_table(payload)
    left_panel, right_panel = st.columns(2)
    with left_panel:
        st.markdown("#### Current Recommended Portfolio")
        weights = pd.DataFrame(
            [
                {"Symbol": symbol, "Weight": weight}
                for symbol, weight in (payload.get("latestWeights") or {}).items()
                if weight > 0.0001
            ]
        )
        if weights.empty:
            st.info("No crypto holdings currently selected; model is in cash.")
        else:
            st.dataframe(weights, use_container_width=True, hide_index=True)
    with right_panel:
        st.markdown("#### Latest Eligible Coins")
        eligible = pd.DataFrame({"Symbol": payload.get("latestEligible") or []})
        if eligible.empty:
            st.info("No coins passed the latest eligibility filters.")
        else:
            st.dataframe(eligible, use_container_width=True, hide_index=True)

    st.markdown("#### Data Health")
    health_rows = pd.DataFrame((payload.get("dataHealth") or {}).get("rows") or [])
    if health_rows.empty:
        st.info("No data health rows available.")
    else:
        st.dataframe(health_rows, use_container_width=True, hide_index=True)
    health_errors = (payload.get("dataHealth") or {}).get("errors") or []
    for issue in health_errors:
        st.warning(issue)

    st.markdown("#### Robustness Test Metrics")
    robustness = pd.DataFrame(payload.get("robustness") or [])
    if robustness.empty:
        st.info("No robustness metrics available.")
    else:
        keep = [
            column
            for column in ["variant", "annReturn", "annVol", "maxDrawdown", "sharpe", "turnover", "percentTimeInCash"]
            if column in robustness.columns
        ]
        st.dataframe(robustness[keep], use_container_width=True, hide_index=True)

if selected_id == "risk-parity-etf" and result.get("payload"):
    payload = result["payload"]
    benchmark_table(payload)
    if payload.get("latestWeights"):
        st.markdown("#### Latest ETF Weights")
        weights = pd.DataFrame(
            [{"Symbol": symbol, "Weight": weight} for symbol, weight in payload["latestWeights"].items()]
        )
        st.dataframe(weights, use_container_width=True, hide_index=True)

with st.expander("Strategy Details", expanded=True):
    render_lists(selected)

st.markdown("### Strategy Comparison")
table = comparison_table(capital, lookback, fee_tier, risk_mode)
st.dataframe(
    table,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Net PnL": st.column_config.NumberColumn(format="$%.0f"),
        "Ann. Return": st.column_config.NumberColumn(format="%.2f"),
        "Vol": st.column_config.NumberColumn(format="%.2f"),
        "Max DD": st.column_config.NumberColumn(format="%.2f"),
        "Sharpe": st.column_config.NumberColumn(format="%.2f"),
    },
)

csv = table.to_csv(index=False).encode("utf-8")
st.download_button("Download comparison CSV", csv, "collection-strategy-dashboard-results.csv", "text/csv")

with st.expander("Raw Result Payload"):
    st.json(result.get("payload") or result)
