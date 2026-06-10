import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from strategies.blackrock_factor_replication import run_backtest as run_strategy_4
from strategies.dynamic_macro_factor_allocation import run_backtest as run_strategy_2
from strategies.pure_business_cycle_asset_rotation import run_backtest as run_strategy_3
from strategies.regime_aware_etf_momentum import run_backtest as run_strategy_1


STRATEGIES = [
    ("strategy_1_regime_aware_etf_momentum", run_strategy_1),
    ("strategy_2_dynamic_macro_factor_allocation", run_strategy_2),
    ("strategy_3_pure_business_cycle_asset_rotation", run_strategy_3),
    ("strategy_4_blackrock_factor_replication", run_strategy_4),
]


def _cache_coverage(folder, suffix):
    coverage = []
    cache_dir = os.path.join(ROOT, "data", folder)
    for name in sorted(os.listdir(cache_dir)):
        if not name.endswith(suffix):
            continue
        path = os.path.join(cache_dir, name)
        try:
            if suffix == ".json":
                with open(path, "r", encoding="utf-8") as handle:
                    rows = json.load(handle)
                dates = [row.get("date") for row in rows if row.get("date")]
            else:
                frame = pd.read_csv(path)
                rows = frame.to_dict("records")
                dates = frame.iloc[:, 0].dropna().astype(str).tolist()
            coverage.append(
                {
                    "file": os.path.relpath(path, ROOT).replace("\\", "/"),
                    "rows": len(rows),
                    "start": min(dates) if dates else None,
                    "end": max(dates) if dates else None,
                    "updatedUtc": datetime.fromtimestamp(
                        os.path.getmtime(path), timezone.utc
                    ).isoformat(),
                }
            )
        except Exception as error:
            coverage.append(
                {
                    "file": os.path.relpath(path, ROOT).replace("\\", "/"),
                    "error": str(error),
                }
            )
    return coverage


def main():
    results = {}
    for name, runner in STRATEGIES:
        payload = runner()
        results[name] = {
            "mode": payload.get("mode"),
            "rows": payload.get("rows"),
            "latestRegime": payload.get("latestRegime"),
            "currentHoldings": payload.get("currentHoldings"),
            "performanceTable": payload.get("performanceTable"),
            "dataHealth": payload.get("dataHealth"),
        }

    manifest = {
        "generatedUtc": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "etfPrices": "Yahoo Finance chart API, adjusted close when available",
            "macro": "FRED CSV export with documented local fallbacks",
        },
        "strategies": results,
        "etfCache": _cache_coverage("etf_cache", ".json"),
        "fredCache": _cache_coverage("fred_cache", ".csv"),
        "limitationsFile": "DATA_LIMITATIONS.md",
    }
    manifest_path = os.path.join(ROOT, "data", "all_etf_strategies_data_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, default=str)

    print(json.dumps({"manifest": manifest_path, "strategies": list(results)}, indent=2))


if __name__ == "__main__":
    main()
