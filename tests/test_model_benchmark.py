import unittest
from health_agent.model_benchmark import grade

class BenchmarkGradingTests(unittest.TestCase):
    def test_text_without_tool_proof_cannot_pass(self):
        r={'status':'ok','answer':'6.9 mmol/L，条件未知','citations':[],'trace':[],'tool_results':[]}
        self.assertFalse(grade('T01',r)['passed'])
    def test_no_data_claim_requires_query_and_no_fabricated_value(self):
        r={'status':'ok','answer':'2024年没有记录，但血糖为6.9 mmol/L','citations':[],
           'tool_results':[{'status':'no_data','data':{'parameters':{'indicator':'glucose','start_date':'2024-01-01','end_date':'2025-01-01'}},'evidence':[]}], 'trace':[]}
        report=grade('T05',r)
        self.assertTrue(report['checks']['empty_year_query'])
        self.assertFalse(report['passed'])
    def test_clarification_cannot_hide_guessed_comparison(self):
        r={'status':'needs_clarification','needs_clarification':True,'answer':'请选事件','citations':[],
           'trace':[{'tool':'query_medications','arguments':{'mode':'events'}},{'tool':'query_health_data','arguments':{'mode':'compare_event'}}],'tool_results':[]}
        self.assertFalse(grade('T11',r)['passed'])
    def test_deterministic_goldens_pass_all_ten_grades(self):
        import tempfile
        from pathlib import Path
        from health_agent.config import load_config
        from health_agent.seed import initialize_database
        from health_agent.results import ToolContext
        from health_agent.tools import HealthTools
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'health.sqlite3';initialize_database(db)
            tools=HealthTools(db,context=ToolContext('P001','oracle',load_config().reference_date))
            for case_id in ['T01','T02','T03','T04','T05','T07','T10','T11','T12','T13']:
                trace=[];outputs=[]
                def query(name,args):
                    output=getattr(tools,name)(args);outputs.append(output)
                    trace.append({'tool':name,'arguments':args,'status':output['status']})
                if case_id in ('T01','T03'):
                    args={'indicator':'glucose','mode':'latest'}
                    if case_id=='T03':args.update(start_date='2026-10-22',end_date='2026-10-23')
                    query('query_health_data',args);answer='6.9 mmol/L，条件未知。'
                elif case_id=='T02':
                    query('query_health_data',{'indicator':'glucose','mode':'series','start_date':'2026-10-01','end_date':'2026-10-16','measurement_context':'fasting'})
                    answer='7.2、7.1、7.0、6.9、6.8、7.3、6.7、7.0 mmol/L。'
                elif case_id=='T04':
                    query('query_health_data',{'indicator':'glucose','requested_unit':'mg/dL'});answer='首版不支持单位换算。'
                elif case_id=='T05':
                    query('query_health_data',{'indicator':'glucose','mode':'series','start_date':'2024-01-01','end_date':'2025-01-01'});answer='2024年没有血糖记录。'
                elif case_id=='T07':
                    query('query_health_data',{'indicator':'blood_pressure'});answer='126/82 mmHg。'
                elif case_id=='T11':
                    query('query_medications',{'mode':'events'});answer='请明确选择哪个用药事件？'
                else:
                    event='E005' if case_id=='T13' else 'E007'
                    query('query_medications',{'mode':'events','event_id':event})
                    args={'indicator':'glucose','mode':'compare_event','event_id':event,'window_days':2 if case_id=='T12' else 14}
                    if case_id!='T13':args['measurement_context']='fasting'
                    query('query_health_data',args)
                    answer={'T10':'前后各8条，均值7.95至7.00 mmol/L，下降11.95%，不能推断因果。','T12':'前2条、后1条，样本不足，不能比较。','T13':'存在其他用药变更事件冲突，不能作变化结论。'}[case_id]
                result={'status':'needs_clarification' if case_id=='T11' else 'ok','needs_clarification':case_id=='T11',
                        'answer':answer,'citations':list({e['source_id'] for output in outputs for e in output['evidence']}),'tool_results':outputs,'trace':trace}
                report=grade(case_id,result)
                with self.subTest(case_id=case_id):self.assertTrue(report['passed'],report['checks'])
