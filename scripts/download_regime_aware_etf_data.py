import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from strategies.regime_aware_etf_momentum import DEFAULT_CONFIG, run_backtest


def main():
    payload = run_backtest()
    manifest = {
        "mode": payload["mode"],
        "rows": payload["rows"],
        "latestRegime": payload["latestRegime"],
        "currentHoldings": payload["currentHoldings"],
        "dataHealth": payload["dataHealth"],
    }
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
