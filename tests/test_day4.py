"""Offline guard/state contracts; never counted as live-model acceptance."""
import json
import tempfile
import unittest
from pathlib import Path
from health_agent.config import load_config
from health_agent.conversation import ConversationAgent
from health_agent.results import ToolContext
from health_agent.seed import initialize_database
from health_agent.tools import HealthTools
from tests.scripted_provider import ScriptedProvider,call,final

def ask(field='mode',question='最近一次还是一段时间？',choices=None,task=None):
 return call('ask_user',{'question':question,'choices':choices or ['latest','series'],'missing_fields':[field],'pending_task_id':task},'ask_'+field)

class ConversationTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.folder=Path(self.temp.name);self.db=self.folder/'health.sqlite3';initialize_database(self.db)
  self.checkpoints=self.folder/'conversations.sqlite3';self.cfg=load_config()
 def agent(self,replies,session='A',patient='P001'):
  tools=HealthTools(self.db,context=ToolContext(patient,session,self.cfg.reference_date))
  agent=ConversationAgent(tools,ScriptedProvider(replies),checkpoint_path=self.checkpoints,retry_sleep=lambda _:None)
  self.addCleanup(lambda:agent.close() if agent.conn else None)
  return agent
 def close(self,a):
  a.close();a.conn=None
 def test_durable_pause_restart_resume_and_third_turn(self):
  a=self.agent([ask()]);first=a.run('帮我看看血压。');tid=first['thread_id'];task=first['pending_task_id']
  self.assertEqual(first['status'],'needs_clarification');self.assertEqual(first['tool_results'][0]['status'],'needs_clarification')
  self.close(a)
  b=self.agent([call('query_health_data',{'indicator':'blood_pressure','mode':'latest','measurement_context':None},'bp'),
                call('query_health_data',{'indicator':'blood_pressure','mode':'latest','start_date':'2026-09-23','end_date':'2026-09-24','measurement_context':None},'bp2'),final('日期对应的血压记录。',['SBP004','DBP004'])])
  second=b.run('最近一次就好。');self.assertEqual(second['status'],'ok');self.assertEqual(second['pending_task_id'],task);self.assertIsNone(second['pending'])
  third=b.run('那2026年9月23日最近一次呢？');self.assertEqual(third['status'],'ok');self.assertEqual(third['thread_id'],tid);self.assertNotEqual(third['pending_task_id'],task)
  self.assertEqual({e['source_id'] for r in third['tool_results'] for e in r['evidence']},{'SBP004','DBP004'})
  self.assertIn('血压',json.dumps(b.provider.requests[-1]['messages'],ensure_ascii=False))
 def test_guessed_event_then_context_blocked_before_statistics(self):
  args={'indicator':'glucose','mode':'compare_event','event_id':'E007','measurement_context':'fasting','window_days':14}
  a=self.agent([call('query_medications',{'mode':'events'},'events'),call('query_health_data',args,'guess1'),call('query_health_data',args,'guess2'),call('query_health_data',args,'confirmed'),final('各8条，7.95和7.00 mmol/L，下降11.95%，不能推断因果。',['E007','G001','G009'])])
  first=a.run('我调整药物以后血糖有没有变化？')
  self.assertEqual(first['pending']['missing_fields'],['event_id']);self.assertIn('E007',first['pending']['choices'])
  self.assertFalse(any(r.get('data',{}).get('comparisons') for r in first['tool_results']))
  second=a.run('E007');self.assertEqual(second['pending']['missing_fields'],['measurement_context'])
  self.assertFalse(any(r.get('data',{}).get('comparisons') for r in second['tool_results']))
  third=a.run('空腹');self.assertEqual(third['status'],'ok')
  self.assertEqual(sum(t['tool']=='query_medications' for t in third['trace']),1)
  self.assertEqual(sum(t['tool']=='query_health_data' and t['status']=='ok' for t in third['trace']),1)
  self.assertEqual(third['pending_task_id'],first['pending_task_id'])
 def test_unclear_reply_does_not_authorize_guessed_event(self):
  args={'indicator':'glucose','mode':'compare_event','event_id':'E007','measurement_context':'fasting'}
  a=self.agent([call('query_medications',{'mode':'events'},'events'),call('query_health_data',args,'one'),call('query_health_data',args,'two')])
  a.run('我调药后血糖有变化吗？');r=a.run('随便吧')
  self.assertEqual(r['status'],'needs_clarification');self.assertEqual(r['pending']['missing_fields'],['event_id'])
  self.assertFalse(any(x.get('data',{}).get('comparisons') for x in r['tool_results']))
 def test_explicit_event_and_context_need_no_repeat_question(self):
  a=self.agent([call('query_medications',{'mode':'events','event_id':'E007'},'e'),call('query_health_data',{'indicator':'glucose','mode':'compare_event','event_id':'E007','measurement_context':'fasting'},'c'),final('7.95至7.00，仅描述关联。',['E007','G001','G009'])])
  r=a.run('比较2026年10月1日二甲双胍调整前后14天空腹血糖。')
  self.assertEqual(r['status'],'ok');self.assertIsNone(r['pending'])
 def test_new_session_and_patient_do_not_resume_another_pending_task(self):
  first=self.agent([ask()]).run('帮我看看血压。')
  for session,patient in [('B','P001'),('A','P002')]:
   other=self.agent([final('你好。')],session,patient).run('你好')
   self.assertNotEqual(other['thread_id'],first['thread_id']);self.assertEqual(other['status'],'ok');self.assertIsNone(other['pending'])
 def test_indicator_switch_clears_stale_fasting_before_execution(self):
  a=self.agent([call('query_health_data',{'indicator':'glucose','measurement_context':'fasting'},'g'),call('query_health_data',{'indicator':'weight','measurement_context':'fasting'},'w'),final('最近体重记录。',['W016'])])
  a.run('最近一次空腹血糖是多少？');r=a.run('那最近一次体重呢？')
  self.assertEqual(r['status'],'ok');self.assertEqual(r['session_state']['indicator'],'weight');self.assertIsNone(r['session_state']['measurement_context']);self.assertIsNone(r['trace'][0]['arguments']['measurement_context'])
 def test_cancel_clears_pending_and_context(self):
  a=self.agent([ask(),final('新任务。')]);a.run('帮我看看血压');cancel=a.run('取消任务')
  self.assertIsNone(cancel['pending']);self.assertEqual(cancel['session_state'],{})
  r=a.run('你好');self.assertEqual(r['status'],'ok');self.assertEqual(len(a.provider.requests[-1]['messages']),1)
 def test_new_turn_cannot_reuse_old_evidence_without_query(self):
  a=self.agent([call('query_health_data',{'indicator':'glucose'},'g'),final('6.9。',['G032']),final('上次6.9。',['G032']),final('6.9。',['G032'])])
  a.run('最近血糖是多少？');r=a.run('再说一次')
  self.assertEqual(r['error']['code'],'invalid_final_response');self.assertEqual(r['tool_results'],[])
 def test_model_pending_task_id_cannot_override_app_identity(self):
  r=self.agent([ask(task='another-task')]).run('帮我看看血压')
  self.assertEqual(r['status'],'needs_clarification');self.assertNotEqual(r['pending_task_id'],'another-task')
 def test_numeric_comparison_cannot_skip_comparison_tool(self):
  a=self.agent([call('query_medications',{'mode':'events'},'events'),final('前后均值8和7，下降10%。',['E007'])])
  r=a.run('调药以后血糖有没有变化？');self.assertEqual(r['status'],'needs_clarification');self.assertEqual(r['pending']['missing_fields'],['event_id'])
 def test_final_question_compatibility_becomes_durable_interrupt(self):
  a=self.agent([final('最近一次还是一段时间？',clarify=True),call('query_health_data',{'indicator':'blood_pressure'},'bp'),final('126/82。',['SBP024','DBP024'])])
  self.assertEqual(a.run('帮我看看血压')['status'],'needs_clarification')
  self.assertEqual(a.run('最近一次')['status'],'ok')
 def test_invalid_input_does_not_corrupt_pending(self):
  a=self.agent([ask()]);first=a.run('帮我看看血压')
  self.assertEqual(a.run('')['error']['code'],'invalid_question')
  self.assertEqual(a.graph.get_state(a.graph_config).values['task_id'],first['pending_task_id'])

 def test_vague_mode_cannot_be_silently_defaulted_by_model(self):
  a=self.agent([call('query_health_data',{'indicator':'blood_pressure','mode':'latest'},'guess')])
  r=a.run('帮我看看血压。')
  self.assertEqual(r['status'],'needs_clarification');self.assertEqual(r['pending']['missing_fields'],['mode'])
  self.assertFalse(any(x['evidence'] for x in r['tool_results']))
  self.assertTrue(any(t['tool']=='ask_user' and t['origin']=='policy_guard' for t in r['trace']))
 def test_model_cannot_offer_fabricated_event_in_question(self):
  r=self.agent([ask('event_id','请选择E999',['E999'])]).run('调药后有什么变化？')
  self.assertEqual(r['error']['code'],'invalid_ask_user')
 def test_day4_rules_survive_native_provider_selection_prefix(self):
  a=self.agent([])
  prefix=a._instructions({'slots':{}}).split('最终按指定JSON结构输出')[0]
  self.assertIn('Day 4会话已启用',prefix);self.assertIn('帮我看看血压',prefix)
  self.assertNotIn('本阶段尚无正式ask_user',prefix)

 def test_conflicting_event_is_checked_before_asking_unneeded_context(self):
  a=self.agent([call('query_medications',{'mode':'events','event_id':'E005'},'e'),call('query_health_data',{'indicator':'glucose','mode':'compare_event','event_id':'E005','measurement_context':None},'c'),final('窗口内有其他用药变更冲突，不能比较。',['E005','E004','E006'])])
  r=a.run('比较2026年9月2日阿托伐他汀调整前后14天的血糖。')
  self.assertEqual(r['status'],'ok');self.assertIsNone(r['pending'])
  self.assertEqual(r['tool_results'][-1]['status'],'conflicting_events')
 def test_unconfirmed_condition_cannot_compute_even_homogeneous_windows(self):
  a=self.agent([call('query_medications',{'mode':'events','event_id':'E007'},'e'),call('query_health_data',{'indicator':'glucose','mode':'compare_event','event_id':'E007','measurement_context':None},'c')])
  r=a.run('比较2026年10月1日二甲双胍调整前后血糖。')
  self.assertEqual(r['status'],'needs_clarification');self.assertEqual(r['pending']['missing_fields'],['measurement_context'])
  self.assertEqual(r['tool_results'][-1]['data']['comparisons'],[])
  self.assertNotIn('before',r['tool_results'][-1]['data'])

 def test_old_assistant_values_are_not_replayed_as_new_evidence(self):
  a=self.agent([call('query_health_data',{'indicator':'glucose','measurement_context':'fasting'},'g'),call('query_health_data',{'indicator':'weight'},'w'),final('体重77.5 kg。',['W016'])])
  a.run('最近一次空腹血糖是多少？');r=a.run('那最近一次体重呢？')
  request=a.provider.requests[-1]
  self.assertNotIn('6.7',json.dumps(request['messages'],ensure_ascii=False))
  self.assertIn('空腹血糖',json.dumps(request['messages'],ensure_ascii=False))
  self.assertEqual(r['status'],'ok')

 def test_wrong_model_event_for_explicit_conditions_is_corrected_without_reasking(self):
  a=self.agent([call('query_medications',{'mode':'events','medication_name':'阿托伐他汀'},'e'),call('query_health_data',{'indicator':'glucose','mode':'compare_event','event_id':'E004'},'wrong'),call('query_health_data',{'indicator':'glucose','mode':'compare_event','event_id':'E005'},'correct'),final('窗口内有其他事件冲突，不能比较。',['E005','E004','E006'])])
  r=a.run('比较2026年9月2日阿托伐他汀调整前后14天的血糖。')
  self.assertEqual(r['status'],'ok');self.assertIsNone(r['pending'])
  self.assertEqual(r['trace'][1]['error_code'],'event_mismatch')
  self.assertEqual(r['tool_results'][1]['data']['allowed_event_ids'],['E005'])
  self.assertEqual(r['tool_results'][2]['status'],'conflicting_events')

 def test_native_ask_cannot_repeat_an_event_already_explicit_in_user_request(self):
  invalid=call('ask_user',{'question':'选E004还是E005？','choices':['E004','E005'],'missing_fields':[]},'repeat')
  a=self.agent([call('query_medications',{'mode':'events','medication_name':'阿托伐他汀'},'e'),invalid,call('query_health_data',{'indicator':'glucose','mode':'compare_event','event_id':'E005'},'c'),final('存在其他用药事件冲突。',['E005','E004','E006'])])
  r=a.run('比较2026年9月2日阿托伐他汀调整前后14天的血糖。')
  self.assertEqual(r['status'],'ok');self.assertIsNone(r['pending'])
  self.assertEqual(r['trace'][1]['error_code'],'event_mismatch')
  self.assertEqual(r['tool_results'][-1]['status'],'conflicting_events')

 def test_conflict_terminal_state_ends_without_another_model_call(self):
  a=self.agent([call('query_medications',{'mode':'events','event_id':'E005'},'e'),call('query_health_data',{'indicator':'glucose','mode':'compare_event','event_id':'E005'},'c'),final('窗口存在其他事件冲突。',['E005','E004','E006'])])
  r=a.run('比较2026年9月2日阿托伐他汀调整前后14天的血糖。')
  self.assertEqual(r['status'],'ok');self.assertEqual(len(a.provider.requests),2)
  self.assertEqual(r['business_status'],'conflicting_events');self.assertEqual(r['answer_origin'],'business_receipt')
  self.assertEqual(a._tool_specs({'results':r['tool_results']}),[])

if __name__=='__main__':unittest.main()
