"""Non-destructive demo preparation. No reset, deletion, cloud fallback or secret output."""
from pathlib import Path
import sys,json,argparse,subprocess
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from health_agent.config import PROJECT_ROOT,DEFAULT_DATABASE
from health_agent.model_config import ModelSettings
from health_agent.seed import initialize_database
from health_agent.validation import validate_database
from health_agent.rag import MODEL,build_index,read_report
from health_agent.db import connect

def prepare(*,start=False,initialize=False,reindex=False):
    if start:subprocess.run([sys.executable,str(PROJECT_ROOT/'scripts/start_local.py')],cwd=PROJECT_ROOT,check=True)
    if initialize and not DEFAULT_DATABASE.exists():initialize_database(DEFAULT_DATABASE)
    report={'ok':True,'checks':{},'reference_date':'2026-10-23','synthetic_only':True}
    if not DEFAULT_DATABASE.exists():return {'ok':False,'error':'数据库不存在；使用 --initialize 创建虚构数据。'}
    validation=validate_database(DEFAULT_DATABASE);report['checks']['database']=validation['ok']
    settings=ModelSettings.load();report['model']=settings.model;report['provider']=settings.provider
    if settings.provider!='ollama':return {'ok':False,'error':'当前交付验收仅支持本机Ollama配置；不会自动切换云端。'}
    with httpx.Client(timeout=10,trust_env=False,follow_redirects=False) as c:
        tags=c.get(settings.base_url+'/api/tags');tags.raise_for_status()
        names={x['name'] for x in tags.json()['models']};report['checks']['llm_downloaded']=settings.model in names;report['checks']['embedding_downloaded']=MODEL in names
        report['checks']['ui']=c.get('http://127.0.0.1:8501/_stcore/health').status_code==200
    with connect(DEFAULT_DATABASE,readonly=True) as db:
        rows=[dict(x) for x in db.execute('SELECT * FROM reports')]
    for row in rows:read_report(row)
    report['checks']['source_hashes']=True
    if reindex and report['checks']['embedding_downloaded']:report['index']=build_index(DEFAULT_DATABASE)
    with connect(DEFAULT_DATABASE,readonly=True) as db:report['checks']['report_statuses_ready']=not db.execute("SELECT 1 FROM reports WHERE index_status!='ready'").fetchone()
    report['ok']=all(report['checks'].values()) and report.get('index',{}).get('ok',True)
    return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--start',action='store_true');p.add_argument('--initialize',action='store_true');p.add_argument('--reindex',action='store_true');args=p.parse_args()
    try:report=prepare(**vars(args))
    except Exception as exc:report={'ok':False,'error':'演示准备失败：请检查本机服务、目录权限和模型下载状态。','error_type':type(exc).__name__}
    out=PROJECT_ROOT/'artifacts/M4_validation';out.mkdir(parents=True,exist_ok=True);(out/'preflight.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['ok'] else 1
if __name__=='__main__':raise SystemExit(main())
