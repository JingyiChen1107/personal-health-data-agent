"""Single-agent LangGraph loop; deterministic tools remain the sole data authority."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from time import monotonic, sleep
from typing import TypedDict

from health_agent.contracts import HealthQuery, MedicationQuery
from health_agent.prompts import instructions
from health_agent.provider import ModelProvider, ProviderError
from health_agent.results import ToolResult
from health_agent.tool_specs import tool_specs
from health_agent.tools import HealthTools


class AgentState(TypedDict):
    messages: list[dict]
    calls: list[dict]
    results: list[dict]
    trace: list[dict]
    confirmed_events: list[str]
    tool_count: int
    model_count: int
    correction_count: int
    retry_count: int
    final_corrections: int
    usage: dict
    final: dict | None
    model_text: str


def strict_json(text):
    def reject_constant(value):
        raise ValueError('Nonfinite JSON value')
    return json.loads(text, parse_constant=reject_constant)


class HealthAgent:
    def __init__(self, tools: HealthTools, provider: ModelProvider, *, event_sink=None, retry_sleep=sleep):
        self.tools, self.provider = tools, provider
        self.event_sink, self.retry_sleep = event_sink, retry_sleep
        from langgraph.graph import END, START, StateGraph
        builder = StateGraph(AgentState)
        builder.add_node('model', self._model)
        builder.add_node('tools', self._tools)
        builder.add_node('finish', self._finish)
        builder.add_edge(START, 'model')
        builder.add_conditional_edges('model', lambda s: 'end' if s['final'] else ('tools' if s['calls'] else 'finish'),
                                      {'end': END, 'tools': 'tools', 'finish': 'finish'})
        builder.add_conditional_edges('tools', lambda s: 'end' if s['final'] else 'model', {'end': END, 'model': 'model'})
        builder.add_conditional_edges('finish', lambda s: 'end' if s['final'] else 'model', {'end': END, 'model': 'model'})
        self.graph = builder.compile()

    def _emit(self, kind, **details):
        if self.event_sink:
            self.event_sink({'event': kind, **details})

    @staticmethod
    def _stopped(code, message):
        return {'status': 'error', 'answer': message, 'citations': [], 'needs_clarification': False,
                'error': {'code': code, 'message': message}}

    def run(self, question: str) -> dict:
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            return self._stopped('invalid_question', '请输入不超过4000字的非空问题。')
        state: AgentState = {'messages': [{'role': 'user', 'content': question.strip()}],
                             'calls': [], 'results': [], 'trace': [], 'confirmed_events': [],
                             'tool_count': 0, 'model_count': 0, 'correction_count': 0, 'retry_count': 0,
                             'final_corrections': 0, 'usage': {}, 'final': None, 'model_text': ''}
        started = monotonic()
        state = self.graph.invoke(state, config={'recursion_limit': 3 * (self.tools.config.max_tool_calls + 5)})
        result = deepcopy(state['final'])
        result.update({'trace': state['trace'], 'tool_results': state['results'],
                       'tool_calls': state['tool_count'], 'model_calls': state['model_count'],
                       'retries': state['retry_count'], 'usage': state['usage'],
                       'duration_ms': round((monotonic() - started) * 1000),
                       'reference_date': self.tools.context.reference_date.isoformat(),
                       'created_at': datetime.now(timezone.utc).isoformat()})
        return result

    def _instructions(self, state):
        return instructions(self.tools.context, self.tools.config)

    def _tool_specs(self, state=None):
        return tool_specs()

    def _model(self, state):
        if state['model_count'] >= self.tools.config.max_tool_calls + 3:
            return {'final': self._stopped('model_call_limit', '本轮已达到模型调用上限，请缩小问题范围。')}
        self._emit('model_started', model_call=state['model_count'] + 1)
        retries = state['retry_count']
        for attempt in range(self.tools.config.max_retries + 1):
            if state['model_count'] + attempt >= self.tools.config.max_tool_calls + 3:
                return {'retry_count': retries, 'model_count': state['model_count'] + attempt,
                        'final': self._stopped('model_call_limit', '本轮已达到模型请求上限，请缩小问题范围。')}
            try:
                reply = self.provider.complete(instructions=self._instructions(state),
                                               messages=deepcopy(state['messages']), tools=self._tool_specs(state))
                break
            except ProviderError as exc:
                if exc.retryable and attempt < self.tools.config.max_retries:
                    retries += 1
                    self._emit('model_retry', code=exc.code)
                    self.retry_sleep(0.25)
                    continue
                return {'retry_count': retries, 'model_count': state['model_count'] + attempt + 1,
                        'final': self._stopped(exc.code, str(exc))}
        usage = dict(state['usage'])
        for key, count in reply.usage.items():
            usage[key] = usage.get(key, 0) + count
        previous_ids = {item.get('call_id') for item in state['messages'] if item.get('type') == 'function_call'}
        if (len({call['call_id'] for call in reply.calls}) != len(reply.calls)
                or any(call['call_id'] in previous_ids for call in reply.calls)):
            return {'final': self._stopped('invalid_tool_protocol', '工具调用标识重复，本轮已停止。')}
        return {'messages': state['messages'] + reply.output_items, 'calls': reply.calls,
                'model_text': reply.text, 'model_count': state['model_count'] + attempt + 1,
                'retry_count': retries, 'usage': usage}

    def _tools(self, state):
        messages, trace, results = list(state['messages']), list(state['trace']), list(state['results'])
        confirmed_before = set(state['confirmed_events'])
        confirmed = set(confirmed_before)
        count, corrections = state['tool_count'], state['correction_count']
        for call in state['calls']:
            if count >= self.tools.config.max_tool_calls:
                return {'messages': messages, 'trace': trace, 'results': results, 'tool_count': count,
                        'final': self._stopped('tool_call_limit', '本轮已达到工具调用上限。已有查询证据可在来源中查看，请缩小问题范围。')}
            count += 1
            started = monotonic()
            name = call['name']
            arguments = None
            self._emit('tool_started', tool=name)
            try:
                arguments = strict_json(call['arguments'])
                if not isinstance(arguments, dict):
                    raise ValueError()
                if name not in ('query_health_data', 'query_medications', 'search_health_documents', 'plot_health_trend'):
                    result = ToolResult('error', error={'code': 'unknown_tool', 'message': '仅可调用已注册工具。'}).to_dict()
                elif name == 'plot_health_trend' and arguments.get('result_id') not in {r['result_id'] for r in state['results']}:
                    try:
                        self.tools.cache.get(arguments.get('result_id'),context=self.tools.context,tool='query_health_data')
                    except KeyError:
                        result=ToolResult('error',error={'code':'invalid_result_id','message':'绘图必须使用此前已返回的真实查询result_id；无效或过期时重新查询。'}).to_dict()
                    else:
                        result=self.tools.plot_health_trend(arguments)
                elif (name == 'query_health_data' and arguments.get('mode') == 'compare_event'
                      and arguments.get('event_id') not in confirmed_before):
                    result = ToolResult('error', error={'code': 'unconfirmed_event', 'message': '请先在前一次模型决策中调用query_medications events确认事件，再进行比较。'}).to_dict()
                else:
                    function = getattr(self.tools, name)
                    result = function(arguments)
            except (ValueError, TypeError):
                result = ToolResult('error', error={'code': 'invalid_tool_arguments', 'message': '工具参数不是有效JSON对象，请纠正一次。'}).to_dict()
            from health_agent.rag import DocumentQuery
            from health_agent.plotting import PlotQuery
            fields = {'query_health_data': HealthQuery, 'query_medications': MedicationQuery, 'search_health_documents': DocumentQuery, 'plot_health_trend': PlotQuery}.get(name, MedicationQuery).__dataclass_fields__
            safe_args = {key: value for key, value in (arguments or {}).items() if key in fields} if isinstance(arguments, dict) else {}
            entry = {'tool': name, 'call_id': call['call_id'], 'arguments': safe_args,
                     'status': result['status'], 'result_id': result['result_id'],
                     'evidence_ids': [e['source_id'] for e in result['evidence']],
                     'duration_ms': round((monotonic() - started) * 1000),
                     'error_code': result['error']['code'] if result['error'] else None}
            trace.append(entry)
            results.append(result)
            self._emit('tool_finished', **entry)
            messages.append({'type': 'function_call_output', 'call_id': call['call_id'],
                             'output': json.dumps(result, ensure_ascii=False, allow_nan=False)})
            if name == 'query_medications' and result['status'] == 'ok':
                confirmed.update(event['event_id'] for event in result['data'].get('events', []))
            if result['status'] == 'error':
                corrections += 1
                if corrections > 1:
                    return {'messages': messages, 'trace': trace, 'results': results, 'tool_count': count,
                            'correction_count': corrections,
                            'final': self._stopped('tool_correction_limit', '工具参数或执行连续失败，本轮已停止。请检查条件后重新提问。')}
        return {'messages': messages, 'trace': trace, 'results': results, 'confirmed_events': sorted(confirmed),
                'tool_count': count, 'correction_count': corrections, 'calls': []}

    def _finish(self, state):
        known = {e['source_id'] for result in state['results'] for e in result['evidence']}
        try:
            parsed = strict_json(state['model_text'])
            if (not isinstance(parsed, dict) or set(parsed) != {'answer', 'citations', 'needs_clarification'}
                    or not isinstance(parsed['answer'], str) or not parsed['answer'].strip()
                    or not isinstance(parsed['citations'], list)
                    or not all(isinstance(item, str) for item in parsed['citations'])
                    or type(parsed['needs_clarification']) is not bool):
                raise ValueError()
            unknown_latest = any(result.get('data',{}).get('parameters',{}).get('mode') == 'latest'
                and any(row.get('indicator') == 'glucose' and row.get('measurement_context') == 'unknown'
                        for row in result.get('data',{}).get('records',[])) for result in state['results'])
            if unknown_latest and any(word in parsed['answer'] for word in ('非空腹', '不是空腹', '非空肚')):
                raise ValueError()
            if set(parsed['citations']) - known:
                raise ValueError()
            if known and not parsed['citations'] and not parsed['needs_clarification']:
                raise ValueError()
            from health_agent.verification import validate_answer
            check=validate_answer(parsed['answer'],parsed['citations'],state['results']) if not parsed['needs_clarification'] else {'passed':True,'issues':[]}
            if not check['passed']:
                if state['final_corrections']<1:
                    reminder={'role':'user','content':'回答的事实、范围、数值、单位或来源没有匹配到工具证据。semantic检索不得断言全文没有；样本不足或事件冲突不得给变化结论。请按本轮真实工具结果重写最终JSON一次，不能自行新增统计或单位换算，不能新增查询。若引用报告内容，citations必须含对应报告来源，否则删除报告段落。可引用来源：'+json.dumps(sorted(known),ensure_ascii=False)+'。校验问题：'+json.dumps(check['issues'],ensure_ascii=False)}
                    if any(issue['code']=='unsupported_whole_report_absence' for issue in check['issues']):
                        reminder['content']+='\nDelete unsupported claims that the whole report does not mention, provide or contain a topic. Reply only with the requested supported quotation or translated recommendation from the cited excerpt. Do not add guesses about missing content or measurements. A document-wide absence needs an explicit matching statement in the source. 删除未获原文明确支持的全文缺失断言，只回答有来源支持的所问内容。'
                    return {'messages':state['messages']+[reminder],'final_corrections':1}
                from health_agent.verification import fallback_facts
                final=self._stopped('unverified_facts','回答未通过事实校验，已停止生成结论。请查看下方工具事实及原始来源。')
                final.update({'fallback_facts':fallback_facts(state['results']),'evidence_validation':check})
                return {'final':final}
        except (ValueError, TypeError):
            if state['final_corrections'] < 1:
                reminder = {'role': 'user', 'content': '最终答案格式、来源或测量条件表述不符合约定。unknown只能说明条件未知，不能说非空腹或不是空腹。只使用本轮evidence的source_id，按指定JSON结构重写一次；不要新增查询或编造证据。'}
                return {'messages': state['messages'] + [reminder], 'final_corrections': 1}
            return {'final': self._stopped('invalid_final_response', '回答未通过格式或来源校验，已保留工具结果供查看。')}
        return {'final': {'status': 'needs_clarification' if parsed['needs_clarification'] else 'ok',
                          **parsed, 'error': None}}
