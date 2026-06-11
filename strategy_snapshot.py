import json
import os
from datetime import datetime, timezone

from strategies.blackrock_factor_replication import run_backtest as run_blackrock_factor_replication
from strategies.dynamic_macro_factor_allocation import run_backtest as run_dynamic_macro_factor_allocation
from strategies.pure_business_cycle_asset_rotation import run_backtest as run_pure_business_cycle_asset_rotation
from strategies.regime_aware_etf_momentum import run_backtest as run_regime_aware_etf_momentum


ROOT = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.path.join(ROOT, "data", "strategies_1_4_snapshot.json")
SNAPSHOT_VERSION = 1


def _json_default(value):
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def run_strategies_one_to_four(capital):
    return {
        "Strategy 1 - Regime-Aware ETF Momentum": run_regime_aware_etf_momentum(capital=capital),
        "Strategy 2 - Dynamic Macro Factor Allocation": run_dynamic_macro_factor_allocation(capital=capital),
        "Strategy 3 - Pure Business-Cycle Asset Rotation": run_pure_business_cycle_asset_rotation(capital=capital),
        "Strategy 4 - BlackRock Factor Replication": run_blackrock_factor_replication(capital=capital),
    }


def load_strategies_snapshot(capital, path=SNAPSHOT_PATH):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        snapshot = json.load(handle)
    if snapshot.get("version") != SNAPSHOT_VERSION or snapshot.get("capital") != capital:
        return None
    return snapshot.get("payloads")


def save_strategies_snapshot(payloads, capital, path=SNAPSHOT_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    snapshot = {
        "version": SNAPSHOT_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "capital": capital,
        "payloads": payloads,
    }
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, separators=(",", ":"), default=_json_default)
    os.replace(temporary_path, path)
    return payloads


def refresh_strategies_snapshot(capital):
    return save_strategies_snapshot(run_strategies_one_to_four(capital), capital)
