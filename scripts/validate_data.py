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
TOPICS = {"fed_rates", "boj_yen", "geopolitics", "china", "global_risk"}


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
        if values.get("1W") is None:
            raise SystemExit(f"missing weekly value: {asset.get('key')}")
        for period, value in values.items():
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise SystemExit(f"invalid value: {asset.get('key')} {period}")
        history = asset.get("history", [])
        if len(history) < 2:
            raise SystemExit(f"insufficient history: {asset.get('key')}")
        for point in history:
            datetime.fromisoformat(point["date"])
            if not isinstance(point.get("value"), (int, float)) or not math.isfinite(point["value"]):
                raise SystemExit(f"invalid history: {asset.get('key')}")
    topics = payload.get("macroTopics", [])
    topic_keys = {topic.get("key") for topic in topics}
    if topic_keys != TOPICS:
        raise SystemExit("missing macro news topics")
    for topic in topics:
        if not isinstance(topic.get("items"), list):
            raise SystemExit(f"invalid topic items: {topic.get('key')}")
    print(f"validated {len(assets)} assets")


if __name__ == "__main__":
    main()
