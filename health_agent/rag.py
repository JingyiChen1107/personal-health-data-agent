"""Local, hash-verified TXT retrieval. Retrieved text is untrusted data."""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import os
import re
import fcntl
from datetime import date, datetime, timezone
from uuid import uuid4
import httpx
from health_agent.config import PROJECT_ROOT, load_config
from health_agent.contracts import arguments, optional_text, validate_dates, InputError
from health_agent.db import connect
from health_agent.results import ToolResult

MODEL = 'embeddinggemma:300m'
INDEX = PROJECT_ROOT/'data/report_vectors.sqlite3'

def collection(index_path, model):
    import chromadb
    from chromadb.config import Settings
    directory=Path(str(index_path)+'.chroma');directory.mkdir(parents=True,exist_ok=True);os.chmod(directory,0o700)
    client=chromadb.PersistentClient(path=str(directory),settings=Settings(anonymized_telemetry=False))
    name='reports_'+hashlib.sha256(model.encode()).hexdigest()[:16]
    return client.get_or_create_collection(name,embedding_function=None,metadata={'hnsw:space':'cosine'})

@dataclass(frozen=True)
class DocumentQuery:
    query: str
    mode: str = 'semantic'
    report_id: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    top_k: int = 4

    @classmethod
    def parse(cls, raw):
        values = arguments(raw, cls)
        obj = cls(**values)
        if not isinstance(obj.query,str) or not 1<=len(obj.query.strip())<=500:
            raise InputError('invalid_query','检索问题须为1至500字。')
        if obj.mode not in ('semantic','mention_check'):
            raise InputError('invalid_mode','mode须为semantic或mention_check。')
        optional_text(obj.report_id,'report_id'); validate_dates(obj.start_date,obj.end_date)
        if type(obj.top_k) is not int or not 1<=obj.top_k<=5:
            raise InputError('invalid_top_k','top_k须为1至5。')
        return obj

class LocalEmbedding:
    def __init__(self, base_url='http://127.0.0.1:11435', model=MODEL):
        # Deliberately fixed to loopback: report text cannot be redirected to a cloud host.
        if base_url != 'http://127.0.0.1:11435':
            raise ValueError('Embedding endpoint must be the project loopback endpoint')
        self.base_url,self.model=base_url,model

    def embed(self, texts, *, query=False):
        prefix='task: search result | query: ' if query else 'title: health report | text: '
        with httpx.Client(timeout=120,trust_env=False,follow_redirects=False) as client:
            response=client.post(self.base_url+'/api/embed',json={'model':self.model,'input':[prefix+t for t in texts],'truncate':False,'keep_alive':'5m'})
            response.raise_for_status(); vectors=response.json()['embeddings']
        if len(vectors)!=len(texts) or not vectors or any(not v or any(not math.isfinite(x) for x in v) for v in vectors):
            raise ValueError('Invalid embeddings')
        return vectors

def read_report(row):
    root=(PROJECT_ROOT/'sample_data/reports').resolve()
    path=(PROJECT_ROOT/row['file_path']).resolve()
    if not path.is_relative_to(root) or path.suffix.lower()!='.txt':
        raise ValueError('Report path outside registered TXT directory')
    raw=path.read_bytes()
    if len(raw)>1048576 or hashlib.sha256(raw).hexdigest()!=row['content_hash']:
        raise ValueError('Report content hash mismatch or size limit exceeded')
    return raw.decode('utf-8')

def chunks(text, size=600, overlap=100):
    if size<=overlap or overlap<0: raise ValueError('Invalid chunk configuration')
    start=0;number=0
    while start<len(text):
        end=min(start+size,len(text))
        if end<len(text):
            boundary=text.rfind('\n\n',start+max(overlap+1,size//2),end)
            if boundary!=-1:end=boundary+2
        number+=1
        yield {'chunk':number,'start_char':start,'end_char':end,'start_line':text.count('\n',0,start)+1,
               'end_line':text.count('\n',0,end-1)+1,'text':text[start:end]}
        if end==len(text):break
        start=end-overlap

def build_index(database, *, index_path=INDEX, embedder=None):
    cfg=load_config(); embedder=embedder or LocalEmbedding()
    path=Path(index_path);path.parent.mkdir(parents=True,exist_ok=True)
    with connect(database) as db, sqlite3.connect(path) as idx:
        os.chmod(path,0o600)
        idx.execute('CREATE TABLE IF NOT EXISTS reports_index (patient_id TEXT, report_id TEXT, content_hash TEXT, model TEXT, chunks TEXT, PRIMARY KEY(patient_id,report_id))')
        rows=[dict(r) for r in db.execute('SELECT * FROM reports ORDER BY report_id')]
        if len(rows)>5: raise ValueError('Prototype supports at most five reports')
        output=[]
        try:
            vectors_store=collection(index_path,embedder.model)
        except Exception:
            db.execute("UPDATE reports SET index_status='failed'");db.commit()
            return {'ok':False,'model':embedder.model,'error':{'code':'index_store_unavailable','message':'本地向量索引不可用，请检查目录权限或重建。'},'reports':[{'report_id':row['report_id'],'status':'failed'} for row in rows]}
        for row in rows:
            try:
                text=read_report(row); parts=list(chunks(text,cfg.rag_chunk_chars,cfg.rag_overlap_chars))
                vectors=embedder.embed([p['text'] for p in parts])
                if len(vectors)!=len(parts):raise ValueError('Incomplete embedding batch')
                identity=row['patient_id']+':'+row['report_id']+':'+row['content_hash']
                ids=[identity+':'+str(p['chunk']) for p in parts]
                vectors_store.upsert(ids=ids,embeddings=vectors,documents=[p['text'] for p in parts],
                    metadatas=[{'patient_id':row['patient_id'],'report_id':row['report_id'],'content_hash':row['content_hash'],'chunk':p['chunk']} for p in parts])
                stored=vectors_store.get(ids=ids)
                if len(stored['ids'])!=len(parts):raise ValueError('Incomplete Chroma write')
                for p,identifier in zip(parts,ids):p['vector_id']=identifier
                idx.execute('INSERT OR REPLACE INTO reports_index VALUES (?,?,?,?,?)',
                    (row['patient_id'],row['report_id'],row['content_hash'],embedder.model,json.dumps(parts,ensure_ascii=False,allow_nan=False)))
                idx.commit()
                db.execute("UPDATE reports SET index_status='ready' WHERE report_id=?",(row['report_id'],));db.commit()
                output.append({'report_id':row['report_id'],'status':'ready','chunks':len(parts)})
            except Exception:
                db.execute("UPDATE reports SET index_status='failed' WHERE report_id=?",(row['report_id'],));db.commit()
                output.append({'report_id':row['report_id'],'status':'failed'})
        return {'ok':all(r['status']=='ready' for r in output),'model':embedder.model,'reports':output}

def aliases_for(query,cfg):
    folded=query.casefold()
    for key,aliases in cfg.mention_aliases.items():
        if key.casefold() in folded or any(a.casefold() in folded for a in aliases):return aliases
    return [query.strip()]

def cosine(a,b):
    if len(a)!=len(b):raise ValueError('Embedding dimension mismatch')
    norm=math.sqrt(sum(x*x for x in a)*sum(x*x for x in b))
    if not norm:raise ValueError('Zero embedding')
    return sum(x*y for x,y in zip(a,b))/norm

def search(tools, raw, *, index_path=INDEX, embedder=None):
    try:
        q=DocumentQuery.parse(raw);embedder=embedder or LocalEmbedding();cfg=tools.config
        end=min(q.end_date or '9998-12-31',(tools.context.reference_date+timedelta(days=1)).isoformat())
        with connect(tools.database,readonly=True) as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM reports WHERE patient_id=? AND report_date>=? AND report_date<? AND (? IS NULL OR report_id=?) ORDER BY report_date, report_id',
                (tools.context.patient_id,q.start_date or '0001-01-01',end,q.report_id,q.report_id))]
        data={'mode':q.mode,'scope_report_ids':[r['report_id'] for r in rows],
              'scope_report_count':len(rows),'date_field':'report_date','start_date':q.start_date,'end_date_exclusive':end,
              'untrusted_document_content':True,'matches':[]}
        evidence=[]
        if not rows:return ToolResult('no_data',data=data).to_dict()
        texts={r['report_id']:read_report(r) for r in rows}
        if q.mode=='mention_check':
            aliases=aliases_for(q.query,cfg);data.update(checked_aliases=aliases,coverage='full_text',absence_scope='仅限上述报告全文和列出的别名')
            for row in rows:
                text=texts[row['report_id']]
                for number,line in enumerate(text.splitlines(),1):
                    if any(a.casefold() in line.casefold() for a in aliases):
                        item={'source_id':f"{row['report_id']}:L{number}",'report_id':row['report_id'],'text':line,'start_line':number,'end_line':number}
                        data['matches'].append(item)
            # Coverage itself is evidence, even when there is no mention.
            for row in rows:
                rid=row['report_id'];evidence.append({'source_type':'report_scan','source_id':rid+':SCAN',
                    'locator':{'report_id':rid,'file_path':row['file_path'],'content_hash':row['content_hash'],'start_line':1,'end_line':len(texts[rid].splitlines())},
                    'record':{'report_date':row['report_date'],'checked_aliases':aliases,'coverage':'full_text','match_count':sum(x['report_id']==rid for x in data['matches'])}})
        else:
            if not Path(index_path).is_file():raise InputError('index_not_ready','报告索引尚未建立。')
            vector=embedder.embed([q.query],query=True)[0];candidates=[]
            vectors_store=collection(index_path,embedder.model)
            with sqlite3.connect(f'file:{Path(index_path).resolve()}?mode=ro',uri=True) as idx:
                for row in rows:
                    cached=idx.execute('SELECT content_hash,model,chunks FROM reports_index WHERE patient_id=? AND report_id=?',(tools.context.patient_id,row['report_id'])).fetchone()
                    if row['index_status']!='ready' or not cached or cached[0]!=row['content_hash'] or cached[1]!=embedder.model:
                        raise InputError('index_not_ready','报告索引缺失或版本不匹配，请重建。')
                    parts=json.loads(cached[2])
                    ranked=vectors_store.query(query_embeddings=[vector],n_results=len(parts),
                        where={'$and':[{'patient_id':tools.context.patient_id},{'report_id':row['report_id']},{'content_hash':row['content_hash']}]},include=['distances'])
                    scores=dict(zip(ranked['ids'][0],[1-x for x in ranked['distances'][0]]))
                    if set(scores)!={part['vector_id'] for part in parts}:raise ValueError('Incomplete Chroma index')
                    for part in parts:
                        if texts[row['report_id']][part['start_char']:part['end_char']]!=part['text']:raise ValueError('Chunk text mismatch')
                        item={k:v for k,v in part.items() if k!='vector_id'}
                        item.update(report_id=row['report_id'],report_date=row['report_date'],source_id=f"{row['report_id']}:C{part['chunk']:03d}",score=round(scores[part['vector_id']],6))
                        candidates.append(item)
            data.update(coverage='top_k_only',absence_scope='检索片段不能证明整份报告未提及',embedding_model=embedder.model)
            data['matches']=sorted(candidates,key=lambda x:(-x['score'],x['source_id']))[:q.top_k]
        for item in data['matches']:
            row=next(r for r in rows if r['report_id']==item['report_id'])
            evidence.append({'source_type':'report_text','source_id':item['source_id'],
                'locator':{'file_path':row['file_path'],'content_hash':row['content_hash'],'start_line':item['start_line'],'end_line':item['end_line']},
                'record':item})
        return ToolResult('ok',data=data,evidence=evidence).to_dict()
    except InputError as exc:
        return ToolResult(exc.status,error={'code':exc.code,'message':str(exc)}).to_dict()
    except Exception:
        return ToolResult('error',error={'code':'retrieval_failed','message':'原文完整性、索引或本地embedding服务检查失败，未返回检索结论。'}).to_dict()


def upload_report(database, *, patient_id, file_name, content, report_date, synthetic_confirmed=False, index_path=INDEX, embedder=None):
    """Only UI/application can upload; the model has no write tool."""
    if synthetic_confirmed is not True:raise ValueError('请确认仅上传虚构演示数据。')
    if not isinstance(file_name,str) or Path(file_name).suffix.lower()!='.txt':raise ValueError('仅支持TXT。')
    if not isinstance(content,bytes) or not 0<len(content)<=1048576:raise ValueError('文件须为1字节至1MB。')
    text=content.decode('utf-8')
    if not text.strip() or '\x00' in text:raise ValueError('TXT内容无效。')
    day=date.fromisoformat(report_date)
    if day.isoformat()!=report_date or day>load_config().reference_date:raise ValueError('报告日期须不晚于参考日期。')
    digest=hashlib.sha256(content).hexdigest()
    lock_path=Path(database).parent/'report_upload.lock'
    with lock_path.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with connect(database) as db:
            if not db.execute('SELECT 1 FROM patients WHERE patient_id=?',(patient_id,)).fetchone():raise ValueError('用户不存在。')
            old=db.execute('SELECT report_id,index_status FROM reports WHERE patient_id=? AND content_hash=?',(patient_id,digest)).fetchone()
            if old:return {'report_id':old['report_id'],'status':old['index_status'],'duplicate':True}
            if db.execute('SELECT count(*) FROM reports WHERE patient_id=?',(patient_id,)).fetchone()[0]>=5:raise ValueError('首版最多保存5份报告。')
            rid='R'+uuid4().hex[:12].upper()
            # Never use user-controlled names as paths. Record only an app-generated TXT name.
            relative='sample_data/reports/'+rid+'.txt';target=PROJECT_ROOT/relative
            with target.open('xb') as file:os.chmod(target,0o600);file.write(content)
            try:
                db.execute('INSERT INTO reports VALUES (?,?,?,?,?,?,?,?)',
                    (rid,patient_id,rid+'.txt',relative,report_date,datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),digest,'pending'))
            except Exception:
                target.unlink(missing_ok=True);raise
            db.commit()
        result=build_index(database,index_path=index_path,embedder=embedder)
        status=next(r['status'] for r in result['reports'] if r['report_id']==rid)
        return {'report_id':rid,'status':status,'duplicate':False}
