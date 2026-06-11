import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from strategy_snapshot import SNAPSHOT_PATH, refresh_strategies_snapshot


if __name__ == "__main__":
    refresh_strategies_snapshot(100000)
    print(f"Stored Strategies 1-4 dashboard snapshot at {SNAPSHOT_PATH}")
