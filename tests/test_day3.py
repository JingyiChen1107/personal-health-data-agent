from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from health_agent.agent import HealthAgent
from health_agent.config import load_config
from health_agent.provider import ModelReply, ProviderError
from health_agent.results import ToolContext
from health_agent.seed import initialize_database
from health_agent.tool_specs import tool_specs
from health_agent.tools import HealthTools
from tests.scripted_provider import ScriptedProvider, call, final


class Day3AgentContractTests(unittest.TestCase):
    """Offline orchestration tests, not evidence of live-model routing quality."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'demo.sqlite3'
        initialize_database(self.db)
        self.cfg = load_config()
        self.context = ToolContext('P001', 'contract-session', self.cfg.reference_date)
        self.tools = HealthTools(self.db, context=self.context)

    def run_replies(self, replies, question='最近一次血糖是多少？', tools=None):
        provider = ScriptedProvider(replies)
        agent = HealthAgent(tools or self.tools, provider, retry_sleep=lambda _: None)
        result = agent.run(question)
        return result, provider

    def test_single_tool_loop_queries_real_database_and_returns_evidence(self):
        result, provider = self.run_replies([
            call('query_health_data', {'indicator': 'glucose'}),
            final('最近一次血糖为6.9 mmol/L，测量条件未知。[G032]', ['G032']),
        ])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['tool_calls'], 1)
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual(result['tool_results'][0]['data']['records'][0]['value'], 6.9)
        output = next(item for item in provider.requests[1]['messages'] if item.get('type') == 'function_call_output')
        self.assertEqual(output['call_id'], 'call_1')
        self.assertEqual(json.loads(output['output'])['data']['records'][0]['record_id'], 'G032')
        self.assertEqual(result['usage']['total_tokens'], 30)

    def test_multi_tool_loop_requires_prior_event_query(self):
        result, provider = self.run_replies([
            call('query_medications', {'mode': 'events', 'event_id': 'E007'}, 'med_1'),
            call('query_health_data', {'indicator': 'glucose', 'mode': 'compare_event', 'event_id': 'E007', 'measurement_context': 'fasting'}, 'health_1'),
            final('前后均值7.95和7.0 mmol/L，变化−11.95%，仅为时间关联。[E007][G001][G009]', ['E007', 'G001', 'G009']),
        ], '比较2026年10月1日二甲双胍调整前后空腹血糖。')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual([row['tool'] for row in result['trace']], ['query_medications', 'query_health_data'])
        self.assertEqual(result['tool_calls'], 2)
        self.assertEqual(result['tool_results'][1]['data']['comparisons'][0]['display']['percent_change'], '-11.95')

    def test_comparison_without_prior_event_result_is_rejected(self):
        result, _ = self.run_replies([
            call('query_health_data', {'indicator': 'glucose', 'mode': 'compare_event', 'event_id': 'E007', 'measurement_context': 'fasting'}),
            final('需要先查询确认用药事件，请说明要比较的日期。', clarify=True),
        ])
        self.assertEqual(result['trace'][0]['error_code'], 'unconfirmed_event')
        self.assertEqual(result['status'], 'needs_clarification')

    def test_same_batch_cannot_pretend_model_read_prior_tool_result(self):
        meds = call('query_medications', {'mode': 'events', 'event_id': 'E007'}, 'med_1')
        health = call('query_health_data', {'indicator': 'glucose', 'mode': 'compare_event', 'event_id': 'E007', 'measurement_context': 'fasting'}, 'health_1')
        batch = ModelReply(meds.output_items + health.output_items, meds.calls + health.calls)
        result, _ = self.run_replies([batch, final('已取得事件，尚未完成比较。[E007]', ['E007'])])
        self.assertEqual(result['trace'][1]['error_code'], 'unconfirmed_event')
        self.assertNotIn('comparisons', result['tool_results'][1]['data'])

    def test_one_argument_correction_allowed(self):
        result, _ = self.run_replies([
            call('query_health_data', '{bad', 'bad_1'),
            call('query_health_data', {'indicator': 'glucose'}, 'good_1'),
            final('记录是6.9 mmol/L。[G032]', ['G032']),
        ])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['tool_calls'], 2)
        self.assertEqual(result['trace'][0]['error_code'], 'invalid_tool_arguments')

    def test_second_tool_error_stops_loop(self):
        result, provider = self.run_replies([
            call('query_health_data', '{bad', 'bad_1'),
            call('query_health_data', {'indicator': 'weight', 'patient_id': 'P002'}, 'bad_2'),
        ])
        self.assertEqual(result['error']['code'], 'tool_correction_limit')
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(len(result['trace']), 2)

    def test_unknown_tool_is_not_executed(self):
        result, _ = self.run_replies([
            call('execute_sql', {'sql': 'DROP TABLE patients'}),
            final('该操作不受支持。', clarify=True),
        ])
        self.assertEqual(result['trace'][0]['error_code'], 'unknown_tool')
        self.assertEqual(result['trace'][0]['arguments'], {})
        self.assertEqual(self.tools.query_health_data({'indicator': 'glucose'})['status'], 'ok')

    def test_nan_and_array_tool_arguments_are_rejected(self):
        for invalid in ['[]', '{"window_days":NaN}', 'null']:
            with self.subTest(invalid=invalid):
                result, _ = self.run_replies([call('query_health_data', invalid), final('请补充有效条件。', clarify=True)])
                self.assertEqual(result['trace'][0]['error_code'], 'invalid_tool_arguments')

    def test_tool_call_limit_bounds_real_executions(self):
        cfg = replace(self.cfg, max_tool_calls=2)
        tools = HealthTools(self.db, context=self.context, config=cfg)
        result, provider = self.run_replies([
            call('query_health_data', {'indicator': 'glucose'}, 'a'),
            call('query_health_data', {'indicator': 'weight'}, 'b'),
            call('query_medications', {}, 'c'),
        ], tools=tools)
        self.assertEqual(result['error']['code'], 'tool_call_limit')
        self.assertEqual(result['tool_calls'], 2)
        self.assertEqual(len(result['tool_results']), 2)

    def test_batch_cannot_bypass_tool_call_limit(self):
        cfg = replace(self.cfg, max_tool_calls=1)
        tools = HealthTools(self.db, context=self.context, config=cfg)
        a, b = call('query_health_data', {'indicator': 'glucose'}, 'a'), call('query_medications', {}, 'b')
        result, _ = self.run_replies([ModelReply(a.output_items+b.output_items, a.calls+b.calls)], tools=tools)
        self.assertEqual(result['tool_calls'], 1)
        self.assertEqual(result['error']['code'], 'tool_call_limit')

    def test_retryable_model_error_retried_once_before_any_tool_execution(self):
        result, provider = self.run_replies([
            ProviderError('connection_failed', '连接失败', retryable=True),
            call('query_health_data', {'indicator': 'glucose'}),
            final('6.9 mmol/L。[G032]', ['G032']),
        ])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['retries'], 1)
        self.assertEqual(result['model_calls'], 3)
        self.assertEqual(result['tool_calls'], 1)
        self.assertEqual(provider.requests[0], provider.requests[1])

    def test_repeated_network_failure_stops_without_fake_answer(self):
        error = ProviderError('connection_failed', '连接失败', retryable=True)
        result, provider = self.run_replies([error, error])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['tool_calls'], 0)
        self.assertEqual(result['retries'], 1)
        self.assertEqual(len(provider.requests), 2)

    def test_auth_failure_not_retried(self):
        result, provider = self.run_replies([ProviderError('authentication_failed', '认证失败')])
        self.assertEqual(result['error']['code'], 'authentication_failed')
        self.assertEqual(len(provider.requests), 1)

    def test_bad_final_source_id_cannot_escape_to_user(self):
        result, _ = self.run_replies([
            call('query_health_data', {'indicator': 'glucose'}),
            final('伪造来源。[FAKE]', ['FAKE']), final('仍伪造。[FAKE]', ['FAKE']),
        ])
        self.assertEqual(result['error']['code'], 'invalid_final_response')
        self.assertNotIn('伪造', result['answer'])
        self.assertEqual(len(result['tool_results']), 1)

    def test_final_source_can_be_corrected_once(self):
        result, _ = self.run_replies([
            call('query_health_data', {'indicator': 'glucose'}),
            final('错误引用。[FAKE]', ['FAKE']), final('6.9 mmol/L。[G032]', ['G032']),
        ])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['citations'], ['G032'])

    def test_personal_answer_after_query_requires_nonempty_citations(self):
        result, _ = self.run_replies([
            call('query_health_data', {'indicator': 'glucose'}), final('6.9 mmol/L。'), final('6.9 mmol/L。'),
        ])
        self.assertEqual(result['status'], 'error')

    def test_no_data_can_be_explained_without_fabricating_citations(self):
        result, _ = self.run_replies([
            call('query_health_data', {'indicator': 'glucose', 'mode': 'series', 'start_date': '2024-01-01', 'end_date': '2025-01-01'}),
            final('指定范围没有记录。'),
        ])
        self.assertEqual(result['tool_results'][0]['status'], 'no_data')
        self.assertEqual(result['citations'], [])

    def test_conflicting_events_can_be_returned_without_statistics(self):
        result, _ = self.run_replies([
            call('query_medications', {'mode': 'events', 'event_id': 'E005'}, 'a'),
            call('query_health_data', {'indicator': 'glucose', 'mode': 'compare_event', 'event_id': 'E005'}, 'b'),
            final('该窗口还有其他用药变更，不能据此比较。[E005][E004][E006]', ['E005', 'E004', 'E006']),
        ])
        self.assertEqual(result['tool_results'][1]['status'], 'conflicting_events')
        self.assertEqual(result['tool_results'][1]['data']['comparisons'], [])

    def test_only_registered_tools_sent_and_identity_not_exposed_in_schema(self):
        result, provider = self.run_replies([final()])
        specs = provider.requests[0]['tools']
        self.assertEqual({t['name'] for t in specs}, {'query_health_data', 'query_medications', 'search_health_documents', 'plot_health_trend'})
        for spec in specs:
            schema = spec['parameters']
            self.assertTrue(spec['strict'])
            self.assertFalse(schema['additionalProperties'])
            self.assertEqual(set(schema['properties']), set(schema['required']))
            self.assertNotIn('patient_id', schema['properties'])
            self.assertNotIn('session_id', schema['properties'])

    def test_system_prompt_contains_reference_date_and_scope(self):
        result, provider = self.run_replies([final()])
        prompt = provider.requests[0]['instructions']
        self.assertIn('2026-10-23', prompt)
        self.assertIn('America/Chicago', prompt)
        self.assertIn('不诊断', prompt)
        self.assertIn('本地文档RAG', prompt)
        self.assertIn('plot_health_trend', prompt)

    def test_each_turn_does_not_inherit_other_turn_evidence(self):
        provider = ScriptedProvider([
            call('query_health_data', {'indicator': 'glucose'}), final('6.9。[G032]', ['G032']),
            final('上次是6.9。[G032]', ['G032']), final('仍然引用。[G032]', ['G032']),
        ])
        agent = HealthAgent(self.tools, provider)
        self.assertEqual(agent.run('查血糖')['status'], 'ok')
        second = agent.run('再说一次')
        self.assertEqual(second['error']['code'], 'invalid_final_response')
        self.assertEqual(second['tool_results'], [])

    def test_invalid_user_question_does_not_call_provider(self):
        provider = ScriptedProvider([])
        agent = HealthAgent(self.tools, provider)
        for question in ['', '  ', None, 'a'*4001]:
            self.assertEqual(agent.run(question)['error']['code'], 'invalid_question')
        self.assertEqual(provider.requests, [])

    def test_status_events_contain_no_hidden_model_text(self):
        events = []
        provider = ScriptedProvider([call('query_health_data', {'indicator': 'glucose'}), final('6.9。[G032]', ['G032'])])
        result = HealthAgent(self.tools, provider, event_sink=events.append).run('查血糖')
        self.assertEqual([e['event'] for e in events], ['model_started', 'tool_started', 'tool_finished', 'model_started'])
        self.assertFalse(any('messages' in event or 'reasoning' in event for event in events))

    def test_reasoning_items_preserved_for_protocol_but_not_returned_in_trace(self):
        reply = call('query_health_data', {'indicator': 'glucose'})
        reply.output_items.insert(0, {'type': 'reasoning', 'id': 'r1', 'encrypted_content': 'opaque', 'summary': []})
        result, provider = self.run_replies([reply, final('6.9。[G032]', ['G032'])])
        self.assertTrue(any(item.get('type') == 'reasoning' for item in provider.requests[1]['messages']))
        self.assertNotIn('encrypted_content', json.dumps(result))

    def test_reused_call_id_across_decisions_is_not_executed_twice(self):
        result, _ = self.run_replies([
            call('query_health_data', {'indicator': 'glucose'}, 'same'),
            call('query_health_data', {'indicator': 'weight'}, 'same'),
        ])
        self.assertEqual(result['error']['code'], 'invalid_tool_protocol')
        self.assertEqual(result['tool_calls'], 1)
        self.assertEqual(len(result['tool_results']), 1)

    def test_insufficient_data_keeps_null_change_in_agent_result(self):
        result, _ = self.run_replies([
            call('query_medications', {'mode': 'events', 'event_id': 'E007'}, 'a'),
            call('query_health_data', {'indicator': 'glucose', 'mode': 'compare_event', 'event_id': 'E007',
                                      'measurement_context': 'fasting', 'window_days': 2}, 'b'),
            final('两个窗口样本不足，无法比较变化。[E007][G007][G009]', ['E007', 'G007', 'G009']),
        ])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['tool_results'][1]['status'], 'insufficient_data')
        self.assertIsNone(result['tool_results'][1]['data']['comparisons'][0]['delta'])


if __name__ == '__main__':
    unittest.main()
