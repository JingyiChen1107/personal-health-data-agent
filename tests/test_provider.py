from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import APIConnectionError, AuthenticationError, RateLimitError

from health_agent.model_config import ModelConfigError, ModelSettings, model_readiness
from health_agent.provider import OpenAIProvider, ProviderError
from health_agent.tool_specs import tool_specs


class DumpItem:
    def __init__(self, value):
        self.value = value

    def model_dump(self, **kwargs):
        return self.value


class FakeResponses:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class ProviderConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dotenv = Path(temp.name) / '.env'
        self.env = {'LLM_API_KEY': 'test-secret-do-not-log', 'LLM_MODEL': 'explicit-test-model'}

    def test_missing_configuration_does_not_guess_model_or_key(self):
        status = model_readiness(env={}, dotenv_path=self.dotenv)
        self.assertFalse(status['configured'])
        self.assertIn('LLM_MODEL', status['message'])

    def test_model_key_hidden_from_repr_and_readiness(self):
        settings = ModelSettings.load(env=self.env, dotenv_path=self.dotenv)
        self.assertNotIn(self.env['LLM_API_KEY'], repr(settings))
        self.assertNotIn(self.env['LLM_API_KEY'], json.dumps(model_readiness(env=self.env, dotenv_path=self.dotenv)))
        self.assertEqual(settings.model, 'explicit-test-model')

    def test_env_takes_precedence_and_dotenv_interpolation_is_disabled(self):
        self.dotenv.write_text('LLM_API_KEY="${SECRET_VARIABLE}"\nLLM_MODEL=from-file\n', encoding='utf-8')
        settings = ModelSettings.load(env={'LLM_MODEL': 'from-env'}, dotenv_path=self.dotenv)
        self.assertEqual(settings.model, 'from-env')
        self.assertEqual(settings.api_key, '${SECRET_VARIABLE}')

    def test_openai_key_alias_supported_without_printing_value(self):
        settings = ModelSettings.load(env={'OPENAI_API_KEY': 'test-key', 'LLM_MODEL': 'test-model'}, dotenv_path=self.dotenv)
        self.assertEqual(settings.api_key, 'test-key')

    def test_unsupported_provider_not_silently_changed(self):
        with self.assertRaises(ModelConfigError):
            ModelSettings.load(env=self.env | {'LLM_PROVIDER': 'gemini'}, dotenv_path=self.dotenv)

    def test_invalid_output_token_limit_is_rejected(self):
        for value in ['0', '8193', 'nan', '2.5']:
            with self.subTest(value=value), self.assertRaises(ModelConfigError):
                ModelSettings.load(env=self.env | {'LLM_MAX_OUTPUT_TOKENS': value}, dotenv_path=self.dotenv)

    def test_sdk_request_uses_responses_strict_schema_and_local_state(self):
        response = SimpleNamespace(status='completed', output=[DumpItem({'type': 'function_call', 'name': 'query_medications', 'arguments': '{}', 'call_id': 'c1'})],
                                   output_text='', usage=SimpleNamespace(input_tokens=2, output_tokens=3, total_tokens=5))
        transport = FakeResponses(response)
        provider = OpenAIProvider(ModelSettings('test-model', 'never-log'), client=SimpleNamespace(responses=transport))
        reply = provider.complete(instructions='test', messages=[{'role': 'user', 'content': 'test'}], tools=tool_specs())
        request = transport.requests[0]
        self.assertFalse(request['store'])
        self.assertFalse(request['parallel_tool_calls'])
        self.assertIn('reasoning.encrypted_content', request['include'])
        self.assertTrue(request['text']['format']['strict'])
        self.assertEqual(reply.calls[0]['call_id'], 'c1')
        self.assertEqual(reply.usage['total_tokens'], 5)

    def test_incomplete_response_never_executes_partial_call(self):
        transport = FakeResponses(SimpleNamespace(status='incomplete', output=[], output_text='', usage=None))
        provider = OpenAIProvider(ModelSettings('test-model', 'never-log'), client=SimpleNamespace(responses=transport))
        with self.assertRaises(ProviderError) as caught:
            provider.complete(instructions='test', messages=[], tools=[])
        self.assertEqual(caught.exception.code, 'incomplete_response')

    def test_sdk_errors_redacted_and_retryability_classified(self):
        request = httpx.Request('POST', 'https://api.openai.com/v1/responses')
        response = httpx.Response(401, request=request)
        errors = [
            (AuthenticationError('SECRET', response=response, body={'secret': 'SECRET'}), False, 'authentication_failed'),
            (RateLimitError('SECRET', response=httpx.Response(429, request=request), body={}), True, 'rate_limited'),
            (APIConnectionError(message='SECRET', request=request), True, 'connection_failed'),
            (RuntimeError('SECRET'), False, 'unexpected_provider_error'),
        ]
        for error, retryable, code in errors:
            transport = FakeResponses(error=error)
            provider = OpenAIProvider(ModelSettings('test-model', 'never-log'), client=SimpleNamespace(responses=transport))
            with self.subTest(code=code), self.assertRaises(ProviderError) as caught:
                provider.complete(instructions='test', messages=[], tools=[])
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertEqual(caught.exception.code, code)

    def test_duplicate_call_ids_rejected_before_any_execution(self):
        item = {'type': 'function_call', 'name': 'query_health_data', 'arguments': '{}', 'call_id': 'same'}
        response = SimpleNamespace(status='completed', output=[DumpItem(item), DumpItem(item)], output_text='', usage=None)
        provider = OpenAIProvider(ModelSettings('test-model', 'never-log'), client=SimpleNamespace(responses=FakeResponses(response)))
        with self.assertRaises(ProviderError):
            provider.complete(instructions='test', messages=[], tools=[])


if __name__ == '__main__':
    unittest.main()
