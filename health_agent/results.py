"""JSON tool results and in-memory, session-owned evidence snapshots."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from time import monotonic
from typing import Callable
from uuid import uuid4

from health_agent.contracts import Status


@dataclass(frozen=True)
class ToolContext:
    patient_id: str
    session_id: str
    reference_date: date

    def __post_init__(self):
        if not isinstance(self.patient_id, str) or not self.patient_id.strip():
            raise ValueError('patient_id is required')
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError('session_id is required')
        if type(self.reference_date) is not date or self.reference_date.year >= 9999:
            raise ValueError('reference_date must be a date before year 9999')


@dataclass
class ToolResult:
    status: Status
    data: dict = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    error: dict | None = None
    result_id: str = field(default_factory=lambda: 'res_' + uuid4().hex)

    def to_dict(self) -> dict:
        return deepcopy({name: getattr(self, name) for name in
                         ('status', 'result_id', 'data', 'evidence', 'warnings', 'error')})


class ResultCache:
    """Process-local groundwork for Day 6; IDs cannot cross patients or sessions."""

    def __init__(self, ttl_seconds: float = 1800, *, clock: Callable[[], float] = monotonic):
        if isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
            raise ValueError('ttl_seconds must be positive')
        self.ttl_seconds, self.clock = ttl_seconds, clock
        self._entries: dict[str, dict] = {}

    def put(self, result: ToolResult, *, context: ToolContext, tool: str):
        now = self.clock()
        # Bound retained snapshots by removing expired entries on every write.
        self._entries = {key: value for key, value in self._entries.items() if value['expires_at'] > now}
        self._entries[result.result_id] = {
            'patient_id': context.patient_id, 'session_id': context.session_id,
            'reference_date': context.reference_date.isoformat(), 'tool': tool,
            'expires_at': now + self.ttl_seconds, 'result': result.to_dict(),
        }

    def get(self, result_id: str, *, context: ToolContext, tool: str | None = None) -> dict:
        entry = self._entries.get(result_id) if isinstance(result_id, str) else None
        if (entry is None or entry['expires_at'] <= self.clock()
                or entry['patient_id'] != context.patient_id or entry['session_id'] != context.session_id
                or entry['reference_date'] != context.reference_date.isoformat()
                or (tool is not None and tool != entry['tool'])):
            raise KeyError('invalid_result_id')
        return deepcopy(entry['result'])

    def clear(self, *, context):
        self._entries={key:value for key,value in self._entries.items() if not (
            value['patient_id']==context.patient_id and value['session_id']==context.session_id
            and value['reference_date']==context.reference_date.isoformat())}
