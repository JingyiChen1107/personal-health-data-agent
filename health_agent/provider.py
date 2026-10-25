"""OpenAI Responses adapter; retain tool/reasoning items for correct protocol replay."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from health_agent.model_config import ModelSettings
from health_agent.tool_specs import FINAL_SCHEMA


class ProviderError(RuntimeError):
    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


@dataclass
class ModelReply:
    output_items: list[dict]
    calls: list[dict] = field(default_factory=list)
    text: str = ''
    usage: dict = field(default_factory=dict)


class ModelProvider(Protocol):
    def complete(self, *, instructions: str, messages: list[dict], tools: list[dict]) -> ModelReply: ...


class OpenAIProvider:
    def __init__(self, settings: ModelSettings, *, timeout_seconds=30, client=None):
        self.settings = settings
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=settings.api_key, timeout=timeout_seconds, max_retries=0)
        self.client = client

    def complete(self, *, instructions, messages, tools):
        from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError, RateLimitError
        try:
            response = self.client.responses.create(
                model=self.settings.model, instructions=instructions, input=messages,
                tools=tools, parallel_tool_calls=False, store=False,
                include=['reasoning.encrypted_content'],
                max_output_tokens=self.settings.max_output_tokens,
                text={'format': FINAL_SCHEMA},
            )
        except AuthenticationError:
            raise ProviderError('authentication_failed', '模型认证失败，请检查本地密钥。') from None
        except RateLimitError:
            raise ProviderError('rate_limited', '模型服务暂时限流或额度不足。', retryable=True) from None
        except (APITimeoutError, APIConnectionError):
            raise ProviderError('connection_failed', '模型服务连接失败或超时。', retryable=True) from None
        except APIStatusError as exc:
            retryable = exc.status_code >= 500
            raise ProviderError('service_error' if retryable else 'request_rejected',
                                '模型服务请求失败，请检查模型权限与工具调用支持。', retryable=retryable) from None
        except Exception:
            raise ProviderError('unexpected_provider_error', '模型接口执行失败，未显示服务内部错误内容。') from None
        if response.status != 'completed':
            raise ProviderError('incomplete_response', '模型未完整返回本轮结果，未执行不完整的工具请求。')
        output = [item.model_dump(exclude_none=True) for item in response.output]
        calls = []
        for item in output:
            if item.get('type') == 'function_call':
                if not all(isinstance(item.get(k), str) and item[k] for k in ('name', 'arguments', 'call_id')):
                    raise ProviderError('invalid_tool_protocol', '模型返回的工具请求格式无效。')
                calls.append({'name': item['name'], 'arguments': item['arguments'], 'call_id': item['call_id']})
            elif item.get('type') == 'message' and any(c.get('type') == 'refusal' for c in item.get('content', [])):
                raise ProviderError('model_refusal', '模型未能处理此请求；请调整为健康记录查询。')
        if len({call['call_id'] for call in calls}) != len(calls):
            raise ProviderError('invalid_tool_protocol', '模型返回重复的工具调用标识。')
        usage = {}
        if response.usage is not None:
            usage = {name: int(getattr(response.usage, name, 0) or 0)
                     for name in ('input_tokens', 'output_tokens', 'total_tokens')}
        return ModelReply(output_items=output, calls=calls, text=response.output_text or '', usage=usage)
