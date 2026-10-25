"""Reproducible Day-3 local-model comparison; synthetic database, fixed tasks/profile."""
from __future__ import annotations
import argparse
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import platform
import re
import statistics
import time
from uuid import uuid4
import httpx
from health_agent.agent import HealthAgent
from health_agent.config import PROJECT_ROOT, DEFAULT_DATABASE, load_config
from health_agent.evaluation import grade_smoke,SMOKE_QUESTIONS
from health_agent.model_config import ModelSettings
from health_agent.ollama_provider import OllamaProvider
from health_agent.provider import ProviderError
from health_agent.results import ToolContext
from health_agent.tools import HealthTools

CASE_IDS=['T01','T02','T03','T04','T05','T07','T10','T11','T12','T13']

def grade(case_id,result):
    answer=result.get('answer','')
    numbers={Decimal(x) for x in re.findall(r'(?<![A-Za-z])\d+(?:\.\d+)?',answer)}
    records=result.get('tool_results',[])
    traces=result.get('trace',[])
    citations=set(result.get('citations',[]))
    evidence={e['source_id'] for r in records for e in r.get('evidence',[])}
    checks={'completed':result.get('status')!='error','citations_grounded':citations<=evidence}
    health=[r for r in records if 'parameters' in r.get('data',{}) and 'indicator' in r['data']['parameters']]
    def parameters(r): return r.get('data',{}).get('parameters',{})
    if case_id in ('T01','T03','T10'):
        checks.update(grade_smoke('T01' if case_id=='T03' else case_id,result)['checks'])
        if case_id in ('T01','T03'):
            checks['unknown_not_fasting']=bool(re.search('未知|未明确|未记录|不明确|未注明|无法确认',answer))
        if case_id=='T03':
            checks['exact_day_bounds']=any(parameters(r).get('start_date')=='2026-10-22' and parameters(r).get('end_date')=='2026-10-23' for r in health)
    elif case_id=='T02':
        expected={f'G{i:03}' for i in range(9,17)}
        checks['correct_series']=any({row['record_id'] for row in r['data'].get('records',[])}==expected and parameters(r).get('measurement_context')=='fasting' for r in health)
        checks['exclusive_end']=any(parameters(r).get('start_date')=='2026-10-01' and parameters(r).get('end_date')=='2026-10-16' for r in health)
        checks['values_in_answer']={Decimal(x) for x in ['7.2','7.1','7.0','6.9','6.8','7.3','6.7']}<=numbers
        checks['series_sources']=expected<=citations
    elif case_id=='T04':
        checks['unsupported_unit_tool']=any(r['status']=='unsupported' and r.get('data',{}).get('requested_unit')=='mg/dL' for r in records)
        checks['refusal_explained']=bool(re.search('不支持|不能.*换算|无法.*换算|不提供.*换算|仅支持|暂不',answer))
        checks['no_converted_value']=not bool(re.search(r'\d+(?:\.\d+)?\s*mg/dL',answer,re.I))
    elif case_id=='T05':
        checks['empty_year_query']=any(r['status']=='no_data' and parameters(r).get('start_date')=='2024-01-01' and parameters(r).get('end_date')=='2025-01-01' for r in health)
        checks['absence_explained']=bool(re.search('没有|未找到|无.*记录|不存在|未查询到',answer))
        checks['no_invented_measurement']=not bool(re.search(r'\d+(?:\.\d+)?\s*(mmol/L|mg/dL)',answer,re.I))
    elif case_id=='T07':
        checks['paired_bp']=any({'SBP024','DBP024'}<={row['record_id'] for row in r['data'].get('records',[])} and r['data'].get('measurement_count')==1 for r in health)
        checks['correct_pair_in_answer']={Decimal(126),Decimal(82)}<=numbers and 'mmHg' in answer
        checks['paired_citations']={'SBP024','DBP024'}<=citations
    elif case_id=='T11':
        checks['events_queried']=any(t['tool']=='query_medications' and t['arguments'].get('mode')=='events' for t in traces)
        checks['asks_clarification']=result.get('needs_clarification') is True
        checks['no_guessed_comparison']=not any(t['arguments'].get('mode')=='compare_event' for t in traces)
    elif case_id=='T12':
        checks['small_window_honored']=any(r['status']=='insufficient_data' and r['data'].get('window_days')==2 and r['data']['before'].get('count')==2 and r['data']['after'].get('count')==1 for r in health)
        checks['insufficiency_explained']=bool(re.search('不足|不够|无法.*比较|不能.*比较|样本.*少',answer))
        checks['no_change_conclusion']='%' not in answer and not bool(re.search('下降|上升|降低了|升高了',answer))
    elif case_id=='T13':
        checks['conflicts_detected']=any(r['status']=='conflicting_events' and r['data'].get('event',{}).get('event_id')=='E005' and {e['event_id'] for e in r['data'].get('conflicting_events',[])}=={'E004','E006'} for r in health)
        checks['conflict_explained']=bool(re.search('冲突|其他.*调整|其他.*变更|其他.*用药|多.*变更|多个.*事件',answer))
        checks['no_change_conclusion']='%' not in answer and not bool(re.search('下降|上升|降低了|升高了',answer))
    return {'case_id':case_id,'passed':all(checks.values()),'checks':checks}

class BoundedProvider(OllamaProvider):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.deadline=time.monotonic()+120
    def _request(self,payload):
        remaining=self.deadline-time.monotonic()
        if remaining<=0: raise ProviderError('benchmark_deadline','本轮比较测试达到120秒上限。')
        self.client.timeout=httpx.Timeout(min(60,remaining))
        return super()._request(payload)

def run(model,output,case_ids=None,screen=False):
    case_ids=case_ids or CASE_IDS
    if screen:
        case_ids=['T01','T07','T10']+[item for item in case_ids if item not in ('T01','T07','T10')]
    fixture=json.loads((PROJECT_ROOT/'tests/fixtures/agent_cases.json').read_text())
    questions={c['id']:c['turns'][0] for c in fixture['cases']}
    questions.update(SMOKE_QUESTIONS)
    cfg=replace(load_config(),max_retries=0,max_tool_calls=4)
    settings=ModelSettings.load(env={'LLM_PROVIDER':'ollama','LLM_MODEL':model,'LLM_MAX_OUTPUT_TOKENS':'1024','OLLAMA_TIMEOUT_SECONDS':'60'})
    profile=hashlib.sha256(b''.join((PROJECT_ROOT/'health_agent'/f).read_bytes() for f in ['ollama_provider.py','prompts.py','tool_specs.py','model_benchmark.py'])).hexdigest()
    report={'model':model,'status':'running','profile_sha256':profile,'environment':{'machine':platform.machine(),'macos':platform.mac_ver()[0]},
            'settings':{'context':8192,'temperature':0,'max_output_tokens':1024,'max_tool_calls':4,'retries':0,'request_timeout':60,'case_deadline':120},
            'scope':'Day3 supported single-turn tasks; T11 clarification output adapted, not formal HITL. RAG/plots/multi-turn excluded.', 'cases':[]}
    output.parent.mkdir(parents=True,exist_ok=True)
    def save(): output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    with httpx.Client(base_url=settings.base_url,trust_env=False,timeout=60) as client:
        report['model_metadata']=next((item for item in client.get('/api/tags').json()['models'] if item['name']==model),{})
        running=client.get('/api/ps').json().get('models',[])
        report['unloaded_before_warmup']=[item['name'] for item in running]
        for item in running:
            client.post('/api/generate',json={'model':item['name'],'stream':False,'keep_alive':0}).raise_for_status()
        started=time.monotonic()
        try:
            warm=client.post('/api/chat',json={'model':model,'messages':[{'role':'user','content':'Reply OK.'}],'stream':False,'think':False,'keep_alive':'5m','options':{'temperature':0,'num_ctx':8192,'num_predict':16}})
            warm.raise_for_status()
        except Exception as exc:
            report['status']='unavailable';report['warmup_error']=type(exc).__name__;report['warmup_seconds']=round(time.monotonic()-started,3);save();print(model,'unavailable',flush=True);return report
        report['warmup_seconds']=round(time.monotonic()-started,3)
    for case_id in case_ids:
        provider=BoundedProvider(settings)
        context=ToolContext(cfg.patient_id,uuid4().hex,cfg.reference_date)
        try:
            result=HealthAgent(HealthTools(DEFAULT_DATABASE,context=context,config=cfg),provider).run(questions[case_id])
        finally: provider.client.close()
        case=grade(case_id,result);case['question']=questions[case_id];case['result']=result
        report['cases'].append(case);save()
        print(model,case_id,'PASS' if case['passed'] else 'FAIL',round(result['duration_ms']/1000,2),flush=True)
        if screen and case_id in ('T01','T07','T10') and not case['passed']:
            report.update(status='screened_out',passed=sum(c['passed'] for c in report['cases']),total=len(report['cases']),planned_total=len(case_ids),not_run=[item for item in case_ids if item not in {c['case_id'] for c in report['cases']}],screen_failed_case=case_id)
            save();return report
    times=[c['result']['duration_ms']/1000 for c in report['cases']]
    report.update(status='completed',passed=sum(c['passed'] for c in report['cases']),total=len(case_ids),median_seconds=round(statistics.median(times),3),max_seconds=round(max(times),3))
    save();return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--model',required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--cases',nargs='+');parser.add_argument('--screen',action='store_true')
    args=parser.parse_args();run(args.model,args.output,args.cases,args.screen)
