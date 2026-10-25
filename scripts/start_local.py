"""Start project-contained Ollama and Streamlit without a login/background service."""
from pathlib import Path
import os
import subprocess
import time
import urllib.request
import sys
import shutil

root=Path(__file__).resolve().parents[1]
runtime=root/'.runtime'
logs=runtime/'logs'
logs.mkdir(parents=True,exist_ok=True)
env=os.environ.copy()
env.update(OLLAMA_HOST='127.0.0.1:11435', OLLAMA_MODELS=str(runtime/'ollama/models'),
           OLLAMA_NO_CLOUD='1', OLLAMA_NUM_PARALLEL='1', OLLAMA_CONTEXT_LENGTH='8192',
           OLLAMA_FLASH_ATTENTION='1', OLLAMA_KV_CACHE_TYPE='q8_0',
           PYTHONDONTWRITEBYTECODE='1')

def ready(url):
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url,timeout=2) as response:
            return response.status==200
    except OSError:
        return False

if not ready('http://127.0.0.1:11435/api/version'):
    binary=runtime/'ollama/ollama'
    if not binary.is_file():
        installed=shutil.which('ollama')
        if not installed:raise SystemExit('未找到Ollama。请安装官方Ollama，或将运行程序放入.runtime/ollama/。')
        binary=Path(installed)
    with (logs/'ollama.log').open('ab') as log:
        subprocess.Popen([str(binary),'serve'],cwd=root,env=env,
                         stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    for _ in range(30):
        if ready('http://127.0.0.1:11435/api/version'): break
        time.sleep(1)
    else: raise SystemExit('Ollama未启动，请查看.runtime/logs/ollama.log。')
if not ready('http://127.0.0.1:8501/_stcore/health'):
    with (logs/'streamlit.log').open('ab') as log:
        subprocess.Popen([str(root/'.venv/bin/python'),'-B','-m','streamlit','run','streamlit_app.py',
                          '--server.address','127.0.0.1','--server.port','8501','--server.headless','true',
                          '--browser.gatherUsageStats','false'],cwd=root,env=env,
                         stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    for _ in range(20):
        if ready('http://127.0.0.1:8501/_stcore/health'): break
        time.sleep(1)
    else: raise SystemExit('界面未启动，请查看.runtime/logs/streamlit.log。')
sys.path.insert(0,str(root))
from health_agent.model_config import ModelSettings
print(f'本机模型：{ModelSettings.load().model}；打开 http://127.0.0.1:8501/ 。关闭窗口不停止服务。')
