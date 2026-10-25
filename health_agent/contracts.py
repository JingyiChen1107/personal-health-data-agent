"""Typed tool boundaries with strict runtime validation, independent of an LLM SDK."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date
from typing import Literal, Mapping

from health_agent.config import DemoConfig

Status = Literal['ok', 'no_data', 'needs_clarification', 'insufficient_data',
                 'conflicting_events', 'unsupported', 'error']


class InputError(ValueError):
    def __init__(self, code: str, message: str, *, status: Status = 'error', details: dict | None = None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details or {}


def arguments(value, cls) -> dict:
    if isinstance(value, cls):
        result = {f.name: getattr(value, f.name) for f in fields(cls)}
    elif isinstance(value, Mapping):
        result = dict(value)
    else:
        raise InputError('invalid_arguments', '工具参数必须是JSON对象。')
    extra = set(result) - {f.name for f in fields(cls)}
    if extra:
        raise InputError('unknown_arguments', '参数不受支持；用户与会话身份只能由应用注入。',
                         details={'fields': sorted(str(key) for key in extra)})
    return result


def optional_text(value, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise InputError('invalid_argument', f'{name}必须是非空字符串，长度不超过200。')
    return value.strip()


def parse_date(value, name: str) -> date | None:
    value = optional_text(value, name)
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value or parsed.year >= 9999:
            raise ValueError()
        return parsed
    except ValueError:
        raise InputError('invalid_date', f'{name}必须是有效的YYYY-MM-DD日期（年份小于9999）。') from None


def validate_dates(start, end):
    left, right = parse_date(start, 'start_date'), parse_date(end, 'end_date')
    if left and right and left >= right:
        raise InputError('invalid_date_range', 'start_date必须早于排他的end_date。')


@dataclass(frozen=True)
class HealthQuery:
    indicator: str
    mode: Literal['latest', 'series', 'summary', 'compare_event'] = 'latest'
    start_date: str | None = None
    end_date: str | None = None
    measurement_context: str | None = None
    event_id: str | None = None
    window_days: int | None = None
    requested_unit: str | None = None

    @classmethod
    def parse(cls, value, config: DemoConfig) -> HealthQuery:
        raw = arguments(value, cls)
        if 'indicator' not in raw:
            raise InputError('missing_indicator', '请指定需要查询的指标。', status='needs_clarification', details={'missing_fields': ['indicator']})
        name = optional_text(raw['indicator'], 'indicator')
        aliases = {alias.casefold(): key for key, spec in config.indicators.items()
                   for alias in [key, *spec['aliases']]}
        aliases.update({key: 'blood_pressure' for key in ['blood_pressure', 'blood pressure', 'bp', '血压']})
        if name is None or name.casefold() not in aliases:
            raise InputError('unsupported_indicator', '首版只支持血糖、体重及血压。', status='unsupported')
        raw['indicator'] = aliases[name.casefold()]
        mode = raw.get('mode', 'latest')
        if not isinstance(mode, str) or mode not in ('latest', 'series', 'summary', 'compare_event'):
            raise InputError('invalid_mode', '健康查询mode必须是latest、series、summary或compare_event。')
        for key in ('start_date', 'end_date', 'measurement_context', 'event_id', 'requested_unit'):
            raw[key] = optional_text(raw.get(key), key)
        validate_dates(raw['start_date'], raw['end_date'])
        context = raw['measurement_context']
        valid_contexts = config.indicators.get(raw['indicator'], {'contexts': ['unknown']})['contexts']
        if context is not None and context not in valid_contexts:
            raise InputError('invalid_measurement_context', '测量条件与指标不匹配。', details={'allowed': valid_contexts})
        if mode == 'compare_event':
            if not raw['event_id']:
                raise InputError('missing_event', '请先通过用药工具确认要比较的变更事件。', status='needs_clarification', details={'missing_fields': ['event_id']})
            if raw['start_date'] or raw['end_date']:
                raise InputError('conflicting_arguments', 'compare_event使用event_id和window_days，不接受手工日期范围。')
            window = raw.get('window_days')
            if window is not None and (type(window) is not int or not 1 <= window <= 90):
                raise InputError('invalid_window', 'window_days必须为1至90的整数。')
        else:
            if raw['event_id'] or raw.get('window_days') is not None:
                raise InputError('conflicting_arguments', 'event_id和window_days只能用于compare_event。')
            if mode in ('series', 'summary') and not raw['start_date']:
                raise InputError('missing_date_range', '请指定时间范围的开始日期。', status='needs_clarification', details={'missing_fields': ['start_date']})
        return cls(**raw)


@dataclass(frozen=True)
class MedicationQuery:
    mode: Literal['current', 'history', 'events'] = 'current'
    medication_name: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    event_id: str | None = None
    event_type: str | None = None

    @classmethod
    def parse(cls, value) -> MedicationQuery:
        raw = arguments(value, cls)
        mode = raw.get('mode', 'current')
        if not isinstance(mode, str) or mode not in ('current', 'history', 'events'):
            raise InputError('invalid_mode', '用药查询mode必须是current、history或events。')
        for key in ('medication_name', 'start_date', 'end_date', 'event_id', 'event_type'):
            raw[key] = optional_text(raw.get(key), key)
        validate_dates(raw['start_date'], raw['end_date'])
        if raw['event_type'] is not None and raw['event_type'] not in ('start', 'stop', 'switch', 'dose_change'):
            raise InputError('invalid_event_type', '不支持的事件类型。')
        if mode != 'events' and (raw['event_id'] or raw['event_type']):
            raise InputError('conflicting_arguments', 'event_id和event_type只能用于events模式。')
        if mode == 'current' and (raw['start_date'] or raw['end_date']):
            raise InputError('conflicting_arguments', 'current使用应用参考日期；历史范围请使用history。')
        return cls(**raw)
