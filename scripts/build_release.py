"""Whitelist-only, gated source delivery. No env, runtime, weights, live DBs or chat history."""
from pathlib import Path
import sys,json,hashlib,zipfile,argparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from health_agent.config import PROJECT_ROOT

BLOCKED={'.git','.venv','.runtime','__pycache__','data'}
SCRIPTS=['start_local.py','prepare_demo.py','final_acceptance.py','bilingual_acceptance.py','build_release.py','review_acceptance.py','build_final_reports.py']

def write_entry(archive,path,content):
    info=zipfile.ZipInfo(path,date_time=(2026,10,23,0,0,0));info.create_system=3
    info.external_attr=((0o100755 if path.endswith('.command') else 0o100644)<<16)
    info.compress_type=zipfile.ZIP_DEFLATED;archive.writestr(info,content)

def source_files(root):
    root=Path(root);files=[]
    for name in ['README.md','pyproject.toml','requirements.in','requirements.txt','requirements.lock','.env.example','streamlit_app.py','启动本地Agent.command','演示前检查.command']:
        p=root/name
        if p.is_file():files.append(p)
    for folder,pattern in [('health_agent','*.py'),('health_agent','schema.sql'),('config','*.json'),('tests','*.py'),('tests/fixtures','*.json'),('tests/fixtures','*.txt'),('docs','*.md'),('.streamlit','*.toml')]:
        files.extend((root/folder).glob(pattern))
    for name in SCRIPTS:
        if (root/'scripts'/name).is_file():files.append(root/'scripts'/name)
    seed=root/'sample_data/seed.json'
    if seed.is_file():
        files.append(seed)
        for report in json.loads(seed.read_text())['reports']:files.append(root/report['file_path'])
    allowed=[]
    for p in files:
        rel=p.relative_to(root)
        if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(root.resolve()) and not (set(rel.parts)&BLOCKED) and ('.env' not in rel.parts):allowed.append(p)
    return sorted(set(allowed))

def build(acceptance,output):
    root=PROJECT_ROOT;acceptance=Path(acceptance).resolve();output=Path(output).resolve()
    results=json.loads((acceptance/'results.json').read_text());review=json.loads((acceptance/'independent_review.json').read_text())
    assert results['machine_gate_passed'] and review['accepted'],'Acceptance must pass, including independent review'
    from scripts.final_acceptance import profile
    assert profile()==results['source_sha256'],'Runtime source changed since accepted run'
    output.parent.mkdir(parents=True,exist_ok=True);assert not output.exists(),'Do not overwrite an existing delivery'
    import tomllib
    version=tomllib.loads((root/'pyproject.toml').read_text())['project']['version']
    manifest={'version':version,'document_layout':'M1-M4 milestones with one operations handbook','acceptance':{'main_passed':results['main_passed'],'main_total':results.get('main_total',20),'critical_passed':results['critical_passed'],'critical_total':results.get('critical_total',15),'blockers':review['blockers'],'per_language':results.get('summary'),'english_report_pair':sum(c['passed'] for c in results.get('additional_cases',[]))},'excluded':['.env','.runtime/','.venv/','model weights','live databases','conversation history','unregistered uploads'],'files':[],'evidence_note':'Machine-local project paths in exported JSON replaced with ${PROJECT_ROOT}; numeric facts and model answers unchanged.'}
    assets={}
    for p in source_files(root):assets[p.relative_to(root).as_posix()]=p.read_bytes()
    assets['README.md']=(root/'README.md').read_bytes()
    for p in (root/'deliverables').glob('*.md'):assets['deliverables/'+p.name]=p.read_bytes()
    if (root/'deliverables/个人健康数据分析Agent_SOW_v2.docx').is_file():assets['deliverables/个人健康数据分析Agent_SOW_v2.docx']=(root/'deliverables/个人健康数据分析Agent_SOW_v2.docx').read_bytes()
    for p in acceptance.glob('*.json'):
        # Never include the acceptance SQLite DB or session cache; only synthetic scenario evidence.
        text=p.read_text().replace(str(root),'${PROJECT_ROOT}')
        assets['evidence/final_acceptance/'+p.name]=text.encode()
    for p in (root/'artifacts/M4_validation').glob('*.json'):
        if p.name not in ('release_verification.json','final_delivery.json','package_smoke.json'):assets['evidence/'+p.name]=p.read_bytes()
    for name in ['automated_tests.log','ui_final.png','ui_records.png','ui_business.png','ui_english_answer.png','ui_english_chart.png']:
        p=root/'artifacts/M4_validation'/name
        if p.is_file():assets['evidence/'+name]=p.read_bytes()
    for case in results['main_cases']+results['repetitions']+results.get('additional_cases',[]):
        for turn in case['turns']:
            for result in turn.get('tool_results',[]):
                chart=result.get('data',{}).get('chart')
                if chart:
                    for key in ('image_path','svg_path','data_path'):
                        p=Path(chart[key]).resolve()
                        assert p.is_relative_to((root/'artifacts/charts').resolve())
                        assets[p.relative_to(root).as_posix()]=p.read_bytes()
    # Include only the explicit synthetic English acceptance upload, never arbitrary uploads.
    if results.get('english_upload'):
        fixture=root/'tests/fixtures/english_report.txt';digest=hashlib.sha256(fixture.read_bytes()).hexdigest()
        assert digest==results['english_upload']['fixture_sha256']
        rid=results['english_upload']['report_id'];path=root/'sample_data/reports'/(rid+'.txt')
        assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==digest
        assets[path.relative_to(root).as_posix()]=path.read_bytes()
    for name in ['ui_english_answer.png','ui_english_chart.png']:
        p=root/'artifacts/M4_validation'/name
        if p.is_file():assets[p.relative_to(root).as_posix()]=p.read_bytes()
    for path,content in sorted(assets.items()):manifest['files'].append({'path':path,'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()})
    with zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as z:
        for path,content in sorted(assets.items()):
            assert not set(Path(path).parts)&BLOCKED and '.env' not in Path(path).parts
            write_entry(z,path,content)
        write_entry(z,'MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    output.chmod(0o600)
    with zipfile.ZipFile(output) as z:
        assert z.testzip() is None
        for item in manifest['files']:assert hashlib.sha256(z.read(item['path'])).hexdigest()==item['sha256']
    final={'path':str(output),'bytes':output.stat().st_size,'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'file_count':len(assets),'verified':True}
    (root/'artifacts/M4_validation/release_verification.json').write_text(json.dumps(final,ensure_ascii=False,indent=2));print(json.dumps(final,ensure_ascii=False,indent=2));return final
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--acceptance',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();build(a.acceptance,a.output)
