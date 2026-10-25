from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from health_agent.cli import main
from health_agent.config import PROJECT_ROOT, load_config
from health_agent.contracts import HealthQuery, MedicationQuery
from health_agent.db import connect
from health_agent.results import ResultCache, ToolContext
from health_agent.seed import initialize_database
from health_agent.tools import HealthTools


class Day2ToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / 'demo.sqlite3'
        initialize_database(self.db)
        self.config = load_config()
        self.context = ToolContext('P001', 'test-session', self.config.reference_date)
        self.tools = HealthTools(self.db, context=self.context, config=self.config)
        self.expected = json.loads((PROJECT_ROOT / 'tests/fixtures/expected.json').read_text(encoding='utf-8'))

    def comparison(self, **kwargs):
        return self.tools.query_health_data({'indicator': 'glucose', 'mode': 'compare_event',
                                            'event_id': 'E007', 'measurement_context': 'fasting', **kwargs})

    def insert(self, row):
        with connect(self.db) as con:
            with con:
                con.execute('INSERT INTO health_records (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))

    def original(self, record_id='G001'):
        with connect(self.db, readonly=True) as con:
            return dict(con.execute('SELECT * FROM health_records WHERE record_id=?', (record_id,)).fetchone())

    def test_t01_latest_has_unknown_context_and_record_evidence(self):
        result = self.tools.query_health_data({'indicator': '血糖'})
        self.assertEqual(result['status'], 'ok')
        row = result['data']['records'][0]
        for key, value in self.expected['latest_glucose'].items():
            self.assertEqual(row[key], value)
        self.assertEqual([e['source_id'] for e in result['evidence']], ['G032'])
        self.assertIn('unknown_measurement_context', [w['code'] for w in result['warnings']])

    def test_t02_exact_range_and_context(self):
        result = self.tools.query_health_data({'indicator': 'glucose', 'mode': 'series',
                                              'start_date': '2026-10-01', 'end_date': '2026-10-16',
                                              'measurement_context': 'fasting'})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual([r['record_id'] for r in result['data']['records']], self.expected['comparison_E007_fasting_14d']['post_ids'])
        self.assertEqual(result['data']['count'], 8)
        self.assertEqual(result['data']['range']['end_utc_exclusive'], '2026-10-16T05:00:00Z')

    def test_t03_same_timestamp_latest_tie_is_stable(self):
        result = self.tools.query_health_data({'indicator': 'glucose', 'mode': 'latest', 'start_date': '2026-10-22', 'end_date': '2026-10-23'})
        self.assertEqual(result['data']['records'][0]['record_id'], 'G032')

    def test_t04_unit_conversion_is_explicitly_unsupported(self):
        result = self.tools.query_health_data({'indicator': 'glucose', 'requested_unit': 'mg/dL'})
        self.assertEqual(result['status'], 'unsupported')
        self.assertEqual(result['data']['allowed_unit'], 'mmol/L')
        self.assertEqual(result['evidence'], [])

    def test_t05_empty_period_has_no_fabricated_values(self):
        result = self.tools.query_health_data({'indicator': 'glucose', 'mode': 'series', 'start_date': '2024-01-01', 'end_date': '2025-01-01'})
        self.assertEqual(result['status'], 'no_data')
        self.assertEqual(result['data']['records'], [])
        self.assertEqual(result['evidence'], [])

    def test_t06_weight_series_provides_seven_original_points(self):
        result = self.tools.query_health_data({'indicator': '体重', 'mode': 'series', 'start_date': '2026-10-01', 'end_date': '2026-10-24'})
        self.assertEqual([r['record_id'] for r in result['data']['records']], self.expected['october_weight_ids'])
        self.assertEqual(len(result['evidence']), 7)

    def test_t07_latest_blood_pressure_keeps_both_components(self):
        result = self.tools.query_health_data(HealthQuery('血压'))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data']['measurement_count'], 1)
        self.assertEqual(result['data']['count'], 2)
        self.assertEqual(result['data']['measurements'][0], self.expected['latest_bp'])

    def test_t08_single_point_is_preserved_without_inventing_more(self):
        result = self.tools.query_health_data({'indicator': 'weight', 'mode': 'series', 'start_date': '2026-10-20', 'end_date': '2026-10-21'})
        self.assertEqual(result['data']['count'], 1)
        self.assertEqual(result['data']['records'][0]['record_id'], 'W016')

    def test_t10_confirm_event_then_compare_matches_frozen_golden_values(self):
        meds = self.tools.query_medications({'mode': 'events', 'event_id': 'E007'})
        self.assertEqual(meds['data']['events'][0]['new_medication_id'], 'M002')
        result = self.comparison()
        target = self.expected['comparison_E007_fasting_14d']
        self.assertEqual(result['status'], 'ok')
        data, stats = result['data'], result['data']['comparisons'][0]
        for label, key in [('before', 'pre'), ('after', 'post')]:
            self.assertEqual([r['record_id'] for r in data[label]['records']], target[f'{key}_ids'])
            for field, value in target[key].items():
                self.assertEqual(stats[label][field], value)
        self.assertEqual(stats['delta'], target['delta'])
        self.assertEqual(stats['display']['percent_change'], '-11.95')
        self.assertEqual(data['windows']['before']['start_utc'], target['pre_utc'][0])
        self.assertEqual(data['windows']['after']['end_utc_exclusive'], target['post_utc'][1])
        sources = {e['source_id'] for e in result['evidence']}
        self.assertEqual(sources, {'E007', 'M001', 'M002', *target['pre_ids'], *target['post_ids']})

    def test_t11_candidates_are_returned_without_selecting_one(self):
        result = self.tools.query_medications({'mode': 'events', 'event_type': 'dose_change'})
        self.assertEqual([e['event_id'] for e in result['data']['events']], ['E004', 'E005', 'E006', 'E007'])
        result = self.tools.query_health_data({'indicator': 'glucose', 'mode': 'compare_event'})
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(result['data']['missing_fields'], ['event_id'])

    def test_t12_small_sample_does_not_produce_change(self):
        result = self.comparison(window_days=2)
        self.assertEqual(result['status'], 'insufficient_data')
        self.assertEqual(result['data']['before']['count'], 2)
        self.assertEqual(result['data']['after']['count'], 1)
        self.assertIsNone(result['data']['comparisons'][0]['delta'])

    def test_t13_conflicts_take_priority_over_mixed_contexts(self):
        result = self.tools.query_health_data({'indicator': 'glucose', 'mode': 'compare_event', 'event_id': 'E005'})
        self.assertEqual(result['status'], 'conflicting_events')
        self.assertEqual([e['event_id'] for e in result['data']['conflicting_events']], ['E004', 'E006'])
        self.assertEqual(result['data']['comparisons'], [])
        self.assertEqual(len([e for e in result['evidence'] if e['source_type'] == 'medication_event']), 3)

    def test_current_medications_and_exclusive_end_date(self):
        for day in (date(2026, 10, 1), date(2026, 10, 23)):
            tools = HealthTools(self.db, context=replace(self.context, reference_date=day))
            result = tools.query_medications({})
            self.assertEqual(result['status'], 'ok')
            self.assertEqual([r['medication_id'] for r in result['data']['medications']], self.expected['current_medication_ids'])

    def test_medication_history_uses_interval_overlap(self):
        result = self.tools.query_medications(MedicationQuery(mode='history', medication_name='二甲双胍', start_date='2026-10-01', end_date='2026-10-02'))
        self.assertEqual([m['medication_id'] for m in result['data']['medications']], ['M002'])
        previous = self.tools.query_medications({'mode': 'history', 'medication_name': '二甲双胍', 'start_date': '2026-09-30', 'end_date': '2026-10-01'})
        self.assertEqual([m['medication_id'] for m in previous['data']['medications']], ['M001'])

    def test_event_filter_matches_old_or_new_medication_name(self):
        result = self.tools.query_medications({'mode': 'events', 'medication_name': '二甲双胍', 'start_date': '2026-09-01'})
        self.assertEqual([e['event_id'] for e in result['data']['events']], ['E007'])
        self.assertEqual(result['data']['events'][0]['old_medication']['dose'], '500 mg')

    def test_sql_injection_in_values_cannot_expand_medication_query(self):
        result = self.tools.query_medications({'mode': 'events', 'medication_name': "' OR 1=1 --"})
        self.assertEqual(result['status'], 'no_data')
        self.assertEqual(result['data']['events'], [])

    def test_sql_injection_in_event_id_cannot_select_event(self):
        result = self.comparison(event_id="E007' OR 1=1 --")
        self.assertEqual(result['status'], 'no_data')

    def test_explicit_context_is_required_for_mixed_comparison(self):
        result = self.tools.query_health_data({'indicator': 'glucose', 'mode': 'compare_event', 'event_id': 'E007'})
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(result['data']['available_contexts'], ['fasting', 'postprandial', 'random', 'unknown'])
        self.assertEqual(result['data']['comparisons'], [])

    def test_summary_never_merges_glucose_contexts(self):
        result = self.tools.query_health_data({'indicator': 'glucose', 'mode': 'summary', 'start_date': '2026-09-01'})
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertNotIn('summaries', result['data'])

    def test_fasting_summary_is_deterministic(self):
        result = self.tools.query_health_data({'indicator': 'glucose', 'mode': 'summary', 'start_date': '2026-09-17', 'end_date': '2026-10-01', 'measurement_context': 'fasting'})
        summary = result['data']['summaries'][0]['statistics']
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(summary['mean'], '7.95')
        self.assertEqual(summary['display']['mean'], '7.95')

    def test_health_input_validation_is_strict_and_json_serializable(self):
        cases = [
            ([], 'error'), ({}, 'needs_clarification'), ({'indicator': 'LDL'}, 'unsupported'),
            ({'indicator': 'weight', 'patient_id': 'P999'}, 'error'),
            ({'indicator': 'weight', 'mode': 'series'}, 'needs_clarification'),
            ({'indicator': 'weight', 'start_date': '2026-02-30'}, 'error'),
            ({'indicator': 'weight', 'end_date': '20261024'}, 'error'),
            ({'indicator': 'weight', 'start_date': '2026-10-02', 'end_date': '2026-10-01'}, 'error'),
            ({'indicator': 'weight', 'mode': []}, 'error'),
            ({'indicator': 'weight', 'measurement_context': 'fasting'}, 'error'),
            ({'indicator': 'weight', 'event_id': 'E007'}, 'error'),
            ({'indicator': 'weight', 'start_date': True}, 'error'),
            ({'indicator': 'weight', 'indicator_sql': 'DROP TABLE patients'}, 'error'),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                result = self.tools.query_health_data(args)
                self.assertEqual(result['status'], expected)
                self.assertEqual(set(result), {'status', 'result_id', 'data', 'evidence', 'warnings', 'error'})
                json.dumps(result, allow_nan=False)

    def test_comparison_window_and_conflicting_arguments_rejected(self):
        for window in [0, 91, -2, True, 2.5, '14']:
            with self.subTest(window=window):
                self.assertEqual(self.comparison(window_days=window)['status'], 'error')
        self.assertEqual(self.comparison(start_date='2026-09-01')['status'], 'error')

    def test_medication_argument_validation(self):
        for args in [None, {'mode': []}, {'mode': 'bogus'}, {'patient_id': 'P999'},
                     {'mode': 'current', 'start_date': '2026-01-01'},
                     {'mode': 'history', 'event_id': 'E007'}, {'mode': 'events', 'event_type': 'bogus'},
                     {'mode': 'events', 'end_date': '2026-02-30'}]:
            with self.subTest(args=args):
                self.assertEqual(self.tools.query_medications(args)['status'], 'error')

    def test_future_records_are_excluded_from_latest(self):
        self.insert(dict(self.original(), record_id='G999', value=99.0, measured_at='2026-10-25T13:00:00Z'))
        result = self.tools.query_health_data({'indicator': 'glucose'})
        self.assertEqual(result['data']['records'][0]['record_id'], 'G032')
        result = self.tools.query_health_data({'indicator': 'glucose', 'end_date': '2026-12-01'})
        self.assertEqual(result['data']['records'][0]['record_id'], 'G032')
        self.assertIn('reference_date_cutoff', [w['code'] for w in result['warnings']])

    def test_incomplete_post_window_cannot_imply_complete_comparison(self):
        tools = HealthTools(self.db, context=replace(self.context, reference_date=date(2026, 10, 10)))
        result = tools.query_health_data({'indicator': 'glucose', 'mode': 'compare_event', 'event_id': 'E007', 'measurement_context': 'fasting'})
        self.assertEqual(result['status'], 'insufficient_data')
        self.assertFalse(result['data']['window_complete'])
        self.assertGreaterEqual(result['data']['after']['count'], 3)
        self.assertIsNone(result['data']['comparisons'][0]['delta'])

    def test_same_day_other_medication_event_is_a_conflict(self):
        with connect(self.db) as con:
            with con:
                con.execute("INSERT INTO medication_events VALUES ('E008', 'P001', '2026-10-01', 'stop', 'M003', NULL)")
        result = self.comparison()
        self.assertEqual(result['status'], 'conflicting_events')
        self.assertEqual([e['event_id'] for e in result['data']['conflicting_events']], ['E008'])

    def test_other_patient_event_cannot_be_selected_or_counted_as_conflict(self):
        with connect(self.db) as con:
            with con:
                con.execute("INSERT INTO patients VALUES ('P002', 'other synthetic patient', 1)")
                con.execute("INSERT INTO medications VALUES ('OTHER_M', 'P002', 'test', '1 mg', 'test', '2026-10-01', NULL)")
                con.execute("INSERT INTO medication_events VALUES ('OTHER_E', 'P002', '2026-10-01', 'start', NULL, 'OTHER_M')")
        self.assertEqual(self.comparison(event_id='OTHER_E')['status'], 'no_data')
        self.assertEqual(self.comparison()['status'], 'ok')

    def test_unknown_application_patient_cannot_read_default_patient(self):
        tools = HealthTools(self.db, context=replace(self.context, patient_id='P999'))
        result = tools.query_health_data({'indicator': 'glucose'})
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['evidence'], [])

    def test_no_post_data_produces_null_statistics_not_zeros(self):
        with connect(self.db) as con:
            with con:
                con.execute("DELETE FROM health_records WHERE indicator='glucose' AND measured_at >= '2026-10-02T05:00:00Z'")
        result = self.comparison()
        stats = result['data']['comparisons'][0]['after']
        self.assertEqual(result['status'], 'insufficient_data')
        self.assertEqual(stats['count'], 0)
        self.assertIsNone(stats['mean'])
        self.assertIsNone(stats['sum'])

    def test_zero_baseline_returns_absolute_change_but_no_percentage(self):
        with connect(self.db) as con:
            with con:
                con.execute("UPDATE health_records SET value=0 WHERE indicator='glucose' AND measurement_context='fasting' AND measured_at < '2026-10-01T05:00:00Z'")
        result = self.comparison()
        self.assertEqual(result['status'], 'ok')
        stats = result['data']['comparisons'][0]
        self.assertEqual(stats['display']['delta'], '7.00')
        self.assertIsNone(stats['percent_change'])
        self.assertIn('zero_baseline', [w['code'] for w in result['warnings']])

    def test_incomplete_bp_pair_is_not_silently_mixed(self):
        with connect(self.db) as con:
            with con:
                con.execute("DELETE FROM health_records WHERE record_id='DBP024'")
        result = self.tools.query_health_data({'indicator': 'blood_pressure'})
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error']['code'], 'incomplete_bp_pair')

    def test_blood_pressure_summary_separates_components(self):
        result = self.tools.query_health_data({'indicator': 'blood_pressure', 'mode': 'summary', 'start_date': '2026-10-23'})
        self.assertEqual(result['status'], 'ok')
        values = {s['indicator']: s['statistics']['display']['mean'] for s in result['data']['summaries']}
        self.assertEqual(values, {'systolic_blood_pressure': '125.50', 'diastolic_blood_pressure': '81.50'})

    def test_missing_database_does_not_create_empty_file(self):
        missing = self.db.parent / 'missing.sqlite3'
        result = HealthTools(missing, context=self.context).query_health_data({'indicator': 'glucose'})
        self.assertEqual(result['status'], 'error')
        self.assertFalse(missing.exists())

    def test_unrelated_database_is_rejected(self):
        unrelated = self.db.parent / 'unrelated.sqlite3'
        with sqlite3.connect(unrelated) as con:
            con.execute('CREATE TABLE demo (id INTEGER)')
        result = HealthTools(unrelated, context=self.context).query_medications({})
        self.assertEqual(result['error']['code'], 'invalid_database')

    def test_event_day_values_do_not_enter_either_comparison_window(self):
        self.insert(dict(self.original(), record_id='EVENT_DAY', value=99.0, measured_at='2026-10-01T13:00:00Z'))
        result = self.comparison()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data']['comparisons'][0]['delta'], '-0.95')
        self.assertNotIn('EVENT_DAY', [e['source_id'] for e in result['evidence']])

    def test_exclusive_end_date_does_not_include_boundary_measurement(self):
        result = self.tools.query_health_data({'indicator': 'weight', 'mode': 'series', 'start_date': '2026-10-17', 'end_date': '2026-10-20'})
        self.assertEqual([r['record_id'] for r in result['data']['records']], ['W015'])

    def test_blood_pressure_comparison_is_separate_for_each_component(self):
        result = self.tools.query_health_data({'indicator': 'blood_pressure', 'mode': 'compare_event', 'event_id': 'E007'})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['data']['comparisons']), 2)
        self.assertEqual({item['indicator'] for item in result['data']['comparisons']}, {'systolic_blood_pressure', 'diastolic_blood_pressure'})
        for item in result['data']['comparisons']:
            self.assertGreaterEqual(item['before']['count'], 3)
            self.assertGreaterEqual(item['after']['count'], 3)
            self.assertEqual(item['unit'], 'mmHg')

    def test_future_medication_event_is_not_returned(self):
        with connect(self.db) as con:
            with con:
                con.execute("INSERT INTO medications VALUES ('FUTURE_M', 'P001', 'test', '1 mg', 'test', '2026-10-25', NULL)")
                con.execute("INSERT INTO medication_events VALUES ('FUTURE_E', 'P001', '2026-10-25', 'start', NULL, 'FUTURE_M')")
        result = self.tools.query_medications({'mode': 'events', 'end_date': '2027-01-01'})
        self.assertNotIn('FUTURE_E', [e['event_id'] for e in result['data']['events']])
        self.assertEqual(self.comparison(event_id='FUTURE_E')['status'], 'no_data')

    def test_actual_foreign_patient_health_rows_are_not_returned(self):
        with connect(self.db) as con:
            with con:
                con.execute("INSERT INTO patients VALUES ('P002', 'other synthetic patient', 1)")
        self.insert(dict(self.original(), record_id='FOREIGN_G', patient_id='P002', value=88.0, measured_at='2026-10-23T13:00:00Z'))
        result = self.tools.query_health_data({'indicator': 'glucose'})
        self.assertEqual(result['data']['records'][0]['record_id'], 'G032')

    def test_result_ids_are_unique_and_both_queries_are_cached(self):
        first = self.tools.query_health_data({'indicator': 'glucose'})
        second = self.tools.query_medications({})
        self.assertNotEqual(first['result_id'], second['result_id'])
        self.assertEqual(self.tools.cache.get(second['result_id'], context=self.context, tool='query_medications'), second)

    def test_result_cache_owns_patient_session_reference_date_and_tool(self):
        result = self.tools.query_health_data({'indicator': 'weight'})
        cached = self.tools.cache.get(result['result_id'], context=self.context, tool='query_health_data')
        self.assertEqual(cached, result)
        for context in [replace(self.context, session_id='other'), replace(self.context, patient_id='P002'),
                        replace(self.context, reference_date=date(2026, 10, 22))]:
            with self.subTest(context=context), self.assertRaises(KeyError):
                self.tools.cache.get(result['result_id'], context=context)
        with self.assertRaises(KeyError):
            self.tools.cache.get(result['result_id'], context=self.context, tool='query_medications')

    def test_result_cache_snapshots_cannot_be_mutated_by_caller(self):
        result = self.tools.query_health_data({'indicator': 'weight'})
        result['data']['records'][0]['value'] = 999
        cached = self.tools.cache.get(result['result_id'], context=self.context)
        self.assertEqual(cached['data']['records'][0]['value'], 77.5)
        cached['data']['records'].clear()
        self.assertEqual(len(self.tools.cache.get(result['result_id'], context=self.context)['data']['records']), 1)

    def test_expired_or_invented_result_id_is_rejected(self):
        now = [10.0]
        cache = ResultCache(ttl_seconds=5, clock=lambda: now[0])
        tools = HealthTools(self.db, context=self.context, cache=cache)
        result = tools.query_health_data({'indicator': 'weight'})
        now[0] = 15.0
        for identifier in [result['result_id'], 'res_invented', [], None]:
            with self.subTest(identifier=identifier), self.assertRaises(KeyError):
                cache.get(identifier, context=self.context)

    def test_cli_returns_json_for_query_and_invalid_json(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(['health', '--db', str(self.db), '--args', '{"indicator":"glucose"}'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())['data']['records'][0]['record_id'], 'G032')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(['medications', '--db', str(self.db), '--args', '{invalid'])
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(output.getvalue())['ok'])


if __name__ == '__main__':
    unittest.main()
