"""Durable session-owned query snapshots; wall-clock expiry survives worker recreation."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from time import time

class SQLiteResultCache:
    def __init__(self,path,*,namespace,ttl_seconds=1800,clock=time):
        if isinstance(ttl_seconds,bool) or ttl_seconds<=0:raise ValueError('Invalid TTL')
        self.path=Path(path);self.namespace=hashlib.sha256(str(namespace).encode()).hexdigest()
        self.ttl_seconds=ttl_seconds;self.clock=clock
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR,0o600);os.fchmod(fd,0o600);os.close(fd)
        with sqlite3.connect(self.path,timeout=30) as con:
            con.execute('CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, namespace TEXT, patient TEXT, session TEXT, reference TEXT, tool TEXT, created REAL, expires REAL, payload TEXT)')
    def put(self,result,*,context,tool):
        now=self.clock()
        with sqlite3.connect(self.path,timeout=30) as con:
            con.execute('DELETE FROM snapshots WHERE expires<=?',(now,))
            con.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)',(result.result_id,self.namespace,context.patient_id,context.session_id,context.reference_date.isoformat(),tool,now,now+self.ttl_seconds,json.dumps(result.to_dict(),ensure_ascii=False,allow_nan=False)))
    def get(self,result_id,*,context,tool=None):
        if not isinstance(result_id,str):raise KeyError('invalid_result_id')
        with sqlite3.connect(self.path,timeout=30) as con:
            row=con.execute('SELECT tool,payload FROM snapshots WHERE id=? AND namespace=? AND patient=? AND session=? AND reference=? AND expires>?',(result_id,self.namespace,context.patient_id,context.session_id,context.reference_date.isoformat(),self.clock())).fetchone()
        if not row or (tool is not None and row[0]!=tool):raise KeyError('invalid_result_id')
        return json.loads(row[1])
    def clear(self,*,context):
        with sqlite3.connect(self.path,timeout=30) as con:con.execute('DELETE FROM snapshots WHERE namespace=? AND patient=? AND session=? AND reference=?',(self.namespace,context.patient_id,context.session_id,context.reference_date.isoformat()))
