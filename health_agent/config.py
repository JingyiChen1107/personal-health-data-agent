"""Validated, explicit demo settings; no reliance on the host clock."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "health_agent.sqlite3"


@dataclass(frozen=True)
class DemoConfig:
    patient_id: str
    reference_date: date
    timezone: ZoneInfo
    comparison_window_days: int
    comparison_min_records: int
    max_tool_calls: int
    request_timeout_seconds: int
    max_retries: int
    rag_chunk_chars: int
    rag_overlap_chars: int
    rag_top_k: int
    indicators: dict
    mention_aliases: dict

    def utc_day_start(self, day: date) -> str:
        value = datetime.combine(day, time.min, tzinfo=self.timezone)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def comparison_bounds(self, event_day: date, days: int | None = None) -> tuple[str, ...]:
        window = self.comparison_window_days if days is None else days
        if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= 90:
            raise ValueError("window_days must be an integer between 1 and 90")
        return tuple(self.utc_day_start(day) for day in (
            event_day - timedelta(days=window), event_day,
            event_day + timedelta(days=1), event_day + timedelta(days=window + 1),
        ))


def load_config(path: Path | None = None) -> DemoConfig:
    raw = json.loads((path or PROJECT_ROOT / "config" / "demo.json").read_text(encoding="utf-8"))
    return DemoConfig(
        patient_id=raw["patient_id"], reference_date=date.fromisoformat(raw["reference_date"]),
        timezone=ZoneInfo(raw["timezone"]),
        comparison_window_days=raw["comparison"]["window_days"],
        comparison_min_records=raw["comparison"]["min_records_per_window"],
        max_tool_calls=raw["agent"]["max_tool_calls"],
        request_timeout_seconds=raw["agent"]["request_timeout_seconds"],
        max_retries=raw["agent"]["max_retries"],
        rag_chunk_chars=raw["rag"]["chunk_chars"],
        rag_overlap_chars=raw["rag"]["overlap_chars"], rag_top_k=raw["rag"]["top_k"],
        indicators=raw["indicators"], mention_aliases=raw["mention_aliases"],
    )
