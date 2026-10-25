from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from unittest.mock import patch

from health_agent.config import PROJECT_ROOT, load_config
from health_agent.db import connect
from health_agent.seed import initialize_database
from health_agent.validation import validate_database


class Day1BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / 'demo.sqlite3'
        self.cfg = load_config()
        self.expected = json.loads((PROJECT_ROOT / 'tests/fixtures/expected.json').read_text(encoding='utf-8'))
        initialize_database(self.db)

    def rows(self, query, params=()):
        with connect(self.db, readonly=True) as con:
            return [dict(row) for row in con.execute(query, params)]

    def period(self, start, end, context='fasting'):
        return self.rows("""SELECT * FROM health_records WHERE patient_id = 'P001' AND indicator = 'glucose'
            AND measurement_context = ? AND measured_at >= ? AND measured_at < ? ORDER BY measured_at, record_id""",
            (context, start, end))

    def test_counts_and_integrity(self):
        report = validate_database(self.db)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['counts'], self.expected['counts'])

    def test_indicator_and_context_distribution(self):
        actual = {r['indicator']: r['n'] for r in self.rows('SELECT indicator, count(*) AS n FROM health_records GROUP BY indicator')}
        self.assertEqual(actual, self.expected['indicator_counts'])
        actual = {r['measurement_context']: r['n'] for r in self.rows("SELECT measurement_context, count(*) AS n FROM health_records WHERE indicator='glucose' GROUP BY measurement_context")}
        self.assertEqual(actual, self.expected['glucose_context_counts'])

    def test_latest_glucose_tie_and_unknown_context(self):
        row = self.rows("SELECT * FROM health_records WHERE indicator='glucose' ORDER BY measured_at DESC, record_id DESC LIMIT 1")[0]
        for field, value in self.expected['latest_glucose'].items():
            self.assertEqual(row[field], value)
        tied = self.rows('SELECT record_id FROM health_records WHERE measured_at=? AND indicator=? ORDER BY record_id', (row['measured_at'], 'glucose'))
        self.assertEqual([r['record_id'] for r in tied], ['G031', 'G032'])

    def test_latest_fasting_is_separate_from_unknown(self):
        row = self.rows("SELECT * FROM health_records WHERE indicator='glucose' AND measurement_context='fasting' ORDER BY measured_at DESC, record_id DESC LIMIT 1")[0]
        for field, value in self.expected['latest_fasting_glucose'].items():
            self.assertEqual(row[field], value)

    def test_comparison_windows_exactly_match_frozen_utc_bounds(self):
        bounds = self.cfg.comparison_bounds(date(2026, 10, 1))
        target = self.expected['comparison_E007_fasting_14d']
        self.assertEqual(list(bounds[:2]), target['pre_utc'])
        self.assertEqual(list(bounds[2:]), target['post_utc'])
        before = self.period(*bounds[:2])
        after = self.period(*bounds[2:])
        self.assertEqual([r['record_id'] for r in before], target['pre_ids'])
        self.assertEqual([r['record_id'] for r in after], target['post_ids'])

    def test_hand_checked_comparison_statistics(self):
        bounds = self.cfg.comparison_bounds(date(2026, 10, 1))
        target = self.expected['comparison_E007_fasting_14d']
        means = []
        for label, rows in [('pre', self.period(*bounds[:2])), ('post', self.period(*bounds[2:]))]:
            values = [Decimal(str(r['value'])) for r in rows]
            mean = sum(values) / len(values)
            means.append(mean)
            self.assertEqual(len(values), target[label]['count'])
            for key, value in [('sum', sum(values)), ('mean', mean), ('min', min(values)), ('max', max(values))]:
                self.assertEqual(value, Decimal(target[label][key]))
        delta = means[1] - means[0]
        self.assertEqual(delta, Decimal(target['delta']))
        pct = (delta / means[0] * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        self.assertEqual(str(pct), target['percent_change_display'])

    def test_short_window_is_insufficient_without_expanding(self):
        bounds = self.cfg.comparison_bounds(date(2026, 10, 1), 2)
        target = self.expected['comparison_E007_fasting_2d']
        for label, rows in [('pre', self.period(*bounds[:2])), ('post', self.period(*bounds[2:]))]:
            self.assertEqual([r['record_id'] for r in rows], target[f'{label}_ids'])
            self.assertLess(len(rows), self.cfg.comparison_min_records)

    def test_other_medication_events_exist_in_conflict_window(self):
        rows = self.rows("SELECT event_id FROM medication_events WHERE event_id <> 'E005' AND event_date >= '2026-08-19' AND event_date < '2026-09-17' ORDER BY event_date")
        self.assertEqual([r['event_id'] for r in rows], self.expected['comparison_E005_conflicts'])

    def test_current_medications_use_exclusive_end_date(self):
        for day in ['2026-10-01', '2026-10-23']:
            rows = self.rows('SELECT medication_id FROM medications WHERE start_date <= ? AND (end_date IS NULL OR ? < end_date) ORDER BY medication_id', (day, day))
            self.assertEqual([r['medication_id'] for r in rows], self.expected['current_medication_ids'])

    def test_weight_series_and_single_point(self):
        start = self.cfg.utc_day_start(date(2026, 10, 1))
        end = self.cfg.utc_day_start(date(2026, 10, 24))
        rows = self.rows("SELECT record_id FROM health_records WHERE indicator='weight' AND measured_at >= ? AND measured_at < ? ORDER BY measured_at", (start, end))
        self.assertEqual([r['record_id'] for r in rows], self.expected['october_weight_ids'])
        rows = self.rows("SELECT record_id FROM health_records WHERE indicator='weight' AND measured_at >= ? AND measured_at < ?", (self.cfg.utc_day_start(date(2026, 10, 20)), self.cfg.utc_day_start(date(2026, 10, 21))))
        self.assertEqual([r['record_id'] for r in rows], ['W016'])

    def test_blood_pressure_pairs_and_local_date(self):
        rows = self.rows("SELECT * FROM health_records WHERE measurement_id='BP024' ORDER BY indicator")
        self.assertEqual(len(rows), 2)
        values = {r['indicator']: r['value'] for r in rows}
        self.assertEqual(values, {'systolic_blood_pressure': 126, 'diastolic_blood_pressure': 82})
        self.assertEqual(rows[0]['measured_at'], self.expected['latest_bp']['measured_at'])
        local = datetime.fromisoformat(rows[0]['measured_at']).astimezone(self.cfg.timezone)
        self.assertEqual(local.isoformat(), self.expected['latest_bp']['local_time'])
        self.assertEqual(self.rows("SELECT count(*) AS n FROM health_records WHERE measurement_id IS NOT NULL")[0]['n'], 48)

    def test_missing_period_is_really_empty(self):
        rows = self.rows("SELECT record_id FROM health_records WHERE indicator='glucose' AND measured_at >= '2024-01-01T00:00:00Z' AND measured_at < '2025-01-01T00:00:00Z'")
        self.assertEqual(rows, [])

    def test_report_year_is_not_upload_year(self):
        rows = self.rows("SELECT report_id FROM reports WHERE report_date >= '2025-01-01' AND report_date < '2026-01-01' ORDER BY report_id")
        self.assertEqual([r['report_id'] for r in rows], self.expected['reports_2025'])
        self.assertEqual(self.rows("SELECT count(*) AS n FROM reports WHERE upload_time LIKE '2026-%'")[0]['n'], 4)

    def test_report_positive_negative_and_untrusted_fixtures(self):
        texts = {r['report_id']: (PROJECT_ROOT / r['file_path']).read_text(encoding='utf-8') for r in self.rows('SELECT * FROM reports')}
        self.assertIn(self.expected['ldl_mention_positive']['phrase'], texts['R001'])
        self.assertFalse(any(alias.casefold() in texts['R002'].casefold() for alias in self.cfg.mention_aliases['ldl']))
        self.assertIn('99 mmol/L', texts['R004'])
        self.assertFalse(self.rows("SELECT record_id FROM health_records WHERE indicator='glucose' AND value=99"))
        self.assertEqual({r['index_status'] for r in self.rows('SELECT index_status FROM reports')}, {'pending'})

    def test_schema_rejects_bad_units_duplicate_ids_and_unknown_patient(self):
        with connect(self.db) as con:
            original = dict(con.execute("SELECT * FROM health_records WHERE record_id='G001'").fetchone())
            variants = [dict(original, record_id='BADUNIT', unit='mg/dL'), dict(original), dict(original, record_id='BADPATIENT', patient_id='P999')]
            for row in variants:
                with self.subTest(row=row['record_id']):
                    with self.assertRaises(sqlite3.IntegrityError):
                        con.execute('INSERT INTO health_records VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))

    def test_reset_is_explicit_and_deterministic(self):
        before = self.rows('SELECT * FROM health_records ORDER BY record_id')
        with self.assertRaises(FileExistsError):
            initialize_database(self.db)
        initialize_database(self.db, reset=True)
        self.assertEqual(self.rows('SELECT * FROM health_records ORDER BY record_id'), before)

    def test_reset_refuses_unrelated_database(self):
        other = self.db.parent / 'unrelated.sqlite3'
        with sqlite3.connect(other) as con:
            con.execute('CREATE TABLE unrelated (id INTEGER)')
        old = other.read_bytes()
        with self.assertRaises(ValueError):
            initialize_database(other, reset=True)
        self.assertEqual(old, other.read_bytes())

    def test_failed_rebuild_preserves_existing_database(self):
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with patch('health_agent.validation.validate_database', return_value={'ok': False, 'errors': ['test failure']}):
            with self.assertRaises(ValueError):
                initialize_database(self.db, reset=True)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)

    def test_day_boundary_handles_daylight_saving(self):
        start = datetime.fromisoformat(self.cfg.utc_day_start(date(2026, 11, 1)))
        end = datetime.fromisoformat(self.cfg.utc_day_start(date(2026, 11, 2)))
        self.assertEqual((end - start).total_seconds(), 25 * 3600)

    def test_invalid_comparison_window_rejected(self):
        for value in [0, -1, 91, True, 2.5]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.cfg.comparison_bounds(date(2026, 10, 1), value)

    def test_twenty_agent_cases_are_specs_not_claimed_passes(self):
        suite = json.loads((PROJECT_ROOT / 'tests/fixtures/agent_cases.json').read_text(encoding='utf-8'))
        self.assertEqual(len(suite['cases']), 20)
        self.assertEqual(len({c['id'] for c in suite['cases']}), 20)
        self.assertEqual({c['execution_status'] for c in suite['cases']}, {'not_run'})
        counts = {}
        for case in suite['cases']:
            counts[case['group']] = counts.get(case['group'], 0) + 1
        self.assertEqual(counts, {'结构化查询': 5, '统计与图表': 4, '多工具比较': 4, '报告检索': 4, '追问与安全': 3})


if __name__ == '__main__':
    unittest.main()
