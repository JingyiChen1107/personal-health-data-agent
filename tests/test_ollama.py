import json
import tempfile
import unittest
from pathlib import Path
import httpx
from health_agent.model_config import ModelSettings, ModelConfigError
from health_agent.ollama_provider import OllamaProvider, native_messages
from health_agent.provider import ProviderError
from health_agent.tool_specs import tool_specs

class LocalModelTests(unittest.TestCase):
    def settings(self, **extra):
        return ModelSettings.load(env={'LLM_PROVIDER':'ollama','LLM_MODEL':'qwen3:1.7b','OPENAI_API_KEY':'DO_NOT_FORWARD',**extra}, dotenv_path=Path('/private/tmp/nonexistent-health-dotenv'))
    def test_local_requires_no_key_and_drops_cloud_key(self):
        settings=self.settings()
        self.assertEqual(settings.api_key,'')
        self.assertEqual(settings.provider,'ollama')
    def test_rejects_remote_endpoint_and_cloud_model(self):
        for key,value in [('OLLAMA_BASE_URL','https://example.com'),('OLLAMA_BASE_URL','http://localhost@evil.com'),('OLLAMA_BASE_URL','http://127.0.0.1:11435/api'),('LLM_MODEL','qwen3-cloud'),('OLLAMA_TIMEOUT_SECONDS','0')]:
            with self.subTest(value=value),self.assertRaises(ModelConfigError): self.settings(**{key:value})
    def test_tool_protocol_roundtrip(self):
        captured=[]
        def handle(request):
            captured.append(json.loads(request.content))
            self.assertNotIn('authorization',request.headers)
            return httpx.Response(200,json={'done':True,'message':{'tool_calls':[{'function':{'name':'query_health_data','arguments':{'indicator':'glucose','mode':'latest'}}}]}})
        provider=OllamaProvider(self.settings(),client=httpx.Client(base_url='http://127.0.0.1:11435',transport=httpx.MockTransport(handle)))
        reply=provider.complete(instructions='test',messages=[{'role':'user','content':'query'}],tools=tool_specs())
        call=reply.calls[0]
        output={'evidence':[{'source_id':'G032','locator':{'table':'health_records'},'record':{'value':'6.9'}}],'data':{'value':'6.9'}}
        messages=reply.output_items+[{'type':'function_call_output','call_id':call['call_id'],'output':json.dumps(output)}]
        replay=native_messages('test',messages)
        self.assertEqual(replay[-1]['tool_name'],'query_health_data')
        self.assertEqual(json.loads(replay[-1]['content'])['data']['value'],'6.9')
        self.assertNotIn('record',json.loads(replay[-1]['content'])['evidence'][0])
        self.assertNotIn('locator',json.loads(replay[-1]['content'])['evidence'][0])
        self.assertIn('record',output['evidence'][0])
        self.assertIn('locator',output['evidence'][0])
    def test_final_format_is_separate_request(self):
        captured=[]
        def handle(request):
            payload=json.loads(request.content);captured.append(payload)
            return httpx.Response(200,json={'done':True,'message':{'content':'hello' if len(captured)==1 else '{"answer":"hello","citations":[],"needs_clarification":false}'}})
        provider=OllamaProvider(self.settings(),client=httpx.Client(base_url='http://127.0.0.1',transport=httpx.MockTransport(handle)))
        reply=provider.complete(instructions='test',messages=[{'role':'user','content':'hi'}],tools=tool_specs())
        self.assertEqual(reply.usage['backend_requests'],2)
        self.assertEqual(captured[0]['tools'],captured[1]['tools'])
        self.assertEqual(captured[0]['messages'][0],captured[1]['messages'][0])
        self.assertNotIn('format',captured[0]);self.assertIn('format',captured[1])
    def test_truncated_call_not_executed(self):
        client=httpx.Client(base_url='http://127.0.0.1',transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'done':True,'done_reason':'length','message':{}})))
        with self.assertRaises(ProviderError): OllamaProvider(self.settings(),client=client).complete(instructions='',messages=[],tools=[])
    def test_exact_json_function_envelope_is_an_actual_model_call(self):
        body={'done':True,'message':{'content':json.dumps({'name':'query_health_data','arguments':{'indicator':'glucose','mode':'latest'}})}}
        client=httpx.Client(base_url='http://127.0.0.1',transport=httpx.MockTransport(lambda request:httpx.Response(200,json=body)))
        reply=OllamaProvider(self.settings(),client=client).complete(instructions='',messages=[],tools=tool_specs())
        self.assertEqual(reply.calls[0]['name'],'query_health_data')
        self.assertEqual(reply.usage['backend_requests'],1)
        self.assertEqual(reply.text,'')
    def test_valid_final_json_avoids_second_generation(self):
        requests=[]
        def handle(request):
            requests.append(request)
            return httpx.Response(200,json={'done':True,'message':{'content':'{"answer":"hello","citations":[],"needs_clarification":false}'}})
        client=httpx.Client(base_url='http://127.0.0.1',transport=httpx.MockTransport(handle))
        reply=OllamaProvider(self.settings(),client=client).complete(instructions='',messages=[],tools=[])
        self.assertEqual(len(requests),1)
        self.assertEqual(reply.usage['backend_requests'],1)
        self.assertEqual(json.loads(reply.text)['answer'],'hello')
    def test_comparison_projection_preserves_statistics_and_original_records(self):
        output={'status':'ok','data':{'comparisons':[{'before':{'count':1,'mean':'7.95'}}],
                'before':{'records':[{'record_id':'G001','value':7.95}]},'after':{'records':[]}},'evidence':[]}
        messages=[{'type':'function_call','name':'query_health_data','call_id':'x','arguments':'{}'},
                  {'type':'function_call_output','call_id':'x','output':json.dumps(output)}]
        projected=json.loads(native_messages('',messages)[-1]['content'])
        self.assertEqual(projected['data']['comparisons'],output['data']['comparisons'])
        self.assertEqual(projected['data']['before']['record_ids'],['G001'])
        self.assertIn('records',output['data']['before'])
    def test_comparison_json_without_event_citation_requires_reformatting(self):
        calls=[]
        def handle(request):
            calls.append(request)
            content='{"answer":"mean 7","citations":["G001"],"needs_clarification":false}' if len(calls)==1 else '{"answer":"mean 7","citations":["G001","E007"],"needs_clarification":false}'
            return httpx.Response(200,json={'done':True,'message':{'content':content}})
        client=httpx.Client(base_url='http://127.0.0.1',transport=httpx.MockTransport(handle))
        messages=[{'type':'function_call','name':'query_health_data','call_id':'x','arguments':'{}'},
                  {'type':'function_call_output','call_id':'x','output':json.dumps({'status':'ok','data':{'event':{'event_id':'E007'},'comparisons':[{}]},'evidence':[{'source_id':'G001'},{'source_id':'E007'}]})}]
        reply=OllamaProvider(self.settings(),client=client).complete(instructions='',messages=messages,tools=tool_specs())
        self.assertEqual(len(calls),2)
        self.assertIn('E007',json.loads(reply.text)['citations'])
