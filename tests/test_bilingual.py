from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json,re,unittest
from health_agent.config import PROJECT_ROOT,load_config
from health_agent.seed import initialize_database
from health_agent.results import ToolContext
from health_agent.tools import HealthTools
from health_agent.conversation import ConversationAgent
from health_agent.verification import validate_answer
from health_agent.i18n import language_for,wrong_language,ui_text
from health_agent.date_intent import explicit_bounds
from tests.scripted_provider import ScriptedProvider,call,final
from tests.test_day5 import FakeEmbedding
from health_agent.rag import build_index,search

class BilingualTests(unittest.TestCase):
 def setUp(self):
  self.temp=TemporaryDirectory();self.root=Path(self.temp.name);self.db=self.root/'health.sqlite3';initialize_database(self.db);self.tools=HealthTools(self.db,context=ToolContext('P001','bilingual-tests',load_config().reference_date))
 def tearDown(self):self.temp.cleanup()
 def agent(self,replies):return ConversationAgent(self.tools,ScriptedProvider(replies),checkpoint_path=self.root/'chat.sqlite3')
 def latest(self):return self.tools.query_health_data({'indicator':'glucose'})
 def compare(self,**kwargs):return self.tools.query_health_data({'indicator':'glucose','mode':'compare_event','event_id':'E007','measurement_context':'fasting',**kwargs})
 def test_language_detection_and_choice_inheritance(self):
  for text,previous,wanted in [('What is my latest glucose?','zh','en'),('血糖是多少','en','zh'),('E007','en','en'),('fasting','zh','zh'),('2','en','en'),('用英文回答血糖问题','zh','en'),('Does R002 mention 低密度脂蛋白?','zh','en'),('Compare 二甲双胍 records.','zh','en')]:self.assertEqual(language_for(text,previous),wanted)
 def test_explicit_language_override(self):self.assertEqual(language_for('血糖是多少',preferred='en'),'en')
 def test_language_guard_keeps_original_source_quotes(self):
  self.assertFalse(wrong_language('The report states:\n```text\n空腹、餐后、随机及条件未知分别归档。\n```','en'));self.assertTrue(wrong_language('这是不符合要求的中文回答。','en'))
 def test_english_calendar_bounds_and_inclusive_terminal_day(self):
  for text,left,right in [('from October 1 to October 15, 2026','2026-10-01','2026-10-16'),('October 1, 2026 through October 23, 2026','2026-10-01','2026-10-24'),('on October 22, 2026','2026-10-22','2026-10-23'),('in 2024','2024-01-01','2025-01-01'),('last year','2025-01-01','2026-01-01'),('past 30 days','2026-09-24','2026-10-24'),('2026-10-01 to 2026-10-15','2026-10-01','2026-10-16')]:self.assertEqual(explicit_bounds(text,load_config().reference_date),{'start_date':left,'end_date':right})
 def test_invalid_english_calendar_date_is_not_executed(self):
  with self.assertRaises(ValueError):explicit_bounds('on February 30, 2026',load_config().reference_date)
 def test_english_latest_uses_real_fact_and_english_instructions(self):
  a=self.agent([call('query_health_data',{'indicator':'glucose'}),final('The latest blood glucose is 6.9 mmol/L on 2026-10-22. The measurement condition is unknown.',['G032'])]);r=a.run('What is my latest blood glucose?');requests=a.provider.requests;a.close();self.assertEqual(r['status'],'ok');self.assertEqual(r['language'],'en');self.assertIn('REPLY_LANGUAGE=en',requests[0]['instructions'])
 def test_english_series_is_complete_with_english_receipt(self):
  a=self.agent([call('query_health_data',{'indicator':'glucose','mode':'series','measurement_context':'fasting'})]);r=a.run('List fasting glucose records from October 1 to October 15, 2026.');a.close();self.assertEqual(r['status'],'ok');self.assertEqual(r['answer_origin'],'record_receipt');self.assertIn('Retrieved 8',r['answer']);self.assertEqual(r['trace'][0]['arguments']['end_date'],'2026-10-16');self.assertEqual(len(r['citations']),8);self.assertTrue(validate_answer(r['answer'],r['citations'],r['tool_results'])['passed'])
 def test_english_nonfasting_does_not_authorize_fasting(self):
  a=self.agent([]);self.assertFalse(a._context_allowed({'user_texts':['non-fasting glucose'],'slots':{}},'fasting'));self.assertFalse(a._context_allowed({'user_texts':['not fasting'],'slots':{}},'fasting'));a.close()
 def test_english_clarification_survives_agent_recreation(self):
  a=self.agent([call('query_health_data',{'indicator':'blood_pressure'})]);r=a.run('Help me check my blood pressure.');a.close();self.assertEqual(r['status'],'needs_clarification');self.assertIn('latest',r['answer'].lower());self.assertFalse(re.search(r'[\u4e00-\u9fff]',r['answer']))
  a=self.agent([call('query_health_data',{'indicator':'blood_pressure'},'bp-resume'),final('Latest blood pressure is 126/82 mmHg, measured at 2026-10-23 20:00 local time.',['SBP024','DBP024'])]);r=a.run('latest');a.close();self.assertEqual(r['language'],'en');self.assertEqual(r['status'],'ok');self.assertIsNone(r['pending'])
 def test_english_medication_alias_and_date_confirm_real_event(self):
  a=self.agent([call('query_medications',{'mode':'events','medication_name':'Metformin'}),call('query_health_data',{'indicator':'glucose','mode':'compare_event','event_id':'E007','measurement_context':'fasting'},'compare'),final('Before mean 7.95 mmol/L; after mean 7.00 mmol/L. Decrease 0.95 mmol/L (-11.95%). This is an association, not a causal conclusion.',['E007','G001','G009'])]);r=a.run('Compare fasting glucose before and after the metformin change on October 1, 2026.');a.close();self.assertEqual(r['status'],'ok');self.assertEqual(r['trace'][0]['arguments']['medication_name'],'二甲双胍');self.assertIn('E007',r['citations'])
 def test_english_insufficient_receipt_is_not_a_trend_conclusion(self):
  from health_agent.business_receipts import business_receipt
  data=self.compare(window_days=2);r=business_receipt(data,'en');self.assertIn('before 2 records, after 1 records',r['answer']);self.assertNotIn('mean',r['answer']);self.assertTrue(validate_answer(r['answer'],r['citations'],[data])['passed'])
 def test_english_cancellation(self):
  a=self.agent([call('query_health_data',{'indicator':'blood_pressure'})]);a.run('Help me check my blood pressure.');r=a.run('cancel task');a.close();self.assertIn('cleared',r['answer']);self.assertEqual(r['session_state'],{})
 def test_english_plot_negation_and_intent(self):
  a=self.agent([]);self.assertTrue(a._plot_requested({'user_texts':['Plot my weight records.']}));self.assertFalse(a._plot_requested({'user_texts':['Do not plot the records.']}));self.assertFalse(a._chart_only({'user_texts':['Plot and explain the mean.']}));a.close()
 def test_english_chart_labels_and_data_match_original_snapshot(self):
  self.tools.language='en';q=self.tools.query_health_data({'indicator':'weight','mode':'series','start_date':'2026-10-01','end_date':'2026-10-24'})
  from health_agent.plotting import plot
  r=plot(self.tools,{'result_id':q['result_id']},output_root=self.root/'charts');c=r['data']['chart'];svg=Path(c['svg_path']).read_text();self.assertEqual(c['language'],'en');self.assertIn('Measurement time',svg);self.assertIn('Synthetic data',svg);self.assertEqual(c['record_ids'],[v['record_id'] for v in q['data']['records']])
 def test_english_numeric_unknown_and_latest_value_guards(self):
  r=self.latest()
  for answer in ['The latest blood glucose is 99 mmol/L.','The latest blood glucose is 6.9 mmol/L, non-fasting.','The latest blood glucose is 6.9 mmol/L, condition unknown and non-fasting.','The fasting blood glucose is 6.9 mmol/L.','You should increase the metformin dose.']:
   self.assertFalse(validate_answer(answer,['G032'],[r])['passed'],answer)
  self.assertTrue(validate_answer('The latest blood glucose is 6.9 mmol/L; condition unknown.',['G032'],[r])['passed'])
 def test_english_wrong_mean_side_and_direction_are_rejected(self):
  r=self.compare();ids=['E007','G001','G009']
  for text in ['Before mean 7.0 mmol/L; after mean 7.95 mmol/L.','The mean increased 11.95%.','Mean 8.0 mmol/L.']:
   self.assertFalse(validate_answer(text,ids,[r])['passed'],text)
 def test_english_no_derived_mean_or_false_chart_completion(self):
  r=self.latest()
  for text in ['Average glucose is 6.9 mmol/L.','The chart was generated.']:self.assertFalse(validate_answer(text,['G032'],[r])['passed'])
 def test_english_report_whole_absence_not_inferred_from_top_k(self):
  idx=self.root/'idx.sqlite3';build_index(self.db,index_path=idx,embedder=FakeEmbedding());r=search(self.tools,{'query':'thyroid follow-up','report_id':'R002'},index_path=idx,embedder=FakeEmbedding())
  self.assertFalse(validate_answer('The report does not mention thyroid follow-up.',['R002:C001'],[r])['passed']);self.assertTrue(validate_answer('The retrieved excerpts do not support a thyroid follow-up answer; they cannot establish absence from the full report.',['R002:C001'],[r])['passed'])
 def test_english_month_dates_validate_against_cited_fact(self):
  r=self.latest();self.assertTrue(validate_answer('Blood glucose 6.9 mmol/L on October 22, 2026.',['G032'],[r])['passed']);self.assertFalse(validate_answer('Blood glucose 6.9 mmol/L on October 21, 2026.',['G032'],[r])['passed'])
 def test_language_switch_does_not_reuse_old_health_fact(self):
  a=self.agent([call('query_health_data',{'indicator':'glucose'}),final('Latest blood glucose is 6.9 mmol/L, condition unknown.',['G032'])]);r=a.run('Latest blood glucose?');a.close()
  a=self.agent([call('query_health_data',{'indicator':'weight'}),final('最近一次体重77.5 kg。',['W016'])]);r=a.run('那体重呢？');a.close();self.assertEqual(r['language'],'zh');self.assertEqual(r['citations'],['W016']);self.assertEqual(len(r['trace']),1)
 def test_english_interface_and_record_table(self):
  from streamlit.testing.v1 import AppTest
  q=self.tools.query_health_data({'indicator':'glucose','mode':'series','start_date':'2026-10-01','end_date':'2026-10-16','measurement_context':'fasting'})
  class UIOnly:
   def run(self,qtext):return {'status':'ok','answer':'Retrieved 8 original records.','answer_origin':'record_receipt','trace':[],'tool_results':[q]}
  with patch('health_agent.config.DEFAULT_DATABASE',self.db),patch('health_agent.app_support.configuration_status',return_value={'configured':True,'model':'test-double-only'}),patch('health_agent.app_support.create_session_agent',return_value=UIOnly()):
   app=AppTest.from_file(str(PROJECT_ROOT/'streamlit_app.py'),default_timeout=15).run();app.radio(key='ui_language').set_value('English').run();self.assertFalse(app.exception);self.assertEqual(app.title[0].value,'Personal Health Data Agent');self.assertIn('New session',[b.label for b in app.button]);app.chat_input[0].set_value('List my fasting glucose records.').run();self.assertFalse(app.exception);self.assertEqual(len(app.dataframe[0].value),8);self.assertIn('Original query records',[x.label for x in app.expander])

 def test_plot_handle_is_bound_to_current_snapshot_without_weakening_direct_id_checks(self):
  q=self.tools.query_health_data({'indicator':'weight','mode':'series','start_date':'2026-10-01','end_date':'2026-10-24'})
  a=self.agent([]);state={'user_texts':['Plot my weight.'],'results':[q]};self.assertEqual(a._plot_handles(state),{'query_1':q['result_id']});self.assertEqual(a._tool_specs(state)[0]['parameters']['properties']['result_id']['enum'],['query_1']);self.assertEqual(self.tools.plot_health_trend({'result_id':'query_1'})['status'],'error');a.close()
 def test_english_am_pm_times_checked_against_local_record(self):
  r=self.latest();self.assertTrue(validate_answer('Blood glucose 6.9 mmol/L at 8 AM on 2026-10-22.',['G032'],[r])['passed']);self.assertFalse(validate_answer('Blood glucose 6.9 mmol/L at 9 AM on 2026-10-22.',['G032'],[r])['passed'])
 def test_empty_english_input_uses_english_error(self):
  a=self.agent([]);r=a.run('',language='en');a.close();self.assertEqual(r['language'],'en');self.assertIn('non-empty',r['answer'])

 def test_english_language_allows_only_controlled_source_terminology(self):
  self.assertFalse(wrong_language('The full scan checked LDL, LDL-C, 低密度脂蛋白 and low-density lipoprotein.','en'));self.assertTrue(wrong_language('这份报告没有提及甲状腺复查安排。','en'))
 def test_event_clarification_requires_real_candidates_first(self):
  a=self.agent([call('ask_user',{'question':'Which event?','choices':[],'missing_fields':['event_id']},'ask-first'),call('query_medications',{'mode':'events'},'events'),call('ask_user',{'question':'Which event should be compared?','choices':['E007'],'missing_fields':['event_id']},'ask-after')]);r=a.run('Did glucose change after my medication was adjusted?');a.close();self.assertEqual(r['status'],'needs_clarification');self.assertEqual(r['trace'][0]['error_code'],'events_required_before_clarification');self.assertIn('E007',r['pending']['choices'])

 def test_english_date_and_no_trend_equivalence_in_acceptance_grading(self):
  from scripts.bilingual_acceptance import grade
  a=self.agent([call('query_health_data',{'indicator':'blood_pressure'}),final('Latest blood pressure is 126/82 mmHg on October 23, 2026.',['SBP024','DBP024'])]);r=a.run('What is my latest blood pressure?');a.close();self.assertTrue(grade({'id':'T07'},[r],'en')['passed'])
  self.tools.context=ToolContext('P001','bilingual-plot-grading',load_config().reference_date)
  a=self.agent([call('query_health_data',{'indicator':'weight','mode':'series'},'query-single'),call('plot_health_trend',{'result_id':'query_1'},'plot-single')]);r=a.run('Plot my weight on October 20, 2026.');a.close();self.assertTrue(grade({'id':'T08'},[r],'en')['passed'])

 def test_english_empty_receipt_has_authoritative_scope_without_citations(self):
  from health_agent.business_receipts import business_receipt
  data=self.tools.query_health_data({'indicator':'glucose','mode':'series','start_date':'2024-01-01','end_date':'2025-01-01'});r=business_receipt(data,'en');self.assertEqual(r['citations'],[]);self.assertTrue(validate_answer(r['answer'],[],[data])['passed']);self.assertFalse(validate_answer(r['answer'].replace('2025-01-01','2025-01-02'),[],[data])['passed']);self.assertFalse(validate_answer('Glucose is 0 mmol/L.',[],[data])['passed'])
 def test_unit_spellings_do_not_convert_or_accept_wrong_units(self):
  data=self.tools.query_health_data({'indicator':'weight'})
  for answer in ['Latest weight is 77.5 kilograms.','最近一次体重77.5公斤。']:self.assertTrue(validate_answer(answer,['W016'],[data])['passed'])
  for answer in ['Latest weight is 77.5 pounds.','Latest weight is 77.5 lb.','最近一次体重77.5磅。','Latest weight is 77.5 grams.']:self.assertFalse(validate_answer(answer,['W016'],[data])['passed'])

 def test_spelled_out_medication_unit_must_match_source_dose(self):
  data=self.tools.query_medications({'mode':'current'})
  self.assertTrue(validate_answer('Metformin dose: 850 milligrams.',['M002'],[data])['passed']);self.assertFalse(validate_answer('Metformin dose: 850 micrograms.',['M002'],[data])['passed'])

 def test_latest_return_count_does_not_prove_only_history_record(self):
  r=self.tools.query_health_data({'indicator':'glucose','mode':'latest','measurement_context':'fasting'})
  for text in ['This is the only available fasting glucose record in our system.','No other fasting glucose records exist.','这是系统中唯一的空腹血糖记录。']:self.assertFalse(validate_answer(text,['G016'],[r])['passed'],text)
  self.assertTrue(validate_answer('This query returned only one latest record; earlier history is not shown.',['G016'],[r])['passed'])
 def test_unit_abbreviation_spelling_is_case_insensitive_without_rescaling(self):
  r=self.latest()
  for text in ['Glucose: 6.9 mmol per L.','Glucose: 6.9 millimoles per LITER.']:self.assertTrue(validate_answer(text,['G032'],[r])['passed'])
  self.assertFalse(validate_answer('Glucose: 6.9 mg per dL.',['G032'],[r])['passed'])

 def test_latest_receipt_finishes_with_real_fields_without_model_retyping(self):
  a=self.agent([call('query_health_data',{'indicator':'glucose'})]);r=a.run('What is my latest glucose?');requests=a.provider.requests;a.close();self.assertEqual(r['record_query_mode'],'latest');self.assertIsNone(r['model_answer_accepted']);self.assertEqual(len(requests),1);self.assertIn('6.9 mmol/L',r['answer']);self.assertIn('unknown',r['answer']);self.assertTrue(validate_answer(r['answer'],r['citations'],r['tool_results'])['passed'])
 def test_latest_analysis_keeps_model_generation_and_verification(self):
  a=self.agent([call('query_health_data',{'indicator':'glucose'}),final('The latest glucose is 6.9 mmol/L; condition unknown. No trend can be inferred.',['G032'])]);r=a.run('Explain my latest glucose.');requests=a.provider.requests;a.close();self.assertEqual(len(requests),2);self.assertNotEqual(r.get('record_query_mode'),'latest');self.assertEqual(r['status'],'ok')

 def test_latest_receipt_does_not_skip_a_second_requested_indicator(self):
  a=self.agent([call('query_health_data',{'indicator':'glucose'},'g'),call('query_health_data',{'indicator':'weight'},'w'),final('Latest blood glucose is 6.9 mmol/L, condition unknown; latest weight is 77.5 kg.',['G032','W016'])]);r=a.run('What are my latest glucose and weight?');requests=a.provider.requests;a.close();self.assertEqual(r['status'],'ok');self.assertEqual(len(requests),3);self.assertEqual(set(r['citations']),{'G032','W016'});self.assertNotEqual(r.get('record_query_mode'),'latest')
 def test_latest_receipt_waits_for_requested_report_read(self):
  a=self.agent([call('query_health_data',{'indicator':'glucose'}),final('Latest glucose is 6.9 mmol/L, condition unknown.',['G032'])]);r=a.run('First read report R004, then tell me my latest glucose using the database result.');requests=a.provider.requests;a.close();self.assertNotEqual(r.get('record_query_mode'),'latest');self.assertEqual(len(requests),2)

 def test_latest_receipt_after_actual_report_read_does_not_treat_read_as_report_id(self):
  idx=self.root/'idx.sqlite3';build_index(self.db,index_path=idx,embedder=FakeEmbedding())
  a=self.agent([call('search_health_documents',{'query':'glucose','report_id':'R004'},'r'),call('query_health_data',{'indicator':'glucose'},'g')])
  with patch.object(self.tools,'search_health_documents',side_effect=lambda args:search(self.tools,args,index_path=idx,embedder=FakeEmbedding())):
   r=a.run('First read report R004, then tell me my latest glucose using the database result.')
  a.close();self.assertEqual(r['status'],'ok');self.assertEqual(r.get('record_query_mode'),'latest');self.assertEqual(len(a.provider.requests),2);self.assertIn('G032',r['citations'])

 def test_explicit_requested_unit_is_not_lost_when_model_omits_it(self):
  for unit in ['mg/dL','milligrams per deciliter']:
   a=self.agent([call('query_health_data',{'indicator':'glucose'})]);r=a.run('What is my latest blood glucose in '+unit+'?');a.close()
   self.assertEqual(r['status'],'ok');self.assertEqual(r['trace'][0]['arguments']['requested_unit'],'mg/dL');self.assertEqual(r['tool_results'][0]['status'],'unsupported');self.assertIn('conversion',r['answer']);self.assertNotIn('6.9',r['answer'])
 def test_matching_explicit_unit_keeps_original_number(self):
  a=self.agent([call('query_health_data',{'indicator':'weight','requested_unit':'lb'})]);r=a.run('What is my latest weight in kilograms?');a.close();self.assertEqual(r['trace'][0]['arguments']['requested_unit'],'kg');self.assertIn('77.5 kg',r['answer']);self.assertTrue(validate_answer(r['answer'],r['citations'],r['tool_results'])['passed'])

 def test_clarification_cannot_display_unexecuted_database_result_claim(self):
  examples=[('What is my latest glucose?','The database query returned no results. Would you like the latest value?'),('我最近一次血糖是多少？','数据库查询没有结果，血糖99 mmol/L。要最近一次吗？')]
  for i,(question,prose) in enumerate(examples):
   a=ConversationAgent(self.tools,ScriptedProvider([final(prose,clarify=True)]),checkpoint_path=self.root/('isolate'+str(i)+'.sqlite3'));r=a.run(question);a.close()
   self.assertEqual(r['status'],'needs_clarification');self.assertNotIn('database',r['answer'].lower());self.assertNotIn('数据库',r['answer']);self.assertNotIn('99',r['answer']);self.assertEqual(r['pending']['choices'],['latest','series']);self.assertFalse(wrong_language(r['answer'],r['language']))
 def test_ask_user_question_and_choices_cannot_smuggle_measurements(self):
  a=self.agent([call('ask_user',{'question':'Your glucose is 99 mmol/L; no records exist. Choose.','choices':['99 mmol/L','No records exist'],'missing_fields':['mode']})]);r=a.run('Help me check my blood pressure.');a.close();self.assertEqual(r['status'],'needs_clarification');self.assertNotIn('99',r['answer']);self.assertNotIn('records exist',r['answer']);self.assertEqual(r['pending']['choices'],['latest','series'])

 def test_explicit_english_synthetic_label_supports_bilingual_absence_but_not_new_absence(self):
  idx=self.root/'synthetic-source.sqlite3';build_index(self.db,index_path=idx,embedder=FakeEmbedding());r=search(self.tools,{'query':'record organization','report_id':'R003'},index_path=idx,embedder=FakeEmbedding())
  source='Synthetic bilingual report. No real patient data. Keep missing measurement days empty; do not fill or interpolate them.'
  # Synthetic ToolResult: test source-language equivalence without editing frozen reports.
  for row in r['data']['matches']:row['text']=source
  for evidence in r['evidence']:evidence['record']['text']=source
  for answer in ['该报告不包含真实患者数据。','The report contains no real patient data.']:
   self.assertTrue(validate_answer(answer,['R003:C001'],[r])['passed'],answer)
  for answer in ['该报告没有血糖记录。','The document does not provide actual measurement gaps.','The report does not mention thyroid follow-up.']:
   self.assertFalse(validate_answer(answer,['R003:C001'],[r])['passed'],answer)

 def test_report_line_range_is_validated_as_locator_not_negative_measurement(self):
  idx=self.root/'line-source.sqlite3';build_index(self.db,index_path=idx,embedder=FakeEmbedding());r=search(self.tools,{'query':'record organization','report_id':'R003'},index_path=idx,embedder=FakeEmbedding());e=r['evidence'][0];start=e['locator']['start_line'];end=e['locator']['end_line'];ids=[e['source_id']]
  for answer in [f'报告 R003 第{start}-{end}行说明应分类归档。',f'Report R003 lines {start}-{end}: archive by condition.']:
   self.assertTrue(validate_answer(answer,ids,[r])['passed'],answer)
  for answer in [f'报告 R003 第{start}-{end+1}行。',f'Report R001 lines {start}-{end}.',f'报告 R003 第{start}-{end}行，血糖99 mmol/L。']:
   self.assertFalse(validate_answer(answer,ids,[r])['passed'],answer)
