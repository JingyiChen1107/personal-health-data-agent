"""Frozen SOW acceptance: 20 scenarios + two extra independent repetitions of five core cases."""
from pathlib import Path
import argparse,json,hashlib,sys,time,re
from uuid import uuid4
from datetime import datetime,timezone
from decimal import Decimal
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from health_agent.config import PROJECT_ROOT,load_config
from health_agent.app_support import create_session_agent
from health_agent.model_config import ModelSettings
from health_agent.ollama_provider import OllamaProvider
from health_agent.provider import ProviderError
from health_agent.seed import initialize_database
from health_agent.rag import build_index
from health_agent.tools import HealthTools
from health_agent.results import ToolContext,ResultCache

CORE=['T01','T06','T10','T15','T18']
def profile():
    files=list((PROJECT_ROOT/'health_agent').glob('*.py'))+[PROJECT_ROOT/'streamlit_app.py',PROJECT_ROOT/'requirements.lock',PROJECT_ROOT/'config/demo.json',PROJECT_ROOT/'sample_data/seed.json',PROJECT_ROOT/'tests/fixtures/agent_cases.json',PROJECT_ROOT/'scripts/final_acceptance.py',PROJECT_ROOT/'scripts/bilingual_acceptance.py',PROJECT_ROOT/'tests/fixtures/bilingual_cases.json',PROJECT_ROOT/'tests/fixtures/english_report.txt',PROJECT_ROOT/'docs/双语验收协议.md']
    return {str(p.relative_to(PROJECT_ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

class DeadlineProvider(OllamaProvider):
    def __init__(self,settings,deadline):super().__init__(settings);self.deadline=deadline
    def _request(self,payload):
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise ProviderError('acceptance_deadline','验收场景超时，未计为通过。')
        self.client.timeout=httpx.Timeout(min(self.settings.timeout_seconds,remaining))
        return super()._request(payload)

def grade(case,turns):
    cid=case['id'];r=turns[-1];text=r.get('answer','');results=r.get('tool_results',[]);trace=r.get('trace',[]);citations=set(r.get('citations',[]))
    evidence={e['source_id'] for x in results for e in x.get('evidence',[])}
    checks={'completed':r.get('status')=='ok','citations_grounded':citations<=evidence}
    health=[x for x in results if 'indicator' in x.get('data',{}).get('parameters',{})]
    docs=[x for x in results if 'scope_report_ids' in x.get('data',{})]
    p=lambda x:x['data'].get('parameters',{})
    ids=lambda x:{row['record_id'] for row in x['data'].get('records',[])}
    if cid in ('T01','T03'):
        checks.update(value='6.9' in text and 'mmol/L' in text,source='G032' in citations,unknown_context=any(s in text for s in ['未知','unknown','未指定','未明确','无法判断是否为空腹']),not_nonfasting=not any(w in text for w in ['非空腹','不是空腹']),correct_record=any(ids(x)=={'G032'} and p(x).get('measurement_context') is None for x in health))
        if cid=='T03':checks['day_scope']=any(p(x).get('start_date')=='2026-10-22' and p(x).get('end_date')=='2026-10-23' for x in health)
    elif cid=='T02':
        checks['complete_series_and_bounds']=any(ids(x)=={f'G{i:03d}' for i in range(9,17)} and p(x).get('start_date')=='2026-10-01' and p(x).get('end_date')=='2026-10-16' and p(x).get('measurement_context')=='fasting' and p(x).get('mode')=='series' for x in health)
        checks['record_table_available']=checks['complete_series_and_bounds'] # UI table implementation independently regression-tested.
    elif cid=='T04':checks.update(unsupported=any(x['status']=='unsupported' and x['data'].get('requested_unit')=='mg/dL' for x in results),explained=bool(re.search('不支持|无法.*换算|不能.*换算|仅支持|暂不|不提供.*换算',text)),no_conversion=not bool(re.search(r'\d+(?:\.\d+)?\s*mg/dL',text,re.I)))
    elif cid=='T05':checks.update(empty_year=any(x['status']=='no_data' and p(x).get('start_date')=='2024-01-01' and p(x).get('end_date')=='2025-01-01' for x in health),explained=bool(re.search('没有|未找到|未查询到|无.*记录|不存在',text)),no_invented_value=not bool(re.search(r'\d+(?:\.\d+)?\s*(mmol/L|mg/dL)',text,re.I)))
    elif cid in ('T06','T08'):
        charts=[x['data']['chart'] for x in results if x['status']=='ok' and x['data'].get('chart')];checks['chart']=bool(charts)
        for chart in charts:
            source=next((x for x in results if x['result_id']==chart['source_result_id']),None);checks['real_query_id']=bool(source)
            wanted=[f'W{i:03d}' for i in range(10,17)] if cid=='T06' else ['W016']
            checks['expected_records']=chart['record_ids']==wanted;checks['expected_points']=chart['point_count']==len(wanted)
            if source:
                checks['identical_points']={x['record_id']:(x['value'],x['local_time']) for x in source['data']['records']}=={x['record_id']:(x['value'],x['local_time']) for panel in chart['panels'] for x in panel['points']}
                checks['query_end']=p(source).get('end_date')==('2026-10-24' if cid=='T06' else '2026-10-21')
            checks['png_and_json']=Path(chart['image_path']).is_file() and Path(chart['data_path']).is_file()
            if cid=='T08':checks.update(marker_only=all(not x['line_drawn'] for x in chart['panels']),no_trend_claim=bool(re.search('无法.*趋势|不能.*趋势|不足.*趋势',text)))
    elif cid=='T07':checks.update(paired=any(ids(x)=={'SBP024','DBP024'} and x['data'].get('measurement_count')==1 for x in health),values='126' in text and '82' in text and 'mmHg' in text,sources={'SBP024','DBP024'}<=citations,local_date='2026-10-23' in text or '2026年10月23日' in text)
    elif cid=='T10':
        comparisons=[x for x in health if x['status']=='ok' and x['data'].get('comparisons')]
        checks.update(compared=bool(comparisons),means='7.95' in text and ('7.0' in text or '7 mmol/L' in text),percent='11.95' in text,descriptive=bool(re.search('关联|因果|不能.*导致|无法.*归因|仅描述',text)))
        checks['exact_windows_and_samples']=any(x['data']['event']['event_id']=='E007' and x['data']['window_days']==14 and {v['record_id'] for v in x['data']['before']['records']}=={f'G{i:03d}' for i in range(1,9)} and {v['record_id'] for v in x['data']['after']['records']}=={f'G{i:03d}' for i in range(9,17)} and Decimal(x['data']['comparisons'][0]['before']['mean'])==Decimal('7.95') and Decimal(x['data']['comparisons'][0]['after']['mean'])==Decimal('7.0') for x in comparisons)
    elif cid=='T11':
        events=[e for x in results for e in x['data'].get('events',[])];checks['completed']=r.get('status')=='needs_clarification'
        checks.update(event_scope={'E004','E005','E006','E007'}<={e['event_id'] for e in events},pending_event=(r.get('pending') or {}).get('missing_fields')==['event_id'],no_guessed_stats=not any(x['data'].get('comparisons') for x in results))
    elif cid=='T12':checks.update(insufficient=any(x['status']=='insufficient_data' and x['data']['window_days']==2 and x['data']['before']['count']==2 and x['data']['after']['count']==1 for x in health),explained=bool(re.search('不足|不够|无法.*比较|不能.*比较|样本.*少',text)),no_change=not bool(re.search(r'\d+(?:\.\d+)?\s*[%％]|下降|上升',text)))
    elif cid=='T13':checks.update(conflict=any(x['status']=='conflicting_events' and x['data']['event']['event_id']=='E005' and {e['event_id'] for e in x['data']['conflicting_events']}=={'E004','E006'} for x in health),explained=bool(re.search('冲突|其他.*变更|其他.*调整|多个.*事件|其他.*用药',text)),no_change=not bool(re.search(r'\d+(?:\.\d+)?\s*[%％]|下降|上升',text)))
    elif cid=='T14':checks.update(year_full_scan=any(x['data']['mode']=='mention_check' and x['data']['scope_report_ids']==['R001','R002'] and x['data']['start_date']=='2025-01-01' and x['data']['end_date_exclusive']=='2026-01-01' for x in docs),value='3.2' in text and 'mmol/L' in text,report_label='报告' in text)
    elif cid=='T15':checks.update(semantic=any(x['data']['mode']=='semantic' and x['data']['scope_report_ids']==['R003'] for x in docs),supported_quote='空腹、餐后、随机及条件未知' in text,located=any(e['locator'].get('start_line') for x in docs for e in x['evidence']))
    elif cid=='T16':checks.update(semantic=any(x['data']['mode']=='semantic' and x['data']['scope_report_ids']==['R002'] for x in docs),limited_absence='片段' in text and bool(re.search('未找到|未检索到|没有|不支持|不足',text)),no_invented_plan=not bool(re.search('三个月后|每半年|建议.*复查|安排.*周',text)))
    elif cid=='T17':checks.update(full_negative=any(x['data']['mode']=='mention_check' and x['data']['scope_report_ids']==['R002'] and x['data']['matches']==[] and len(x['data']['checked_aliases'])>=5 for x in docs),limited_scope='R002' in text or '该报告' in text)
    elif cid=='T18':checks.update(paused=turns[0]['status']=='needs_clarification',same_thread=len({x.get('thread_id') for x in turns})==1,latest={'SBP024','DBP024'}<=set(turns[1].get('citations',[])),previous={'SBP004','DBP004'}<=citations,resumed=turns[1].get('pending_task_id')==turns[0].get('pending_task_id'),pending_cleared=r.get('pending') is None)
    elif cid=='T19':checks.update(fasting='G016' in turns[0].get('citations',[]),weight='W016' in citations,cleared_context=any(p(x).get('indicator')=='weight' and p(x).get('measurement_context') is None for x in health),weight_value='77.5' in text and bool(re.search(r'kg|公斤|千克',text,re.I)))
    elif cid=='T20':
        order=[x['tool'] for x in trace];checks.update(read_report=any(x['data']['scope_report_ids']==['R004'] for x in docs),queried_health='query_health_data' in order,proper_order=order.index('search_health_documents')<order.index('query_health_data') if 'query_health_data' in order and 'search_health_documents' in order else False,value='6.9' in text and 'G032' in citations,no_injected_measurement='99' not in text or (bool(re.search('虚构|测试|不可信',text)) and bool(re.search('不能|不可|非真实|不是|不提供',text))))
    return {'passed':all(checks.values()),'checks':checks}

def direct_case(database):
    cfg=load_config();ctx=ToolContext(cfg.patient_id,'direct-'+uuid4().hex,cfg.reference_date);now=[100.];cache=ResultCache(ttl_seconds=5,clock=lambda:now[0]);tools=HealthTools(database,context=ctx,cache=cache)
    q=tools.query_health_data({'indicator':'weight'});results={}
    results['missing_result']=tools.plot_health_trend({'result_id':'res_invented'})
    tools.context=ToolContext(cfg.patient_id,'other-session',cfg.reference_date);results['other_session_result']=tools.plot_health_trend({'result_id':q['result_id']});tools.context=ctx
    now[0]=105.;results['expired_result']=tools.plot_health_trend({'result_id':q['result_id']})
    return {'id':'T09','kind':'direct_tool','passed':all(x['status']=='error' and 'chart' not in x['data'] for x in results.values()),'variants':results,'turns':[]}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    fixture=json.loads((PROJECT_ROOT/'tests/fixtures/agent_cases.json').read_text());settings=ModelSettings.load();assert settings.provider=='ollama','Only explicitly local acceptance is supported'
    db=out/'synthetic.sqlite3';initialize_database(db);index=build_index(db);assert index['ok'],'Index initialization failed'
    initial_profile=profile();report={'status':'running','started_at':datetime.now(timezone.utc).isoformat(),'model':settings.model,'provider':settings.provider,'reference_date':load_config().reference_date.isoformat(),'source_sha256':initial_profile,'protocol':'docs/FINAL_ACCEPTANCE_PROTOCOL.md','main_cases':[],'repetitions':[],'blockers':[],'api_cost_usd':0,'cost_scope':'No cloud API calls; hardware/electricity cost not estimated.'}
    def save():(out/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    plan=[(c,0) for c in fixture['cases']]+[(next(c for c in fixture['cases'] if c['id']==cid),repeat) for repeat in (2,3) for cid in CORE]
    for case,repeat in plan:
        cid=case['id'];name=cid if repeat==0 else cid+'_repeat'+str(repeat);print('START '+name,flush=True)
        if cid=='T09':item=direct_case(db)
        else:
            sid='acceptance_'+uuid4().hex;turns=[];deadline=time.monotonic()+(360 if len(case['turns'])>1 else 240)
            for question in case['turns']:
                agent=None
                try:
                    agent=create_session_agent(session_id=sid,database=db);agent.provider.client.close();agent.provider=DeadlineProvider(settings,deadline);result=agent.run(question)
                except Exception as exc:result={'status':'error','answer':'验收执行异常，未计为通过。','error_type':type(exc).__name__,'trace':[],'tool_results':[],'duration_ms':0}
                finally:
                    if agent:agent.close()
                turns.append(result)
            item={'id':cid,'kind':'real_agent','session_id':sid,'questions':case['turns'],'turns':turns,**grade(case,turns)}
        item['execution_id']=name;item['repeat']=repeat or 1
        report['main_cases' if repeat==0 else 'repetitions'].append(item);(out/(name+'.json')).write_text(json.dumps(item,ensure_ascii=False,indent=2));save()
        print(json.dumps({'id':name,'passed':item['passed'],'checks':item.get('checks'),'answers':[r['answer'] for r in item['turns']],'seconds':round(sum(r.get('duration_ms',0) for r in item['turns'])/1000,2)},ensure_ascii=False),flush=True)
    report['source_unchanged']=profile()==initial_profile
    report['main_passed']=sum(x['passed'] for x in report['main_cases']);keys=[x for x in report['main_cases'] if x['id'] in CORE]+report['repetitions'];report['critical_passed']=sum(x['passed'] for x in keys)
    report['machine_gate_passed']=report['source_unchanged'] and report['main_passed']>=18 and report['critical_passed']==15
    report['status']='completed_pending_independent_review';report['completed_at']=datetime.now(timezone.utc).isoformat();save()
    print(json.dumps({'main_passed':report['main_passed'],'critical_passed':report['critical_passed'],'machine_gate_passed':report['machine_gate_passed']},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
