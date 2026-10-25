from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json,sqlite3,unittest
from health_agent.config import PROJECT_ROOT,load_config
from health_agent.seed import initialize_database
from health_agent.tools import HealthTools
from health_agent.results import ToolContext
from health_agent.conversation import ConversationAgent
from health_agent.verification import validate_answer
from health_agent.rag import build_index,search
from tests.test_day5 import FakeEmbedding
from tests.scripted_provider import ScriptedProvider,call,final

class Day7Tests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name);self.db=self.root/'health.sqlite3';initialize_database(self.db)
        self.ctx=ToolContext('P001','day7-tests',load_config().reference_date);self.tools=HealthTools(self.db,context=self.ctx)
    def tearDown(self):self.temp.cleanup()
    def compare(self,**extra):return self.tools.query_health_data({'indicator':'glucose','mode':'compare_event','event_id':'E007','measurement_context':'fasting',**extra})
    def test_no_chart_instruction_does_not_force_plot(self):
        a=ConversationAgent(self.tools,ScriptedProvider([call('query_health_data',{'indicator':'glucose'}),final('最近血糖6.9 mmol/L，条件未知。',['G032'])]),checkpoint_path=self.root/'chat.sqlite3')
        self.assertFalse(a._plot_requested({'user_texts':['不要画图，只告诉我最近的血糖。']}));r=a.run('不要画图，只告诉我最近的血糖。');a.close();self.assertEqual(r['status'],'ok');self.assertFalse(any(x['tool']=='plot_health_trend' for x in r['trace']))
    def test_wrong_latest_value_cannot_borrow_earlier_record(self):
        r=self.tools.query_health_data({'indicator':'glucose','mode':'series','start_date':'2026-10-01','end_date':'2026-10-24'})
        self.assertFalse(validate_answer('最新血糖为7.1 mmol/L。',['G031','G032'],[r])['passed'])
    def test_mean_cannot_borrow_count_or_raw_measurement(self):
        r=self.compare();self.assertFalse(validate_answer('均值8.0 mmol/L。',['G004','G001'],[r])['passed'])
    def test_insufficient_comparison_is_explainable_and_not_a_change(self):
        r=self.compare(window_days=2);self.assertEqual(r['status'],'insufficient_data')
        self.assertTrue(validate_answer('前窗2条，后窗1条；每窗至少3条，样本不足，无法比较。',['E007','G007','G009'],[r])['passed'])
        self.assertFalse(validate_answer('血糖下降10%。',['E007','G007','G009'],[r])['passed'])
    def test_no_prescription_instruction(self):
        r=self.tools.query_health_data({'indicator':'glucose'})
        self.assertFalse(validate_answer('血糖6.9 mmol/L，建议加量。',['G032'],[r])['passed'])
        self.assertTrue(validate_answer('血糖6.9 mmol/L，不建议自行调整剂量。',['G032'],[r])['passed'])
    def test_vector_store_initialization_failure_marks_failed(self):
        with patch('health_agent.rag.collection',side_effect=RuntimeError('TEST_PRIVATE_DETAIL')):r=build_index(self.db,index_path=self.root/'idx.sqlite3',embedder=FakeEmbedding())
        self.assertFalse(r['ok']);self.assertNotIn('TEST_PRIVATE_DETAIL',json.dumps(r))
        with sqlite3.connect(self.db) as con:self.assertEqual(con.execute("SELECT count(*) FROM reports WHERE index_status='failed'").fetchone()[0],4)
    def test_retrieval_library_failure_is_controlled(self):
        idx=self.root/'idx.sqlite3';build_index(self.db,index_path=idx,embedder=FakeEmbedding())
        with patch('health_agent.rag.collection',side_effect=RuntimeError('TEST_PRIVATE_DETAIL')):r=search(self.tools,{'query':'记录','report_id':'R003'},index_path=idx,embedder=FakeEmbedding())
        self.assertEqual(r['status'],'error');self.assertNotIn('TEST_PRIVATE_DETAIL',json.dumps(r))
    def test_series_table_contains_all_records(self):
        from streamlit.testing.v1 import AppTest
        data=self.tools.query_health_data({'indicator':'glucose','mode':'series','start_date':'2026-10-01','end_date':'2026-10-16','measurement_context':'fasting'})
        class UIOnly:
            def run(self,q):return {'status':'ok','answer':'查询到8条，明细见下表。','trace':[],'tool_results':[data]}
        with patch('health_agent.config.DEFAULT_DATABASE',self.db),patch('health_agent.app_support.configuration_status',return_value={'configured':True,'model':'test-double-only'}),patch('health_agent.app_support.create_session_agent',return_value=UIOnly()):
            app=AppTest.from_file(str(PROJECT_ROOT/'streamlit_app.py'),default_timeout=15).run();app.chat_input[0].set_value('列出记录').run()
            self.assertFalse(app.exception);self.assertEqual(list(app.dataframe[0].value.record_id),[f'G{i:03d}' for i in range(9,17)])
    def test_top_k_cannot_claim_whole_report_absence(self):
        idx=self.root/'idx.sqlite3';build_index(self.db,index_path=idx,embedder=FakeEmbedding())
        r=search(self.tools,{'query':'甲状腺','report_id':'R002'},index_path=idx,embedder=FakeEmbedding())
        self.assertFalse(validate_answer('该报告不包含甲状腺相关内容。',['R002:C001'],[r])['passed'])
        self.assertTrue(validate_answer('检索片段未找到支持依据，不能据此断言整份报告未提及。',['R002:C001'],[r])['passed'])

    def test_latest_does_not_guess_fasting(self):
        a=ConversationAgent(self.tools,ScriptedProvider([call('query_health_data',{'indicator':'glucose','mode':'latest','measurement_context':'fasting'}),final('最近血糖6.9 mmol/L，条件未知。',['G032'])]),checkpoint_path=self.root/'chat.sqlite3')
        result=a.run('我最近一次血糖是多少？');a.close()
        self.assertEqual(result['status'],'ok');self.assertIsNone(result['trace'][0]['arguments']['measurement_context'])

    def test_explicit_calendar_bounds(self):
        from health_agent.date_intent import explicit_bounds
        ref=load_config().reference_date
        for text,left,right in [('2026年10月1日到10月15日的空腹记录','2026-10-01','2026-10-16'),('2024年有哪些记录','2024-01-01','2025-01-01'),('2026年10月22日最近一次','2026-10-22','2026-10-23'),('2026年12月30日至1月2日','2026-12-30','2027-01-03')]:
            self.assertEqual(explicit_bounds(text,ref),{'start_date':left,'end_date':right})
        self.assertIsNone(explicit_bounds('最近一次血糖',ref))
    def test_date_normalization_precedes_sql_and_model_sees_actual_args(self):
        provider=ScriptedProvider([call('query_health_data',{'indicator':'glucose','mode':'series','start_date':'2026-10-01','end_date':'2026-10-15','measurement_context':'fasting'}),final('查询到8条记录。',[f'G{i:03d}' for i in range(9,17)])])
        a=ConversationAgent(self.tools,provider,checkpoint_path=self.root/'chat.sqlite3');r=a.run('分析2026年10月1日到10月15日的空腹血糖记录。');a.close()
        self.assertEqual(r['tool_results'][0]['data']['count'],8);self.assertEqual(r['trace'][0]['arguments']['end_date'],'2026-10-16')
        request_call=next(m for m in provider.requests[-1]['messages'] if m.get('type')=='function_call')
        self.assertEqual(json.loads(request_call['arguments'])['end_date'],'2026-10-16')

    def test_insufficient_result_ends_with_business_receipt_without_fake_mean(self):
        provider=ScriptedProvider([call('query_medications',{'mode':'events','event_id':'E007'},'events'),call('query_health_data',{'indicator':'glucose','mode':'compare_event','event_id':'E007','measurement_context':'fasting','window_days':2},'compare')])
        a=ConversationAgent(self.tools,provider,checkpoint_path=self.root/'chat.sqlite3');r=a.run('比较2026年10月1日二甲双胍调整前后各2天的空腹血糖。');a.close()
        self.assertEqual(r['answer_origin'],'business_receipt');self.assertEqual(r['business_status'],'insufficient_data');self.assertIn('前窗2条',r['answer']);self.assertIn('后窗1条',r['answer']);self.assertNotIn('均值',r['answer']);self.assertEqual(len(provider.requests),2)
    def test_business_receipt_is_not_an_unsupported_successful_comparison(self):
        from health_agent.business_receipts import business_receipt
        r=self.compare(window_days=2);receipt=business_receipt(r)
        self.assertEqual(receipt['business_status'],'insufficient_data');self.assertIsNone(receipt['model_answer_accepted']);self.assertNotIn('%',receipt['answer'])

    def test_comparison_ui_explicitly_labels_exclusive_windows(self):
        from streamlit.testing.v1 import AppTest
        comp=self.compare()
        class UIOnly:
            def run(self,q):return {'status':'ok','answer':'比较事实见下方。','trace':[],'tool_results':[comp]}
        with patch('health_agent.config.DEFAULT_DATABASE',self.db),patch('health_agent.app_support.configuration_status',return_value={'configured':True,'model':'test-double-only'}),patch('health_agent.app_support.create_session_agent',return_value=UIOnly()):
            app=AppTest.from_file(str(PROJECT_ROOT/'streamlit_app.py'),default_timeout=15).run();app.chat_input[0].set_value('比较').run()
            self.assertFalse(app.exception);self.assertEqual(list(app.dataframe[0].value['结束（不含）']),['2026-10-01','2026-10-16'])
            self.assertTrue(any('事件当天' in x.value for x in app.caption))

    def test_record_list_completes_with_all_original_rows_without_extra_model_call(self):
        provider=ScriptedProvider([call('query_health_data',{'indicator':'glucose','mode':'series','measurement_context':'fasting'})])
        a=ConversationAgent(self.tools,provider,checkpoint_path=self.root/'chat.sqlite3');r=a.run('2026年10月1日到10月15日的空腹血糖记录是什么？');a.close()
        self.assertEqual(r['answer_origin'],'record_receipt');self.assertIsNone(r['model_answer_accepted']);self.assertEqual(len(provider.requests),1)
        self.assertEqual([v['record_id'] for v in r['tool_results'][0]['data']['records']],[f'G{i:03d}' for i in range(9,17)])
        self.assertIn('终日不含',r['answer']);self.assertTrue(validate_answer(r['answer'],r['citations'],r['tool_results'])['passed'])
    def test_series_analysis_still_requires_model_answer(self):
        provider=ScriptedProvider([call('query_health_data',{'indicator':'weight','mode':'series','start_date':'2026-10-01','end_date':'2026-10-24'}),final('有7条体重记录。',[f'W{i:03d}' for i in range(10,17)])])
        a=ConversationAgent(self.tools,provider,checkpoint_path=self.root/'chat.sqlite3');r=a.run('分析2026年10月1日到23日的体重记录。');a.close()
        self.assertEqual(len(provider.requests),2);self.assertNotEqual(r.get('answer_origin'),'record_receipt')

    def test_supported_report_negation_allows_optional_de_particle_only(self):
        idx=self.root/'idx.sqlite3';build_index(self.db,index_path=idx,embedder=FakeEmbedding())
        r=search(self.tools,{'query':'记录整理','report_id':'R003'},index_path=idx,embedder=FakeEmbedding())
        self.assertTrue(validate_answer('报告不包含新测量数值。',['R003:C001'],[r])['passed'])
        self.assertFalse(validate_answer('报告不包含甲状腺记录。',['R003:C001'],[r])['passed'])
        self.assertFalse(validate_answer('报告不包含血糖记录。',['R003:C001'],[r])['passed'])
