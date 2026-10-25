import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from health_agent.config import PROJECT_ROOT,load_config
from health_agent.conversation import ConversationAgent,ASK_SPEC
from health_agent.results import ToolContext
from health_agent.seed import initialize_database
from health_agent.tools import HealthTools
from tests.scripted_provider import ScriptedProvider,call,final
from tests.test_day4 import ask

class Day4AppTests(unittest.TestCase):
 def test_ui_choice_resumes_using_new_agent_instance_and_rerun_is_idempotent(self):
  with tempfile.TemporaryDirectory() as folder:
   db=Path(folder)/'health.sqlite3';initialize_database(db);checkpoints=Path(folder)/'sessions.sqlite3'
   provider=ScriptedProvider([ask(),call('query_health_data',{'indicator':'blood_pressure','mode':'latest'},'bp'),final('126/82 mmHg。',['SBP024','DBP024'])])
   created=[]
   def factory(**kwargs):
    created.append(kwargs['session_id'])
    return ConversationAgent(HealthTools(db,context=ToolContext('P001',kwargs['session_id'],load_config().reference_date)),provider,checkpoint_path=checkpoints,event_sink=kwargs.get('event_sink'))
   with patch('health_agent.app_support.configuration_status',return_value={'configured':True,'model':'offline-test-only'}),patch('health_agent.app_support.create_session_agent',side_effect=factory),patch('health_agent.config.DEFAULT_DATABASE',db):
    app=AppTest.from_file(str(PROJECT_ROOT/'streamlit_app.py'),default_timeout=15).run()
    app.chat_input[0].set_value('帮我看看血压。').run()
    self.assertEqual(len(app.exception),0);self.assertEqual(len(app.selectbox),1)
    app.selectbox[0].set_value('latest').run()
    next(b for b in app.button if b.label=='提交选择').click().run()
    self.assertEqual(len(app.exception),0);self.assertEqual(len(created),2);self.assertEqual(created[0],created[1])
    self.assertEqual(len(app.selectbox),0);self.assertTrue(any('126/82' in m.value for m in app.markdown))
    app.run();self.assertEqual(len(provider.requests),2)
    old=app.session_state['session_id'];app.sidebar.button[0].click().run()
    self.assertNotEqual(old,app.session_state['session_id']);self.assertEqual(len(app.chat_message),0);self.assertIsNone(app.session_state['pending_payload'])
 def test_local_ask_schema_keeps_required_fields_without_inventing_mode(self):
  import httpx,json
  from health_agent.ollama_provider import OllamaProvider
  from health_agent.model_config import ModelSettings
  captured=[]
  def handle(request):
   captured.append(json.loads(request.content));return httpx.Response(200,json={'done':True,'message':{'content':'{"answer":"你好","citations":[],"needs_clarification":false}'}})
  settings=ModelSettings(model='test-only',api_key='',provider='ollama')
  with httpx.Client(base_url='http://127.0.0.1',transport=httpx.MockTransport(handle)) as client:
   OllamaProvider(settings,client=client).complete(instructions='',messages=[{'role':'user','content':'hello'}],tools=[ASK_SPEC])
  params=captured[0]['tools'][0]['function']['parameters']
  self.assertNotIn('mode',params['required']);self.assertEqual(set(params['required']),set(params['properties']))

if __name__=='__main__':unittest.main()
