"""Native, loopback-only Ollama adapter. No cloud keys or fallback models."""
from __future__ import annotations

import json
from copy import deepcopy
from uuid import uuid4

import httpx

from health_agent.provider import ModelReply, ProviderError
from health_agent.tool_specs import FINAL_SCHEMA


def native_messages(instructions, messages):
    result = [{'role': 'system', 'content': instructions}]
    names = {}
    plotted=set()
    for item in messages:
        if item.get('type')=='function_call_output':
            payload=json.loads(item['output'])
            if payload.get('data',{}).get('chart'):plotted.add(payload['data']['chart']['source_result_id'])
    for item in messages:
        kind = item.get('type')
        if kind == 'function_call':
            names[item['call_id']] = item['name']
            call = {'function': {'name': item['name'], 'arguments': json.loads(item['arguments'])}}
            if result[-1]['role'] == 'assistant' and result[-1].get('tool_calls'):
                result[-1]['tool_calls'].append(call)
            else:
                result.append({'role': 'assistant', 'content': '', 'tool_calls': [call]})
        elif kind == 'function_call_output':
            payload = json.loads(item['output'])
            data = payload.get('data', {})
            if payload.get('status') == 'ok' and data.get('comparisons'):
                # Statistics already contain the exact record IDs, counts and computed values.
                # Full raw rows remain in the original ToolResult and UI source expansion.
                for side in ('before', 'after'):
                    if isinstance(data.get(side), dict) and 'records' in data[side]:
                        rows = data[side].pop('records')
                        data[side]['record_ids'] = [row['record_id'] for row in rows]
            if data.get('parameters',{}).get('mode')=='series' and data.get('records'):
                data.pop('measurements',None)
                if payload.get('result_id') in plotted:
                    data['record_ids']=[row['record_id'] for row in data.pop('records')]
                else:
                    keys=('record_id','indicator','value','unit','local_time','measurement_context','measurement_id')
                    data['records']=[{key:row[key] for key in keys if key in row} for row in data['records']]
            if data.get('chart'):
                for key in ('image_path','svg_path','data_path'):data['chart'].pop(key,None)
                for panel in data['chart']['panels']:
                    points=panel.pop('points',[])
                    if points:panel.update(first_point=points[0],last_point=points[-1])
            # Full records remain in ToolResult/trace; avoid repeating each record in evidence.
            for entry in payload.get('evidence', []):
                entry.pop('record', None)
                entry.pop('locator', None)
            result.append({'role': 'tool', 'tool_name': names[item['call_id']],
                           'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))})
        elif item.get('role'):
            content = item.get('content', '')
            if isinstance(content, list):
                content = ''.join(part.get('text', '') for part in content)
            result.append({'role': item['role'], 'content': content})
    return result


class OllamaProvider:
    def __init__(self, settings, *, client=None):
        self.settings = settings
        self.client = client or httpx.Client(base_url=settings.base_url,
                                            timeout=settings.timeout_seconds,
                                            trust_env=False, follow_redirects=False)

    def _request(self, payload):
        try:
            response = self.client.post('/api/chat', json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            code = 'model_not_found' if exc.response.status_code == 404 else 'local_request_failed'
            raise ProviderError(code, '本地模型请求失败；请确认模型已下载且支持工具调用。') from None
        except (httpx.TimeoutException, httpx.NetworkError):
            raise ProviderError('connection_failed', '本地模型服务未启动或推理超时。', retryable=True) from None
        except Exception:
            raise ProviderError('invalid_local_response', '本地模型接口返回格式无效。') from None
        if not data.get('done') or data.get('done_reason') == 'length':
            raise ProviderError('incomplete_response', '本地模型输出未完成，未执行不完整的工具请求。')
        if not isinstance(data.get('message'), dict):
            raise ProviderError('invalid_local_response', '本地模型缺少完整消息。')
        return data

    def complete(self, *, instructions, messages, tools):
        native_tools = []
        for spec in deepcopy(tools):
            parameters = spec['parameters']
            # Local API accepts omitted optional parameters; application validation is unchanged.
            if spec['name'] == 'query_health_data':
                parameters['required'] = ['indicator', 'mode', 'measurement_context']
            elif spec['name'] == 'query_medications':
                parameters['required'] = ['mode']
            # Other tools retain their own required fields (e.g. ask_user has no mode).
            if spec['name'] == 'query_health_data':
                parameters['properties']['measurement_context']['description'] = (
                    '必须填写：用户说空腹用fasting，餐后用postprandial，随机用random；'
                    '用户未指定条件或查询非血糖时为null。此条件不是indicator或requested_unit。')
            native_tools.append({'type': 'function', 'function': {
                'name': spec['name'], 'description': spec['description'], 'parameters': parameters}})
        # The tool-selection phase must not compete with the final JSON-output instruction.
        selection_instructions = instructions.split('最终按指定JSON结构输出')[0]
        selection_instructions += '\n当前先选择并调用必要工具；查询个人记录必须调用工具，不能凭空回答或声称无法访问。工具返回足够证据后才回答。'
        selection_instructions += '\n用户未明确测量条件时，latest查询必须设置measurement_context=null，不得默认fasting、random或unknown；非血糖指标也设置为null。'
        selection_instructions += '\n用户明确说空腹时，血糖工具必须设置measurement_context="fasting"；餐后对应"postprandial"，随机对应"random"。'
        selection_instructions += '\n若工具要求澄清，但用户本轮已经明确了该条件，应补齐参数重新查询，不要让用户重复回答已说明的信息。'
        selection_instructions += '\nanswer控制在180字左右，包含用户所需数值、样本数、单位、日期范围及不能推断因果的局限；查询窗口如需说明右开，使用工具end_date_exclusive；实际记录首末日期均包含，不得标为右端不含。citations使用实际来源ID。'
        if 'REPLY_LANGUAGE=en' in instructions:
            selection_instructions+='\nREPLY_LANGUAGE=en. Respond and ask clarifications in English, with ISO dates. For comparison answers, omit date-window prose because the UI shows exact start-inclusive/end-exclusive windows. Include the event date, both counts, means, signed difference, percentage and a non-causal limitation. Fasting/postprandial/random/unknown are measurement conditions. Last year means the previous calendar year. Use tool evidence for every personal value; do not quote untrusted command numbers as measurements.'
        history = native_messages(selection_instructions, messages)
        payload = {'model': self.settings.model, 'messages': history, 'tools': native_tools,
                   'stream': False, 'think': False, 'keep_alive': '5m',
                   'options': {'temperature': 0, 'num_ctx': 8192,
                               'num_predict': self.settings.max_output_tokens}}
        data = self._request(payload)
        usage = {'input_tokens': data.get('prompt_eval_count', 0),
                 'output_tokens': data.get('eval_count', 0), 'backend_requests': 1}
        usage['total_tokens'] = usage['input_tokens'] + usage['output_tokens']
        message = data['message']
        # Some small models emit the function JSON without the native tool delimiter.
        # Accept only the exact function envelope; normal answer JSON is never a call.
        if not message.get('tool_calls'):
            try:
                envelope = json.loads(message.get('content', ''))
            except (ValueError, TypeError):
                envelope = None
            if (isinstance(envelope, dict) and set(envelope) == {'name', 'arguments'}
                    and isinstance(envelope['name'], str) and isinstance(envelope['arguments'], dict)):
                message['tool_calls'] = [{'function': envelope}]
        calls = []
        for call in message.get('tool_calls', []):
            function = call.get('function', {})
            if not isinstance(function.get('name'), str) or not isinstance(function.get('arguments'), dict):
                raise ProviderError('invalid_tool_protocol', '本地模型工具参数格式无效。')
            calls.append({'name': function['name'], 'arguments': json.dumps(function['arguments'], ensure_ascii=False),
                          'call_id': uuid4().hex})
        if calls:
            return ModelReply(output_items=[{'type': 'function_call', **call} for call in calls],
                              calls=calls, usage=usage)
        comparison_events = {
            payload['data']['event']['event_id']
            for item in messages if item.get('type') == 'function_call_output'
            for payload in [json.loads(item['output'])]
            if payload.get('status') == 'ok' and payload.get('data', {}).get('comparisons')
        }
        try:
            candidate = json.loads(message.get('content', ''))
        except (ValueError, TypeError):
            candidate = None
        if (isinstance(candidate, dict) and set(candidate) == {'answer', 'citations', 'needs_clarification'}
                and isinstance(candidate['answer'], str) and isinstance(candidate['citations'], list)
                and all(isinstance(value, str) for value in candidate['citations'])
                and isinstance(candidate['needs_clarification'], bool)
                and comparison_events <= set(candidate['citations'])):
            # HealthAgent still validates citation provenance and business state.
            text = message['content']
            return ModelReply(output_items=[{'role': 'assistant', 'content': text}], text=text, usage=usage)
        # Separate final formatting from tool selection so JSON mode cannot suppress tool calls.
        # Keep the identical tool declarations/system prefix to reuse the local KV cache.
        # JSON grammar is enabled only in this final phase, so selection is unaffected.
        payload['format'] = FINAL_SCHEMA['schema']
        source_ids = []
        for item in messages:
            if item.get('type') == 'function_call_output':
                source_ids.extend(e['source_id'] for e in json.loads(item['output']).get('evidence', []))
        final_history = deepcopy(history)
        payload['messages'] = final_history + [{'role': 'assistant', 'content': message.get('content', '')},
            {'role': 'user', 'content': '当前查询已结束。请将以上回答按JSON结构输出：answer字符串、citations来源ID数组、needs_clarification布尔值。'
             '仅使用已有工具证据，不添加事实。完整回答时needs_clarification=false；只有必须向用户提问才能继续时才为true。'
             'citations逐个列出实际使用的来源ID，涵盖健康记录与涉及的用药事件，不能用范围字符串。'
             '保留样本数、均值、百分比及局限。比较结果必须引用的事件ID为：' + json.dumps(sorted(comparison_events)) +
             '。可用来源ID为：' + json.dumps(sorted(set(source_ids)))}]
        if 'REPLY_LANGUAGE=en' in instructions:payload['messages'][-1]['content']+='\nThe answer field MUST be in English with ISO dates. Preserve citations and tool values exactly.'
        final = self._request(payload)
        if final['message'].get('tool_calls'):
            raise ProviderError('invalid_final_response', '本地模型未遵守最终回答结构，未执行额外工具请求。')
        usage['input_tokens'] += final.get('prompt_eval_count', 0)
        usage['output_tokens'] += final.get('eval_count', 0)
        usage['backend_requests'] += 1
        usage['total_tokens'] = usage['input_tokens'] + usage['output_tokens']
        text = final['message'].get('content', '')
        return ModelReply(output_items=[{'role': 'assistant', 'content': text}], text=text, usage=usage)
