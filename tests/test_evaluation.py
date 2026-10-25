import unittest

from health_agent.evaluation import grade_smoke


class LiveSmokeGradingTests(unittest.TestCase):
    def test_text_alone_is_not_proof_of_live_tool_use(self):
        result = {'status': 'ok', 'answer': '6.9 mmol/L [G032]', 'citations': ['G032'], 'trace': [], 'tool_results': []}
        self.assertFalse(grade_smoke('T01', result)['passed'])

    def test_wrong_answer_number_cannot_pass_on_correct_tool_result_alone(self):
        result = {'status': 'ok', 'answer': '9.9 mmol/L [G032]', 'citations': ['G032'],
                  'trace': [{'tool': 'query_health_data'}],
                  'tool_results': [{'evidence': [{'source_id': 'G032'}], 'data': {'records': [{'record_id': 'G032'}]}}]}
        report = grade_smoke('T01', result)
        self.assertFalse(report['passed'])
        self.assertFalse(report['checks']['correct_value_in_answer'])

    def test_malformed_comparison_run_is_not_counted_as_passed(self):
        result = {'status': 'error', 'answer': '7.95 7.0 11.95 [E007][G001]', 'citations': ['E007', 'G001'], 'trace': [], 'tool_results': []}
        self.assertFalse(grade_smoke('T10', result)['passed'])
