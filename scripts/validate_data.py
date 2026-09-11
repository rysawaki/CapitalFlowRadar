#!/usr/bin/env python3
"""Validate the public snapshot before it is committed or deployed."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "dist" / "data.json"
REQUIRED = {"sp500", "nasdaq", "oil", "copper", "gold", "yen", "ust10y", "japan", "bitcoin"}
PERIODS = {"1D", "1W", "4W"}


def main() -> None:
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 1:
        raise SystemExit("unsupported schemaVersion")
    datetime.fromisoformat(payload["generatedAt"].replace("Z", "+00:00"))
    assets = payload.get("assets", [])
    keys = [asset.get("key") for asset in assets]
    if len(keys) != len(set(keys)):
        raise SystemExit("duplicate asset keys")
    missing = REQUIRED - set(keys)
    if missing:
        raise SystemExit(f"missing assets: {', '.join(sorted(missing))}")
    for asset in assets:
        values = asset.get("values", {})
        if not PERIODS.issubset(values):
            raise SystemExit(f"missing periods: {asset.get('key')}")
        for period, value in values.items():
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise SystemExit(f"invalid value: {asset.get('key')} {period}")
    print(f"validated {len(assets)} assets")


if __name__ == "__main__":
    main()
