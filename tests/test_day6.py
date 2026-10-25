from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
from unittest.mock import patch
import json,sqlite3,unittest
from health_agent.config import load_config,PROJECT_ROOT
from health_agent.results import ResultCache,ToolContext,ToolResult
from health_agent.cache import SQLiteResultCache
from health_agent.tools import HealthTools
from health_agent.seed import initialize_database
from health_agent.plotting import plot
from health_agent.verification import validate_answer
from health_agent.conversation import ConversationAgent
from tests.scripted_provider import ScriptedProvider,call,final

class Day6Tests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name);self.db=self.root/'health.sqlite3';initialize_database(self.db)
        self.ctx=ToolContext('P001','day6-test',load_config().reference_date)
        self.tools=HealthTools(self.db,context=self.ctx)
    def tearDown(self):self.temp.cleanup()
    def series(self,indicator='weight',**kw):return self.tools.query_health_data({'indicator':indicator,'mode':'series','start_date':'2026-10-01','end_date':'2026-10-24',**kw})
    def plot(self,result,**kw):return plot(self.tools,{'result_id':result['result_id'],**kw},output_root=self.root/'charts')
    def latest(self):return self.tools.query_health_data({'indicator':'glucose','mode':'latest'})
    def test_seven_points_exactly_reuse_cached_values(self):
        source=self.series();before=json.loads(json.dumps(source));r=self.plot(source)
        self.assertEqual(r['status'],'ok');chart=r['data']['chart'];self.assertEqual(chart['point_count'],7)
        self.assertEqual(chart['record_ids'],['W010','W011','W012','W013','W014','W015','W016'])
        self.assertEqual([p['value'] for p in chart['panels'][0]['points']],[row['value'] for row in before['data']['records']])
        self.assertEqual(source,before);self.assertEqual(Path(chart['image_path']).read_bytes()[:8],b'\x89PNG\r\n\x1a\n')
        self.assertIn('<svg',Path(chart['svg_path']).read_text());self.assertEqual(json.loads(Path(chart['data_path']).read_text())['point_count'],7)
    def test_single_point_only_marker(self):
        source=self.series(start_date='2026-10-20',end_date='2026-10-21');r=self.plot(source)
        self.assertEqual(r['data']['chart']['point_count'],1);self.assertFalse(r['data']['chart']['panels'][0]['line_drawn']);self.assertEqual(r['warnings'][0]['code'],'single_point')
    def test_scatter_never_draws_lines(self):
        r=self.plot(self.series(),chart_type='scatter');self.assertFalse(r['data']['chart']['panels'][0]['line_drawn'])
    def test_missing_and_forged_id_no_figure(self):
        for raw in [{},{'result_id':'res_forged'},{'result_id':'W016'},{'result_id':['res_1']}]:
            with self.subTest(raw=raw):self.assertEqual(plot(self.tools,raw,output_root=self.root/'charts')['status'],'error')
        self.assertFalse((self.root/'charts').exists())
    def test_model_cannot_send_rows_or_file_path(self):
        r=self.series()
        for extra in [{'records':[{'value':99}]},{'output_path':'/tmp/evil.png'},{'patient_id':'P002'},{'chart_type':'pie'}]:
            with self.subTest(extra=extra):self.assertEqual(plot(self.tools,{'result_id':r['result_id'],**extra},output_root=self.root/'charts')['status'],'error')
    def test_no_data_does_not_render(self):
        r=self.series(start_date='2024-01-01',end_date='2025-01-01');p=self.plot(r)
        self.assertEqual(p['status'],'no_data');self.assertNotIn('chart',p['data']);self.assertFalse((self.root/'charts').exists())
    def test_expired_id_does_not_render(self):
        now=[10.];self.tools.cache=ResultCache(ttl_seconds=5,clock=lambda:now[0]);r=self.series();now[0]=15.
        self.assertEqual(self.plot(r)['error']['code'],'invalid_result_id')
    def test_cross_session_patient_and_reference_are_rejected(self):
        r=self.series()
        for ctx in [replace(self.ctx,session_id='other'),replace(self.ctx,patient_id='P002'),replace(self.ctx,reference_date=self.ctx.reference_date.replace(day=22))]:
            self.tools.context=ctx;self.assertEqual(self.plot(r)['error']['code'],'invalid_result_id')
    def test_non_health_result_is_rejected(self):self.assertEqual(self.plot(self.tools.query_medications({}))['error']['code'],'invalid_result_id')
    def test_corrupted_units_patient_indicator_and_pairs_rejected(self):
        source=self.series()
        for key,value in [('unit','mmHg'),('patient_id','P002'),('indicator','glucose')]:
            r=json.loads(json.dumps(source));r['data']['records'][0][key]=value
            broken=ToolResult(r['status'],data=r['data'],evidence=r['evidence']);self.tools.cache.put(broken,context=self.ctx,tool='query_health_data')
            self.assertEqual(self.plot(broken.to_dict())['status'],'error')
    def test_blood_pressure_preserves_pairs_and_separate_panels(self):
        r=self.plot(self.series('blood_pressure'));chart=r['data']['chart']
        self.assertEqual(len(chart['panels']),2);self.assertEqual(chart['point_count'],chart['measurement_count']*2)
        panels={p['indicator']:p for p in chart['panels']}
        self.assertEqual([p['measurement_id'] for p in panels['systolic_blood_pressure']['points']],[p['measurement_id'] for p in panels['diastolic_blood_pressure']['points']])
        self.assertEqual({p['unit'] for p in chart['panels']},{'mmHg'})
    def test_glucose_conditions_have_separate_panels(self):
        source=self.series('glucose');r=self.plot(source);panels=r['data']['chart']['panels']
        self.assertGreater(len(panels),1);self.assertEqual(sum(p['point_count'] for p in panels),len(source['data']['records']))
        self.assertEqual({p['measurement_context'] for p in panels},{row['measurement_context'] for row in source['data']['records']})
    def test_chart_generation_is_idempotent(self):
        source=self.series();first=self.plot(source);image=Path(first['data']['chart']['image_path']);stamp=image.stat().st_mtime_ns
        second=self.plot(source);self.assertEqual(first['data']['chart'],second['data']['chart']);self.assertEqual(stamp,image.stat().st_mtime_ns)
    def test_durable_cache_recreated_agent_keeps_ownership(self):
        path=self.root/'cache.sqlite3';self.tools.cache=SQLiteResultCache(path,namespace=str(self.db));r=self.series()
        second=SQLiteResultCache(path,namespace=str(self.db));self.assertEqual(second.get(r['result_id'],context=self.ctx),r)
        for cache,ctx in [(SQLiteResultCache(path,namespace='other-db'),self.ctx),(second,replace(self.ctx,session_id='other'))]:
            with self.assertRaises(KeyError):cache.get(r['result_id'],context=ctx)
        self.assertEqual(path.stat().st_mode&0o777,0o600)
    def test_durable_expiry_and_defensive_copy(self):
        now=[100.];cache=SQLiteResultCache(self.root/'cache.sqlite3',namespace='db',ttl_seconds=5,clock=lambda:now[0]);self.tools.cache=cache;r=self.series()
        got=cache.get(r['result_id'],context=self.ctx);got['data']['records'].clear();self.assertEqual(cache.get(r['result_id'],context=self.ctx),r)
        now[0]=105.;other=SQLiteResultCache(self.root/'cache.sqlite3',namespace='db',clock=lambda:now[0])
        with self.assertRaises(KeyError):other.get(r['result_id'],context=self.ctx)
    def test_chart_does_not_requery_changed_database(self):
        self.tools.cache=SQLiteResultCache(self.root/'cache.sqlite3',namespace='db');r=self.series()
        with sqlite3.connect(self.db) as con:con.execute("UPDATE health_records SET value=999 WHERE indicator='weight'")
        self.tools=HealthTools(self.db,context=self.ctx,cache=SQLiteResultCache(self.root/'cache.sqlite3',namespace='db'))
        p=self.plot(r);self.assertEqual([x['value'] for x in p['data']['chart']['panels'][0]['points']],[x['value'] for x in r['data']['records']])
    def test_numeric_value_and_unit_checks(self):
        r=self.latest()
        self.assertTrue(validate_answer('最新血糖6.9 mmol/L。[G032]',['G032'],[r])['passed'])
        for text in ['最新血糖99 mmol/L。','最新血糖6.9 mg/dL。','最新血糖13 mg/dL。','最新血糖6.8 mmol/L。','最新血糖6.9 mmol/L，样本12条。']:
            with self.subTest(text=text):self.assertFalse(validate_answer(text,['G032'],[r])['passed'])
    def test_wrong_date_rejected(self):
        r=self.latest();self.assertFalse(validate_answer('测量于2024-10-22，6.9 mmol/L。',['G032'],[r])['passed'])
        self.assertTrue(validate_answer('2026年10月22日血糖6.9 mmol/L。',['G032'],[r])['passed'])
    def test_citation_cannot_borrow_other_query_value(self):
        glucose=self.latest();weight=self.tools.query_health_data({'indicator':'weight'})
        self.assertFalse(validate_answer('体重77.5 kg。',['G032'],[glucose,weight])['passed'])
    def test_report_values_require_report_label(self):
        r=ToolResult('ok',data={'mode':'semantic'},evidence=[{'source_type':'report_text','source_id':'R001:C001','record':{'text':'LDL-C为3.2 mmol/L。'}}]).to_dict()
        self.assertTrue(validate_answer('报告原文记录LDL-C为3.2 mmol/L。',['R001:C001'],[r])['passed'])
        self.assertFalse(validate_answer('您的最新血糖3.2 mmol/L。',['R001:C001'],[r])['passed'])
    def test_rounded_percent_and_comparison_side(self):
        r=self.tools.query_health_data({'indicator':'glucose','mode':'compare_event','event_id':'E007','measurement_context':'fasting'})
        self.assertTrue(validate_answer('前均值7.95，后均值7.00 mmol/L，下降11.95%。',['G001','G009'],[r])['passed'])
        self.assertFalse(validate_answer('前均值7.00，后均值7.95 mmol/L，上升11.95%。',['G001','G009'],[r])['passed'])
    def test_number_correction_then_fallback_after_second_failure(self):
        p=ScriptedProvider([call('query_health_data',{'indicator':'glucose'}),final('血糖99 mmol/L。',['G032']),final('血糖99 mmol/L。',['G032'])])
        a=ConversationAgent(self.tools,p,checkpoint_path=self.root/'checkpoint.sqlite3');r=a.run('分析最新血糖。');a.close()
        self.assertEqual(r['error']['code'],'unverified_facts');self.assertNotIn('99',r['answer']);self.assertEqual(r['fallback_facts'][0]['record']['value'],6.9)
        self.assertEqual(r['model_calls'],3)
    def test_numeric_rewrite_can_succeed_once(self):
        p=ScriptedProvider([call('query_health_data',{'indicator':'glucose'}),final('血糖99 mmol/L。',['G032']),final('血糖6.9 mmol/L。',['G032'])])
        a=ConversationAgent(self.tools,p,checkpoint_path=self.root/'cp.sqlite3');r=a.run('分析最新血糖。');a.close();self.assertEqual(r['status'],'ok');self.assertEqual(r['model_calls'],3)
    def test_agent_can_use_prior_durable_query_to_plot_without_new_sql(self):
        cache=self.root/'cache.sqlite3';self.tools.cache=SQLiteResultCache(cache,namespace=str(self.db));source=self.series()
        p=ScriptedProvider([call('plot_health_trend',{'result_id':source['result_id'],'chart_type':'line'}),final('图表已生成，包含7个体重数据点。',['W010','W016'])])
        a=ConversationAgent(self.tools,p,checkpoint_path=self.root/'cp.sqlite3')
        with patch('health_agent.plotting.PLOT_ROOT',self.root/'charts'):r=a.run('把刚才的数据画出来。')
        a.close();self.assertEqual(r['status'],'ok');self.assertEqual([x['tool'] for x in r['trace']],['plot_health_trend'])
    def test_chart_ui_displays_image_and_downloads(self):
        from streamlit.testing.v1 import AppTest
        source=self.series();chart=self.plot(source)
        answer={'status':'ok','answer':'图表已生成，7个数据点。','trace':[],'tool_results':[chart]}
        class UIOnlyAgent:
            def run(self,q):return answer
        with patch('health_agent.plotting.PLOT_ROOT',self.root/'charts'),patch('health_agent.config.DEFAULT_DATABASE',self.db),patch('health_agent.app_support.configuration_status',return_value={'configured':True,'model':'test-double-only'}),patch('health_agent.app_support.create_session_agent',return_value=UIOnlyAgent()):
            app=AppTest.from_file(str(PROJECT_ROOT/'streamlit_app.py'),default_timeout=15).run();app.chat_input[0].set_value('画体重图').run()
            self.assertEqual(len(app.exception),0);self.assertEqual(len(app.get('image')),1);self.assertEqual(len(app.get('download_button')),3);self.assertEqual(len(app.dataframe),1)

    def test_unsupported_derived_statistic_is_rejected(self):
        self.assertFalse(validate_answer('平均值6.9 mmol/L。',['G032'],[self.latest()])['passed'])
    def test_single_point_cannot_be_called_a_trend(self):
        chart=self.plot(self.series(start_date='2026-10-20',end_date='2026-10-21'))
        self.assertFalse(validate_answer('体重呈下降趋势。',['W016'],[chart])['passed'])
        self.assertTrue(validate_answer('只有1个数据点，无法判断下降趋势。',['W016'],[chart])['passed'])
    def test_cancelling_session_clears_durable_snapshots(self):
        self.tools.cache=SQLiteResultCache(self.root/'cache.sqlite3',namespace=str(self.db));source=self.series()
        a=ConversationAgent(self.tools,ScriptedProvider([]),checkpoint_path=self.root/'cp.sqlite3');a.run('取消任务');a.close()
        with self.assertRaises(KeyError):self.tools.cache.get(source['result_id'],context=self.ctx)
    def test_report_full_scan_citation_can_support_same_report_matched_line(self):
        r=ToolResult('ok',data={'mode':'mention_check'},evidence=[{'source_type':'report_scan','source_id':'R001:SCAN','record':{'coverage':'full_text'}},{'source_type':'report_text','source_id':'R001:L8','record':{'report_id':'R001','text':'LDL-C为3.2 mmol/L。'}}]).to_dict()
        self.assertTrue(validate_answer('报告原文记录LDL-C为3.2 mmol/L。',['R001:SCAN'],[r])['passed'])

    def test_exclusive_range_cannot_exclude_actual_last_record(self):
        source=self.series();ids=['W010','W016']
        for text in ['已生成2026年10月1日至23日的图表（右端日期不含当日）。','记录时间2026-10-02至2026-10-20（右端不含）。']:
            self.assertFalse(validate_answer(text,ids,[source])['passed'])
        self.assertTrue(validate_answer('查询2026-10-01至2026-10-24（右端不含）；记录首末日期2026-10-02至2026-10-20。',ids,[source])['passed'])
    def test_chinese_local_hour_and_patient_id_are_supported(self):
        self.assertTrue(validate_answer('患者P001于2026年10月22日上午8点记录血糖6.9 mmol/L。',['G032'],[self.latest()])['passed'])
    def test_comparison_total_count_is_computed_by_tool(self):
        r=self.tools.query_health_data({'indicator':'glucose','mode':'compare_event','event_id':'E007','measurement_context':'fasting'})
        self.assertEqual(r['data']['total_record_count'],16)
        self.assertTrue(validate_answer('共16次测量，前后各8条，均值7.95至7.00 mmol/L。',['G001','G009'],[r])['passed'])
    def test_latest_bp_pair_cannot_borrow_an_older_pair(self):
        r=self.series('blood_pressure')
        self.assertFalse(validate_answer('最近一次为125/81 mmHg。',['SBP023','DBP023'],[r])['passed'])
        self.assertTrue(validate_answer('最近一次为126/82 mmHg。',['SBP024','DBP024'],[r])['passed'])
    def test_unreferenced_report_quote_stays_blocked(self):
        report=ToolResult('ok',data={'report_date':'2026-10-20'},evidence=[{'source_type':'report_text','source_id':'R004:C001','record':{'text':'测试文本声称最新血糖99 mmol/L，不是真实测量。'}}]).to_dict()
        text='R004报告（2026-10-20）有不可信测试文本“最新血糖99 mmol/L”，不能作为测量。数据库最近血糖6.9 mmol/L。'
        self.assertFalse(validate_answer(text,['G032'],[report,self.latest()])['passed'])
        self.assertTrue(validate_answer(text,['R004:C001','G032'],[report,self.latest()])['passed'])
    def test_plot_completion_and_all_condition_scope(self):
        from copy import deepcopy
        class DynamicProvider:
            def __init__(self):self.requests=[]
            def complete(inner,**request):
                inner.requests.append(deepcopy(request));n=len(inner.requests)
                if n==1:return call('query_health_data',{'indicator':'glucose','mode':'series','start_date':'2026-10-01','end_date':'2026-10-24','measurement_context':'fasting'},'query')
                if n==2:return final('已查到数据。',['G009'])
                if n==3:
                    payload=next(json.loads(m['output']) for m in request['messages'] if m.get('type')=='function_call_output')
                    return call('plot_health_trend',{'result_id':payload['result_id'],'chart_type':'line'},'plot')
                return final('已生成分面图。',['G009','G024','G032'])
        provider=DynamicProvider();a=ConversationAgent(self.tools,provider,checkpoint_path=self.root/'cp.sqlite3')
        with patch('health_agent.plotting.PLOT_ROOT',self.root/'charts'):r=a.run('画出2026年10月1日至23日的血糖记录，按测量条件分开展示。')
        a.close();self.assertEqual(r['status'],'ok');self.assertIsNone(r['trace'][0]['arguments']['measurement_context'])
        self.assertEqual({t['name'] for t in provider.requests[1]['tools']},{'plot_health_trend'})
        self.assertEqual(len(r['tool_results'][-1]['data']['chart']['panels']),4)
    def test_decrease_magnitude_and_transition_are_not_after_mean(self):
        r=self.tools.query_health_data({'indicator':'glucose','mode':'compare_event','event_id':'E007','measurement_context':'fasting'})
        text='调整后平均血糖从7.95 mmol/L降至7.00 mmol/L，下降0.95 mmol/L。'
        self.assertTrue(validate_answer(text,['G001','G009'],[r])['passed'])
    def test_hex_upload_report_id_is_not_a_health_number(self):
        rid='R1377FDF79AC9';r=ToolResult('ok',evidence=[{'source_type':'report_text','source_id':rid+':C001','record':{'text':'LDL-C为3.2 mmol/L。'}}]).to_dict()
        self.assertTrue(validate_answer('报告'+rid+'原文记录LDL-C为3.2 mmol/L。',[rid+':C001'],[r])['passed'])
    def test_native_chart_message_is_compact_without_mutating_artifacts(self):
        from health_agent.ollama_provider import native_messages
        q=self.series();p=self.plot(q)
        messages=[{'type':'function_call','name':'query_health_data','call_id':'q','arguments':'{}'},{'type':'function_call_output','call_id':'q','output':json.dumps(q)},
                  {'type':'function_call','name':'plot_health_trend','call_id':'p','arguments':'{}'},{'type':'function_call_output','call_id':'p','output':json.dumps(p)}]
        native=native_messages('test',messages);wire=json.loads(native[-1]['content'])
        self.assertNotIn('points',wire['data']['chart']['panels'][0]);self.assertIn('first_point',wire['data']['chart']['panels'][0])
        self.assertEqual(len(p['data']['chart']['panels'][0]['points']),7)
        self.assertNotIn('records',json.loads(native[2]['content'])['data'])
    def test_same_batch_scope_queries_reuse_one_snapshot(self):
        from health_agent.provider import ModelReply
        class DynamicProvider:
            requests=[]
            def complete(inner,**request):
                inner.requests.append(request);n=len(inner.requests)
                if n==1:
                    replies=[call('query_health_data',{'indicator':'glucose','mode':'series','start_date':'2026-10-01','end_date':'2026-10-24','measurement_context':ctx},'q'+str(i)) for i,ctx in enumerate(['fasting','postprandial','random'])]
                    return ModelReply(output_items=[o for r in replies for o in r.output_items],calls=[c for r in replies for c in r.calls])
                if n==2:
                    outputs=[json.loads(m['output']) for m in request['messages'] if m.get('type')=='function_call_output']
                    if len({r['result_id'] for r in outputs})!=1:raise AssertionError('duplicate SQL snapshots')
                    return call('plot_health_trend',{'result_id':outputs[0]['result_id'],'chart_type':'line'},'plot')
                return final('已生成图表。',['G009','G032'])
        provider=DynamicProvider();a=ConversationAgent(self.tools,provider,checkpoint_path=self.root/'cp.sqlite3')
        with patch('health_agent.plotting.PLOT_ROOT',self.root/'charts'),patch.object(self.tools,'query_health_data',wraps=self.tools.query_health_data) as query:r=a.run('画出血糖趋势图，按测量条件分开展示。')
        a.close();self.assertEqual(r['status'],'ok');self.assertEqual(query.call_count,1);self.assertEqual(r['tool_calls'],4)
        self.assertEqual(len(provider.requests),2);self.assertEqual(r['answer_origin'],'tool_receipt')

    def test_chart_only_ends_with_explicit_tool_receipt(self):
        source=self.series();provider=ScriptedProvider([call('plot_health_trend',{'result_id':source['result_id'],'chart_type':'line'})])
        a=ConversationAgent(self.tools,provider,checkpoint_path=self.root/'cp.sqlite3')
        with patch('health_agent.plotting.PLOT_ROOT',self.root/'charts'):r=a.run('把刚才的数据画出来。')
        a.close();self.assertEqual(r['answer_origin'],'tool_receipt');self.assertTrue(r['artifact_completed']);self.assertEqual(len(provider.requests),1)
        self.assertIn('2026-10-20',r['answer']);self.assertIsNone(r['model_answer_accepted'])
