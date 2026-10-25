from pathlib import Path
from tempfile import TemporaryDirectory
import json, sqlite3, unittest
from unittest.mock import patch
from health_agent.config import DEFAULT_DATABASE,load_config
from health_agent.results import ToolContext
from health_agent.rag import DocumentQuery, LocalEmbedding, chunks, build_index, search, aliases_for
from health_agent.tools import HealthTools
from health_agent.seed import initialize_database
from tests.scripted_provider import ScriptedProvider,call,final
from health_agent.conversation import ConversationAgent

class FakeEmbedding:
    model='test-only-fake'
    def embed(self,texts,**kw):return [[1.0,0.5,0.2] for t in texts]

class Day5Tests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name);self.db=self.root/'health.sqlite3';self.idx=self.root/'index.sqlite3'
        initialize_database(self.db)
        cfg=load_config();self.tools=HealthTools(self.db,context=ToolContext(cfg.patient_id,'test-day5',cfg.reference_date),config=cfg)
        self.embed=FakeEmbedding();self.assertTrue(build_index(self.db,index_path=self.idx,embedder=self.embed)['ok'])
    def tearDown(self):self.temp.cleanup()
    def search(self,args):return search(self.tools,args,index_path=self.idx,embedder=self.embed)
    def test_date_filters_report_date_and_alias_scan(self):
        r=self.search({'query':'LDL','mode':'mention_check','start_date':'2025-01-01','end_date':'2026-01-01'})
        self.assertEqual(r['data']['scope_report_ids'],['R001','R002']);self.assertEqual(r['data']['coverage'],'full_text')
        self.assertIn('3.2 mmol/L',r['data']['matches'][0]['text']);self.assertGreaterEqual(len(r['data']['checked_aliases']),5)
    def test_negative_scan_has_coverage_evidence(self):
        r=self.search({'query':'LDL','mode':'mention_check','report_id':'R002'})
        self.assertEqual(r['data']['matches'],[]);self.assertEqual(r['evidence'][0]['source_id'],'R002:SCAN');self.assertEqual(r['evidence'][0]['record']['match_count'],0)
    def test_semantic_returns_exact_located_text(self):
        r=self.search({'query':'血糖记录整理','report_id':'R003'})
        self.assertEqual(r['status'],'ok');self.assertEqual(r['data']['coverage'],'top_k_only')
        e=r['evidence'][0];self.assertEqual(e['source_id'],'R003:C001');self.assertIn('空腹、餐后、随机及条件未知',e['record']['text']);self.assertEqual(e['locator']['start_line'],1)
    def test_semantic_never_claims_whole_absence(self):
        r=self.search({'query':'甲状腺复查','report_id':'R002'});self.assertIn('不能证明',r['data']['absence_scope'])
    def test_other_patient_has_no_report(self):
        self.tools.context=ToolContext('P999','other',self.tools.context.reference_date)
        self.assertEqual(self.search({'query':'LDL'})['status'],'no_data')
    def test_bad_arguments_rejected(self):
        for args in [{'query':'x','patient_id':'P002'},{'query':'x','top_k':True},{'query':'x','top_k':6},{'query':'x','start_date':'2026-13-01'},{'query':'x','start_date':'2026-10-10','end_date':'2026-10-01'},{'query':'x','mode':'sql'},{'query':''}]:
            with self.subTest(args=args):self.assertEqual(self.search(args)['status'],'error')
    def test_unknown_report_no_data(self):self.assertEqual(self.search({'query':'x','report_id':'R999'})['status'],'no_data')
    def test_reference_date_cutoff(self):self.assertEqual(self.search({'query':'x','start_date':'2026-10-24'})['status'],'no_data')
    def test_content_hash_change_fails_closed(self):
        with sqlite3.connect(self.db) as con:con.execute("UPDATE reports SET content_hash=? WHERE report_id='R003'",('0'*64,))
        self.assertEqual(self.search({'query':'x','report_id':'R003'})['status'],'error')
    def test_stale_index_fails_closed(self):
        with sqlite3.connect(self.idx) as con:con.execute("UPDATE reports_index SET model='wrong'")
        self.assertEqual(self.search({'query':'x'})['error']['code'],'index_not_ready')
    def test_missing_index_is_explicit(self):
        self.idx.unlink();self.assertEqual(self.search({'query':'x'})['error']['code'],'index_not_ready')
    def test_path_escape_rejected(self):
        with sqlite3.connect(self.db) as con:con.execute("UPDATE reports SET file_path='/etc/passwd' WHERE report_id='R003'")
        self.assertEqual(self.search({'query':'x','report_id':'R003'})['status'],'error')
    def test_failed_embedding_does_not_mark_ready(self):
        with patch.object(self.embed,'embed',side_effect=ValueError('test')):
            r=build_index(self.db,index_path=self.idx,embedder=self.embed)
        self.assertFalse(r['ok'])
        with sqlite3.connect(self.db) as con:self.assertEqual(con.execute("SELECT count(*) FROM reports WHERE index_status='failed'").fetchone()[0],4)
    def test_index_rebuild_is_idempotent(self):
        build_index(self.db,index_path=self.idx,embedder=self.embed)
        with sqlite3.connect(self.idx) as con:self.assertEqual(con.execute('SELECT count(*) FROM reports_index').fetchone()[0],4)
    def test_chunk_overlap_and_location(self):
        text=('第一行\n第二行\n'*150);parts=list(chunks(text,600,100))
        self.assertGreater(len(parts),1)
        for p in parts:self.assertEqual(text[p['start_char']:p['end_char']],p['text'])
        self.assertEqual(parts[1]['start_char'],500)
    def test_embedding_loopback_only(self):
        for url in ['https://example.com','http://127.0.0.1:11435@evil.com','http://localhost:11435']:
            with self.assertRaises(ValueError):LocalEmbedding(url)
    def test_retrieved_instructions_only_data(self):
        r=self.search({'query':'指令测试','report_id':'R004'});self.assertTrue(r['data']['untrusted_document_content']);self.assertIn('99 mmol/L',r['data']['matches'][0]['text'])
    def test_agent_document_dispatch_and_citation(self):
        provider=ScriptedProvider([call('search_health_documents',{'query':'血糖整理','report_id':'R003'}),final('报告原文：空腹、餐后、随机及条件未知的血糖记录应分别归档。[R003:C001]',['R003:C001'])])
        agent=ConversationAgent(self.tools,provider,checkpoint_path=self.root/'checkpoint.sqlite3')
        with patch('health_agent.rag.INDEX',self.idx),patch('health_agent.rag.LocalEmbedding',return_value=self.embed):
            # Default argument binds at definition time, so bind the test-owned index explicitly.
            with patch.object(self.tools,'search_health_documents',side_effect=lambda args:self.search(args)):
                result=agent.run('R003报告怎么说？')
        agent.close();self.assertEqual(result['status'],'ok');self.assertEqual(result['trace'][0]['arguments']['report_id'],'R003')
    def test_forged_report_citation_rejected(self):
        provider=ScriptedProvider([call('search_health_documents',{'query':'LDL','mode':'mention_check','report_id':'R002'}),final('不存在',['R999:C001']),final('不存在',['R999:C001'])])
        agent=ConversationAgent(self.tools,provider,checkpoint_path=self.root/'cp.sqlite3')
        with patch.object(self.tools,'search_health_documents',side_effect=lambda args:self.search(args)):r=agent.run('R002提到LDL吗？')
        agent.close();self.assertEqual(r['status'],'error')

    def test_upload_dedup_limit_and_index_success(self):
        from health_agent.rag import upload_report
        from health_agent.config import PROJECT_ROOT
        import shutil
        shutil.copytree(PROJECT_ROOT/'sample_data/reports',self.root/'sample_data/reports')
        content='完全虚构的演示报告\n血压记录应保留测量时间。'.encode()
        with patch('health_agent.rag.PROJECT_ROOT',self.root):
            first=upload_report(self.db,patient_id='P001',file_name='../../demo.txt',content=content,report_date='2026-10-22',synthetic_confirmed=True,index_path=self.idx,embedder=self.embed)
            self.assertEqual(first['status'],'ready');self.assertFalse(first['duplicate'])
            second=upload_report(self.db,patient_id='P001',file_name='different.txt',content=content,report_date='2026-10-22',synthetic_confirmed=True,index_path=self.idx,embedder=self.embed)
            self.assertTrue(second['duplicate']);self.assertEqual(first['report_id'],second['report_id'])
            result=self.search({'query':'血压','report_id':first['report_id']});self.assertEqual(result['status'],'ok')
            with self.assertRaises(ValueError):upload_report(self.db,patient_id='P001',file_name='x.txt',content=b'new text',report_date='2026-10-22',synthetic_confirmed=True,index_path=self.idx,embedder=self.embed)
        self.assertTrue((self.root/'sample_data/reports'/(first['report_id']+'.txt')).is_file())
    def test_upload_invalid_inputs(self):
        from health_agent.rag import upload_report
        baseline={'database':self.db,'patient_id':'P001','file_name':'x.txt','content':b'test','report_date':'2026-10-22','synthetic_confirmed':True,'index_path':self.idx,'embedder':self.embed}
        for changed in [{'file_name':'x.pdf'},{'content':b'\xff'},{'content':b'x'*1048577},{'content':b''},{'report_date':'2027-01-01'},{'synthetic_confirmed':False},{'patient_id':'P999'}]:
            with self.subTest(changed=tuple(changed)):
                with self.assertRaises(ValueError):upload_report(**(baseline|changed))
    def test_upload_embedding_failure_is_visible(self):
        from health_agent.rag import upload_report
        from health_agent.config import PROJECT_ROOT
        import shutil
        shutil.copytree(PROJECT_ROOT/'sample_data/reports',self.root/'sample_data/reports')
        with patch('health_agent.rag.PROJECT_ROOT',self.root),patch.object(self.embed,'embed',side_effect=ValueError('offline')):
            result=upload_report(self.db,patient_id='P001',file_name='x.txt',content=b'synthetic upload',report_date='2026-10-22',synthetic_confirmed=True,index_path=self.idx,embedder=self.embed)
        self.assertEqual(result['status'],'failed')
    def test_wrong_last_year_arguments_are_corrected_before_search(self):
        provider=ScriptedProvider([
            call('search_health_documents',{'query':'LDL','mode':'mention_check','start_date':'2025-10-23','end_date':'2026-10-23'},'bad'),
            call('search_health_documents',{'query':'LDL','mode':'mention_check','start_date':'2025-01-01','end_date':'2026-01-01'},'good'),
            final('报告原文记录3.2 mmol/L。[R001:L8]',['R001:L8'])])
        agent=ConversationAgent(self.tools,provider,checkpoint_path=self.root/'cp.sqlite3')
        with patch.object(self.tools,'search_health_documents',side_effect=lambda args:self.search(args)) as execute:
            result=agent.run('去年体检报告提到LDL吗？')
        agent.close();self.assertEqual(execute.call_count,1);self.assertEqual(result['trace'][0]['error_code'],'report_date_mismatch');self.assertEqual(result['status'],'ok')
    def test_chroma_missing_vectors_fails_closed(self):
        from health_agent.rag import collection
        store=collection(self.idx,self.embed.model)
        stored=store.get();store.delete(ids=stored['ids'])
        self.assertEqual(self.search({'query':'x','report_id':'R003'})['status'],'error')
    def test_explicit_report_does_not_accept_invented_date_filter(self):
        provider=ScriptedProvider([
            call('search_health_documents',{'query':'血糖整理','report_id':'R003','start_date':'2025-01-01','end_date':'2026-01-01'},'bad'),
            call('search_health_documents',{'query':'血糖整理','report_id':'R003','start_date':None,'end_date':None},'good'),
            final('空腹、餐后、随机及条件未知的血糖记录应分别归档。',['R003:C001'])])
        agent=ConversationAgent(self.tools,provider,checkpoint_path=self.root/'cp.sqlite3')
        with patch.object(self.tools,'search_health_documents',side_effect=lambda args:self.search(args)) as execute:r=agent.run('R003报告对血糖整理怎么说？')
        agent.close();self.assertEqual(execute.call_count,1);self.assertEqual(r['status'],'ok')
    def test_semantic_negative_requires_coverage_qualification(self):
        provider=ScriptedProvider([call('search_health_documents',{'query':'甲状腺','report_id':'R002'}),
            final('报告R002未找到甲状腺复查安排。',['R002:C001']),
            final('已检索片段中未找到支持依据，不能据此断言整份报告未提及。',['R002:C001'])])
        agent=ConversationAgent(self.tools,provider,checkpoint_path=self.root/'cp.sqlite3')
        with patch.object(self.tools,'search_health_documents',side_effect=lambda args:self.search(args)):r=agent.run('从R002找出甲状腺复查安排。')
        agent.close();self.assertEqual(r['status'],'ok');self.assertIn('片段',r['answer']);self.assertEqual(len(provider.requests),3)
    def test_report_ui_displays_text_and_locator(self):
        from streamlit.testing.v1 import AppTest
        from health_agent.config import PROJECT_ROOT
        result=self.search({'query':'血糖整理','report_id':'R003'})
        fake_answer={'status':'ok','answer':'空腹、餐后、随机及条件未知的血糖记录应分别归档。','trace':[], 'tool_results':[result]}
        class UIOnlyAgent:
            def run(self,question):return fake_answer
        with patch('health_agent.app_support.configuration_status',return_value={'configured':True,'model':'test-double-only'}),patch('health_agent.config.DEFAULT_DATABASE',self.db),patch('health_agent.app_support.create_session_agent',return_value=UIOnlyAgent()):
            app=AppTest.from_file(str(PROJECT_ROOT/'streamlit_app.py'),default_timeout=15).run()
            app.chat_input[0].set_value('R003报告怎么说？').run()
            self.assertEqual(len(app.exception),0);self.assertTrue(any('start_line' in item.value for item in app.json));self.assertTrue(any('分别归档' in item.value for item in app.markdown))

    def test_unknown_glucose_cannot_be_called_nonfasting(self):
        provider=ScriptedProvider([call('query_health_data',{'indicator':'glucose','mode':'latest','measurement_context':None}),
            final('最近血糖6.9 mmol/L，条件未知（非空腹）。',['G032']),
            final('最近血糖6.9 mmol/L，测量条件未知。',['G032'])])
        agent=ConversationAgent(self.tools,provider,checkpoint_path=self.root/'cp.sqlite3')
        r=agent.run('分析最近一次血糖。');agent.close()
        self.assertEqual(r['status'],'ok');self.assertNotIn('非空腹',r['answer']);self.assertEqual(len(provider.requests),3)
