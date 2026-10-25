"""Independent structural/factual audit; requires an explicit review of all displayed answers."""
from pathlib import Path
import sys,json,argparse,hashlib,sqlite3,re
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from health_agent.config import PROJECT_ROOT
from health_agent.verification import validate_answer
from scripts.final_acceptance import profile,CORE

def review(folder):
    folder=Path(folder);report=json.loads((folder/'results.json').read_text());manual_path=folder/'manual_review.json'
    bilingual=set(report.get('languages',[]))=={'zh','en'}
    checks={'completed':report['status'].startswith('completed'),'source_unchanged':report.get('source_unchanged') is True and profile()==report['source_sha256']}
    if bilingual:
        checks['forty_paired_cases']=len(report['main_cases'])==40 and {(c['language'],c['id']) for c in report['main_cases']}=={(lang,f'T{i:02d}') for lang in ('zh','en') for i in range(1,21)}
        checks['twenty_extra_repetitions']=len(report['repetitions'])==20 and all(sum(c['id']==cid and c['language']==lang for c in report['repetitions'])==2 for cid in CORE for lang in ('zh','en'))
        checks['english_report_pair']=len(report.get('additional_cases',[]))==2 and {c['language'] for c in report['additional_cases']}=={'zh','en'} and all(c['passed'] for c in report['additional_cases'])
    else:
        checks['twenty_unique_cases']={c['id'] for c in report['main_cases']}=={f'T{i:02d}' for i in range(1,21)} and len(report['main_cases'])==20
        checks['ten_extra_repetitions']=len(report['repetitions'])==10 and all(sum(c['id']==cid for c in report['repetitions'])==2 for cid in CORE)
    allcases=report['main_cases']+report['repetitions']+report.get('additional_cases',[]);facts=[];assets=[];blockers=[]
    for case in allcases:
        for index,turn in enumerate(case['turns']):
            if turn['status']=='ok':
                result=validate_answer(turn['answer'],turn.get('citations',[]),turn.get('tool_results',[]))
                if not result['passed']:blockers.append({'id':case['execution_id'],'turn':index+1,'issues':result['issues']})
                if re.search(r'!\[[^\]]*\]\(https?://',turn['answer']):blockers.append({'id':case['execution_id'],'issue':'external_image_in_answer'})
                facts.append({'id':case['execution_id'],'turn':index+1,'passed':result['passed'],'answer_origin':turn.get('answer_origin','model')})
            for tool in turn.get('tool_results',[]):
                chart=tool.get('data',{}).get('chart')
                if not chart:continue
                if bilingual and chart.get('language')!=case['language']:blockers.append({'id':case['execution_id'],'issue':'wrong_chart_language'})
                source=next((r for r in turn['tool_results'] if r['result_id']==chart['source_result_id']),None)
                same=source is not None and {p['record_id']:(p['value'],p['local_time']) for panel in chart['panels'] for p in panel['points']}=={r['record_id']:(r['value'],r['local_time']) for r in source['data']['records']}
                if not same:blockers.append({'id':case['execution_id'],'issue':'chart_source_mismatch'})
                raw=json.loads(Path(chart['data_path']).read_text())
                if raw['panels']!=chart['panels']:blockers.append({'id':case['execution_id'],'issue':'chart_export_mismatch'})
                for key in ('image_path','svg_path','data_path'):
                    p=Path(chart[key]);assets.append({'path':str(p.relative_to(PROJECT_ROOT)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    with sqlite3.connect(folder/'synthetic.sqlite3') as con:
        checks['synthetic_patient']=con.execute('SELECT count(*) FROM patients WHERE is_synthetic!=1').fetchone()[0]==0
        checks['foreign_keys_clean']=not con.execute('PRAGMA foreign_key_check').fetchall()
        reports={rid:digest for rid,digest in con.execute('SELECT report_id,content_hash FROM reports')}
    seed=json.loads((PROJECT_ROOT/'sample_data/seed.json').read_text());checks['frozen_report_hashes']=all(reports.get(row['report_id'])==hashlib.sha256((PROJECT_ROOT/row['file_path']).read_bytes()).hexdigest() for row in seed['reports'])
    if bilingual:
        fixture_hash=hashlib.sha256((PROJECT_ROOT/'tests/fixtures/english_report.txt').read_bytes()).hexdigest()
        checks['english_upload_fixture_hash']=reports.get(report.get('english_upload',{}).get('report_id'))==fixture_hash==report.get('english_upload',{}).get('fixture_sha256')
    manual=json.loads(manual_path.read_text()) if manual_path.exists() else {}
    checks['all_answers_reviewed']=set(manual.get('reviewed_execution_ids',[]))=={c['execution_id'] for c in allcases}
    blockers+=manual.get('blockers',[])
    output={'accepted':report.get('machine_gate_passed',False) and all(checks.values()) and not blockers,'checks':checks,'blockers':blockers,'numeric_reviews':facts,'chart_assets':assets,'per_language':report.get('summary'),'main_passed':report.get('main_passed'),'critical_passed':report.get('critical_passed'),'manual_review':manual,'scope':'Frozen course scenarios only; not clinical validation or a proof of all natural-language semantics.'}
    (folder/'independent_review.json').write_text(json.dumps(output,ensure_ascii=False,indent=2));print(json.dumps({'accepted':output['accepted'],'checks':checks,'blockers':blockers},ensure_ascii=False,indent=2));return output
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder',type=Path);a=p.parse_args();review(a.folder)
