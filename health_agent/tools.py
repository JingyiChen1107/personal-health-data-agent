"""Read-only parameterized health/medication tools. No LLM-generated SQL."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from health_agent.config import DemoConfig, load_config
from health_agent.contracts import HealthQuery, InputError, MedicationQuery
from health_agent.db import APPLICATION_ID, connect
from health_agent.results import ResultCache, ToolContext, ToolResult
from health_agent.statistics import difference, summarize

BP = ('systolic_blood_pressure', 'diastolic_blood_pressure')


class HealthTools:
    """Application binds context once; public arguments cannot override identity."""

    def __init__(self, database: Path, *, context: ToolContext,
                 config: DemoConfig | None = None, cache: ResultCache | None = None,
                 require_explicit_comparison_context: bool = False):
        self.require_explicit_comparison_context = require_explicit_comparison_context
        self.database = Path(database)
        self.context, self.config = context, config or load_config()
        self.cache = cache if cache is not None else ResultCache()

    def _execute(self, name, raw, parser, operation) -> dict:
        try:
            query = parser(raw)
            with connect(self.database, readonly=True) as con:
                con.execute('BEGIN')
                if (con.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                        or con.execute('PRAGMA user_version').fetchone()[0] != 1):
                    raise InputError('invalid_database', '数据库不属于本项目或结构版本不受支持。')
                if not con.execute('SELECT 1 FROM patients WHERE patient_id = ?', (self.context.patient_id,)).fetchone():
                    raise InputError('unknown_patient', '当前应用用户不存在。')
                result = operation(con, query)
        except InputError as exc:
            result = ToolResult(exc.status, data=exc.details,
                                error={'code': exc.code, 'message': str(exc)})
        except (sqlite3.Error, OSError):
            result = ToolResult('error', error={'code': 'database_error', 'message': '数据库不可用，请先初始化或检查项目数据库。'})
        except (ValueError, OverflowError):
            result = ToolResult('error', error={'code': 'invalid_data', 'message': '日期超出范围或记录格式无效。'})
        self.cache.put(result, context=self.context, tool=name)
        return result.to_dict()

    def query_health_data(self, arguments: dict | HealthQuery) -> dict:
        return self._execute('query_health_data', arguments,
                             lambda raw: HealthQuery.parse(raw, self.config), self._health)

    def query_medications(self, arguments: dict | MedicationQuery) -> dict:
        return self._execute('query_medications', arguments, MedicationQuery.parse, self._medications)

    def plot_health_trend(self, arguments):
        from health_agent.plotting import plot
        return plot(self, arguments)

    def search_health_documents(self, arguments):
        from health_agent.rag import search
        result = search(self, arguments)
        return result

    def _range(self, start: str | None, end: str | None) -> tuple[date | None, date, list[dict]]:
        cutoff = self.context.reference_date + timedelta(days=1)
        left = date.fromisoformat(start) if start else None
        right = date.fromisoformat(end) if end else cutoff
        warnings = []
        if right > cutoff:
            right = cutoff
            warnings.append({'code': 'reference_date_cutoff', 'message': '查询结束时间已限制为参考日结束。'})
        return left, right, warnings

    def _metadata(self, query, start, end) -> dict:
        return {'parameters': asdict(query), 'patient_id': self.context.patient_id,
                'reference_date': self.context.reference_date.isoformat(), 'timezone': str(self.config.timezone),
                'calendar_day_count': (end-start).days if start else None,
                'range': {'start_date': start.isoformat() if start else None, 'end_date_exclusive': end.isoformat(),
                          'start_utc': self.config.utc_day_start(start) if start else None,
                          'end_utc_exclusive': self.config.utc_day_start(end)}}

    def _records(self, con, indicators, start, end, context=None):
        if start and start >= end:
            return []
        clauses = ['patient_id = ?', 'indicator IN (' + ','.join('?' for _ in indicators) + ')', 'measured_at < ?']
        params = [self.context.patient_id, *indicators, self.config.utc_day_start(end)]
        if start:
            clauses.append('measured_at >= ?')
            params.append(self.config.utc_day_start(start))
        if context is not None:
            clauses.append('measurement_context = ?')
            params.append(context)
        sql = 'SELECT * FROM health_records WHERE ' + ' AND '.join(clauses) + ' ORDER BY measured_at, record_id'
        rows = [dict(row) for row in con.execute(sql, params)]
        for row in rows:
            spec = self.config.indicators[row['indicator']]
            if row['unit'] != spec['unit']:
                raise InputError('unit_conflict', '数据单位与标准单位不一致，未进行合并或换算。', status='unsupported')
            if row['measurement_context'] not in spec['contexts']:
                raise InputError('invalid_measurement_context', '数据库中的测量条件无效。')
            row['local_time'] = datetime.fromisoformat(row['measured_at']).astimezone(self.config.timezone).isoformat()
        return rows

    @staticmethod
    def _record_evidence(rows):
        return [{'source_type': 'health_record', 'source_id': row['record_id'],
                 'locator': {'table': 'health_records', 'record_id': row['record_id']},
                 'record': dict(row)} for row in rows]

    @staticmethod
    def _medication_evidence(rows):
        return [{'source_type': 'medication', 'source_id': row['medication_id'],
                 'locator': {'table': 'medications', 'medication_id': row['medication_id']},
                 'record': dict(row)} for row in rows]

    @staticmethod
    def _event_evidence(events):
        evidence, seen = [], set()
        for event in events:
            evidence.append({'source_type': 'medication_event', 'source_id': event['event_id'],
                             'locator': {'table': 'medication_events', 'event_id': event['event_id']},
                             'record': {key: value for key, value in event.items() if key not in ('old_medication', 'new_medication')}})
            for key in ('old_medication', 'new_medication'):
                med = event[key]
                if med is not None and med['medication_id'] not in seen:
                    seen.add(med['medication_id'])
                    evidence.extend(HealthTools._medication_evidence([med]))
        return evidence

    @staticmethod
    def _pair_records(rows):
        grouped = {}
        for row in rows:
            grouped.setdefault(row['measurement_id'], []).append(row)
        pairs = []
        for measurement_id, records in grouped.items():
            if (measurement_id is None or len(records) != 2 or {r['indicator'] for r in records} != set(BP)
                    or len({r['measured_at'] for r in records}) != 1):
                raise InputError('incomplete_bp_pair', '血压测量配对不完整或时间不一致，未拼接其他测量。')
            values = {r['indicator']: r['value'] for r in records}
            pairs.append({'measurement_id': measurement_id, 'measured_at': records[0]['measured_at'],
                          'local_time': records[0]['local_time'], 'unit': 'mmHg',
                          'systolic': values[BP[0]], 'diastolic': values[BP[1]],
                          'record_ids': [next(r['record_id'] for r in records if r['indicator'] == indicator) for indicator in BP]})
        return sorted(pairs, key=lambda p: (p['measured_at'], p['measurement_id']))

    def _health(self, con, query):
        indicators = BP if query.indicator == 'blood_pressure' else (query.indicator,)
        unit = self.config.indicators[indicators[0]]['unit']
        if query.requested_unit is not None and query.requested_unit != unit:
            raise InputError('unsupported_unit', '首版不支持单位换算。', status='unsupported',
                             details={'requested_unit': query.requested_unit, 'allowed_unit': unit})
        if query.mode == 'compare_event':
            return self._compare(con, query, indicators)
        start, end, warnings = self._range(query.start_date, query.end_date)
        data = self._metadata(query, start, end)
        rows = self._records(con, indicators, start, end, query.measurement_context)
        pairs = self._pair_records(rows) if query.indicator == 'blood_pressure' else []
        if query.mode == 'latest' and rows:
            if pairs:
                pairs = pairs[-1:]
                rows = [row for row in rows if row['measurement_id'] == pairs[0]['measurement_id']]
            else:
                rows = [max(rows, key=lambda r: (r['measured_at'], r['record_id']))]
        data.update({'records': rows, 'count': len(rows), 'unit': unit})
        if query.indicator == 'blood_pressure':
            data.update({'measurements': pairs, 'measurement_count': len(pairs)})
        if not rows:
            return ToolResult('no_data', data=data, warnings=warnings)
        contexts = sorted({r['measurement_context'] for r in rows})
        if query.indicator == 'glucose' and 'unknown' in contexts:
            warnings.append({'code': 'unknown_measurement_context', 'message': '部分记录的测量条件未知，不能表述为空腹。'})
        if query.mode == 'summary' and query.indicator == 'glucose' and len(contexts) > 1:
            data['available_contexts'] = contexts
            return ToolResult('needs_clarification', data=data, evidence=self._record_evidence(rows), warnings=warnings,
                              error={'code': 'ambiguous_measurement_context', 'message': '请选择要统计的测量条件，未合并计算。'})
        if query.mode == 'summary':
            data['summaries'] = [{'indicator': indicator, 'measurement_context': contexts[0],
                                  'unit': self.config.indicators[indicator]['unit'],
                                  'statistics': summarize([r for r in rows if r['indicator'] == indicator])}
                                 for indicator in indicators]
        elif len(contexts) > 1:
            warnings.append({'code': 'mixed_measurement_contexts', 'message': '记录包含不同测量条件，图表必须区分条件，不可合并统计。'})
        return ToolResult('ok', data=data, evidence=self._record_evidence(rows), warnings=warnings)

    def _events(self, con, start=None, end=None, name=None, event_id=None, event_type=None):
        clauses, params = ['e.patient_id = ?'], [self.context.patient_id]
        for condition, value in [('e.event_date >= ?', start), ('e.event_date < ?', end),
                                 ('e.event_id = ?', event_id), ('e.event_type = ?', event_type)]:
            if value is not None:
                clauses.append(condition)
                params.append(value)
        if name is not None:
            clauses.append('(old.medication_name = ? COLLATE NOCASE OR new.medication_name = ? COLLATE NOCASE)')
            params.extend([name, name])
        sql = '''SELECT e.* FROM medication_events e
            LEFT JOIN medications old ON old.medication_id=e.old_medication_id AND old.patient_id=e.patient_id
            LEFT JOIN medications new ON new.medication_id=e.new_medication_id AND new.patient_id=e.patient_id
            WHERE ''' + ' AND '.join(clauses) + ' ORDER BY e.event_date, e.event_id'
        events = [dict(row) for row in con.execute(sql, params)]
        for event in events:
            for side in ('old', 'new'):
                identifier = event[side + '_medication_id']
                med = con.execute('SELECT * FROM medications WHERE patient_id = ? AND medication_id = ?',
                                  (self.context.patient_id, identifier)).fetchone() if identifier else None
                event[side + '_medication'] = dict(med) if med is not None else None
        return events

    def _medications(self, con, query):
        start, end, warnings = self._range(query.start_date, query.end_date)
        data = self._metadata(query, start, end)
        if query.mode == 'events':
            events = [] if start and start >= end else self._events(
                con, start.isoformat() if start else None, end.isoformat(),
                query.medication_name, query.event_id, query.event_type)
            data['events'] = events
            return ToolResult('ok' if events else 'no_data', data=data,
                              evidence=self._event_evidence(events), warnings=warnings)
        clauses, params = ['patient_id = ?'], [self.context.patient_id]
        if query.mode == 'current':
            day = self.context.reference_date.isoformat()
            clauses += ['start_date <= ?', '(end_date IS NULL OR end_date > ?)']
            params.extend([day, day])
        else:
            clauses.append('start_date < ?')
            params.append(end.isoformat())
            if start:
                clauses.append('(end_date IS NULL OR end_date > ?)')
                params.append(start.isoformat())
        if query.medication_name is not None:
            clauses.append('medication_name = ? COLLATE NOCASE')
            params.append(query.medication_name)
        ordering = 'medication_id' if query.mode == 'current' else 'start_date, medication_id'
        rows = [] if start and start >= end else [dict(row) for row in con.execute(
            'SELECT * FROM medications WHERE ' + ' AND '.join(clauses) + ' ORDER BY ' + ordering, params)]
        data['medications'] = rows
        return ToolResult('ok' if rows else 'no_data', data=data,
                          evidence=self._medication_evidence(rows), warnings=warnings)

    def _compare(self, con, query, indicators):
        events = self._events(con, end=(self.context.reference_date + timedelta(days=1)).isoformat(), event_id=query.event_id)
        if not events:
            return ToolResult('no_data', data={'event_id': query.event_id},
                              error={'code': 'event_not_found', 'message': '当前用户在参考日期前没有该用药事件。'})
        event = events[0]
        event_day = date.fromisoformat(event['event_date'])
        window = query.window_days if query.window_days is not None else self.config.comparison_window_days
        # Includes the excluded event day, so simultaneous medication changes cannot be missed.
        days = (event_day - timedelta(days=window), event_day,
                event_day + timedelta(days=1), event_day + timedelta(days=window+1))
        bounds = self.config.comparison_bounds(event_day, window)
        evidence = self._event_evidence([event])
        data = {'parameters': asdict(query), 'event': event, 'window_days': window,
                'patient_id': self.context.patient_id, 'reference_date': self.context.reference_date.isoformat(),
                'timezone': str(self.config.timezone),
                'windows': {'before': {'start_date': days[0].isoformat(), 'end_date_exclusive': days[1].isoformat(),
                                       'start_utc': bounds[0], 'end_utc_exclusive': bounds[1]},
                            'after': {'start_date': days[2].isoformat(), 'end_date_exclusive': days[3].isoformat(),
                                      'start_utc': bounds[2], 'end_utc_exclusive': bounds[3]}},
                'comparisons': [], 'minimum_records_per_window': self.config.comparison_min_records}
        cutoff = self.context.reference_date + timedelta(days=1)
        conflicts = [row for row in self._events(con, days[0].isoformat(), min(days[3], cutoff).isoformat())
                     if row['event_id'] != event['event_id']]
        if conflicts:
            data['conflicting_events'] = conflicts
            combined = self._event_evidence([event, *conflicts])
            return ToolResult('conflicting_events', data=data, evidence=combined,
                              error={'code': 'conflicting_medication_events', 'message': '范围内还有其他用药变更，请缩短窗口或选择其他事件。'})
        if self.require_explicit_comparison_context and query.indicator == 'glucose' and query.measurement_context is None:
            data['missing_fields'] = ['measurement_context']
            return ToolResult('needs_clarification', data=data, evidence=evidence,
                              error={'code': 'missing_measurement_context', 'message': '请选择这次比较使用的血糖测量条件：空腹、餐后、随机或条件未知。'})
        before = self._records(con, indicators, days[0], min(days[1], cutoff), query.measurement_context)
        after = self._records(con, indicators, days[2], min(days[3], cutoff), query.measurement_context)
        if query.indicator == 'blood_pressure':
            self._pair_records(before)
            self._pair_records(after)
        data['total_record_count'] = len(before) + len(after)
        data['before'] = {'records': before, 'count': len(before)}
        data['after'] = {'records': after, 'count': len(after)}
        evidence.extend(self._record_evidence(before + after))
        contexts = sorted({r['measurement_context'] for r in before + after})
        if query.indicator == 'glucose' and len(contexts) > 1:
            data['available_contexts'] = contexts
            return ToolResult('needs_clarification', data=data, evidence=evidence,
                              error={'code': 'ambiguous_measurement_context', 'message': '请选择要比较的血糖测量条件，未混合计算。'})
        sufficient = bool(contexts) and days[3] <= cutoff
        for indicator in indicators:
            left = summarize([r for r in before if r['indicator'] == indicator])
            right = summarize([r for r in after if r['indicator'] == indicator])
            sufficient = sufficient and min(left['count'], right['count']) >= self.config.comparison_min_records
            data['comparisons'].append({'indicator': indicator, 'measurement_context': contexts[0] if contexts else query.measurement_context,
                                        'unit': self.config.indicators[indicator]['unit'], 'before': left, 'after': right,
                                        'delta': None, 'percent_change': None, 'display': None})
        data['window_complete'] = days[3] <= cutoff
        warnings = [{'code': 'descriptive_only', 'message': '比较仅描述记录的时间关联，不证明药物导致变化，不用于用药调整。'}]
        if 'unknown' in contexts and query.indicator == 'glucose':
            warnings.append({'code': 'unknown_measurement_context', 'message': '测量条件未知，仅对未知条件记录作描述性整理。'})
        if not sufficient:
            return ToolResult('insufficient_data', data=data, evidence=evidence, warnings=warnings,
                              error={'code': 'insufficient_comparison_data', 'message': '窗口尚未结束，或每窗同类记录不足3条，未给出变化结论。'})
        for item in data['comparisons']:
            item.update(difference(item['before'], item['after']))
            if item['percent_change'] is None:
                warnings.append({'code': 'zero_baseline', 'message': '前窗均值为0，百分比变化不可计算。', 'indicator': item['indicator']})
        return ToolResult('ok', data=data, evidence=evidence, warnings=warnings)
