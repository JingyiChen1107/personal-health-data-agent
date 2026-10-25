"""Day 4: single-agent graph with durable interrupts and app-enforced confirmation."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4
import fcntl
import hashlib
import json
import os
import re
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt
from health_agent.agent import AgentState, HealthAgent, strict_json
from health_agent.config import PROJECT_ROOT
from health_agent.results import ToolResult
from health_agent.tool_specs import tool_specs

ASK_SPEC = {'type':'function','name':'ask_user','strict':True,
 'description':'每次只问一个必要问题并暂停；用户下一轮回答后恢复原任务。已明确的条件不重复询问。事件选项只使用本轮真实事件ID。',
 'parameters':{'type':'object','properties':{
  'question':{'type':'string','maxLength':500},'choices':{'type':'array','maxItems':20,'items':{'type':'string'}},
  'missing_fields':{'type':'array','minItems':1,'maxItems':1,'items':{'type':'string','enum':['indicator','mode','start_date','measurement_context','event_id']}}},
 'required':['question','choices','missing_fields'],'additionalProperties':False}}
CONTEXTS={'fasting':['空腹','fasting'],'postprandial':['餐后','postprandial'],'random':['随机','random'],'unknown':['未知','unknown']}

class SessionState(AgentState):
    language: str
    pending: dict | None
    task_id: str
    dialogue: list[dict]
    user_texts: list[str]
    slots: dict
    selected_event: str | None

class ConversationAgent(HealthAgent):
    def __init__(self, tools, provider, *, checkpoint_path=None, **kwargs):
        self.language="zh"
        tools.require_explicit_comparison_context=True
        super().__init__(tools,provider,**kwargs)
        path=Path(checkpoint_path or PROJECT_ROOT/'data/conversations.sqlite3')
        path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600);os.fchmod(fd,0o600);os.close(fd)
        self.conn=sqlite3.connect(path,check_same_thread=False,timeout=30)
        self.saver=SqliteSaver(self.conn)
        identity=json.dumps([tools.context.patient_id,tools.context.session_id,tools.context.reference_date.isoformat(),str(Path(tools.database).resolve())] if hasattr(tools,'database') else [tools.context.patient_id,tools.context.session_id,tools.context.reference_date.isoformat(),str(path)],ensure_ascii=False)
        self.thread_id=hashlib.sha256(identity.encode()).hexdigest()
        self.lock_path=path.parent/('session_'+self.thread_id+'.lock')
        self.graph_config={'configurable':{'thread_id':self.thread_id},'recursion_limit':3*(tools.config.max_tool_calls+8)}
        builder=StateGraph(SessionState)
        for name,node in [('model',self._model),('tools',self._tools),('finish',self._finish),('ask_user',self._ask_user)]:builder.add_node(name,node)
        builder.add_edge(START,'model')
        builder.add_conditional_edges('model',lambda s:'end' if s['final'] else ('tools' if s['calls'] else 'finish'),{'end':END,'tools':'tools','finish':'finish'})
        for name in ('tools','finish'):
            builder.add_conditional_edges(name,lambda s:'ask_user' if s.get('pending') else ('end' if s['final'] else 'model'),{'ask_user':'ask_user','end':END,'model':'model'})
        builder.add_edge('ask_user','model')
        self.graph=builder.compile(checkpointer=self.saver)

    def close(self):
        self.conn.close()
        if hasattr(self.provider,'client'):self.provider.client.close()

    def _plot_requested(self,state):
        text='\n'.join(state.get('user_texts',[]))
        if re.search(r"\b(?:do not|don't|no need to)\s+(?:plot|draw|chart)\b",text,re.I):return False
        if re.search(r'\b(?:plot|draw|chart|graph)\b',text,re.I):return True
        if re.search(r'(?:不要|不用|不需要|别)[^。；\n]{0,8}(?:画|绘制|图)',text):return False
        return bool(re.search(r'画出|画个|画一|画.*图|绘制|趋势图|画图',text))

    def _chart_only(self,state):
        text='\n'.join(state.get('user_texts',[]))
        return self._plot_requested(state) and not any(w in text.casefold() for w in ['分析','解释','均值','平均','比较','多少','增长率','原因','analy','explain','mean','average','compare','how much','growth','why'])

    def _plot_sources(self,state):
        return [r['result_id'] for r in state['results'] if r['status']=='ok' and r['data'].get('records') and r['data'].get('parameters',{}).get('mode') in ('latest','series','summary')]

    def _plot_handles(self,state):
        return {'query_'+str(i+1):rid for i,rid in enumerate(self._plot_sources(state))}

    def _plot_specs(self,state):
        specs=[spec for spec in tool_specs() if spec['name']=='plot_health_trend']
        for spec in specs:
            spec['parameters']['properties']['result_id']={'type':'string','enum':list(self._plot_handles(state)),'description':'Use an application-owned query handle such as query_1. The application binds it to the exact session snapshot; never copy or guess a long result ID. 使用本任务查询句柄，不抄写长ID。'}
        return specs

    def _tool_specs(self,state=None):
        if state and any(r['status']=='conflicting_events' for r in state['results']):
            return []
        if state and self._plot_requested(state) and any(r['status']=='ok' and r['data'].get('chart') for r in state['results']):
            return []
        if state and self._plot_requested(state) and self._plot_sources(state) and not any(r['data'].get('chart') for r in state['results']):
            return self._plot_specs(state)
        return tool_specs()+[deepcopy(ASK_SPEC)]

    def _instructions(self,state):
        from health_agent.prompts import instructions
        prompt=instructions(self.tools.context,self.tools.config)
        if self.language=='en':
            prompt=prompt.replace('请用中文简洁回答。','Reply concisely in English. All explanations and clarifications must be in English; source quotations may retain their original language.')
            prompt+='\nREPLY_LANGUAGE=en. Use ISO dates (YYYY-MM-DD). Drug labels: metformin=二甲双胍; amlodipine=氨氯地平; atorvastatin=阿托伐他汀. English dates are normalized by the application; do not invent measurements. In English, unknown does not mean non-fasting. For semantic retrieval without support, say: The retrieved excerpts do not support this answer; they cannot establish absence from the full report.\n'

        extra='''\nDay 4会话已启用ask_user与SQLite暂停恢复。
“帮我看看血压”等未指明任务类型时，调用ask_user询问最近一次还是时间序列。已说明最近一次、日期、药物、空腹等条件时不重复问。
用药事件和血糖测量条件不明确必须ask_user；一次只问一个决定下一步的问题。不能替用户选择最新事件或默认空腹。
ask_user的choices是字符串数组；事件选项直接用真实event_id，条件用fasting/postprandial/random/unknown。pending_task_id由应用注入，不要自行生成或填写。
用户回复后继续原任务；无效/模糊回答继续追问。普通后续输入可继承指标和范围，新明确条件覆盖旧条件；切换体重或血压时measurement_context必须清空为null。
只回答当前任务，追问回复继续原任务；不要重复回答已经完成的旧问题。会话历史只用于理解条件，个人事实要重新查询；不要把上轮来源直接当本轮查询证据。用户取消后不再继续旧任务。
'''+'\n应用保存的上轮查询条件（仅用于承接，不是新的测量证据）：'+json.dumps(state.get('slots',{}),ensure_ascii=False)
        if self._plot_requested(state) and self._plot_sources(state) and not any(r['data'].get('chart') for r in state['results']):
            extra+='\n绘图请使用本任务查询句柄作为result_id，应用绑定真实快照。Plot using one of these exact query handles as result_id: '+json.dumps(list(self._plot_handles(state)))+'. Do not copy a long res_ identifier.'
        if any(r['status']=='conflicting_events' for r in state.get('results',[])):
            extra+='\n当前工具已返回conflicting_events，这是本轮比较的终止业务状态。所选事件已明确，不需要再选择事件或测量条件。不要调用ask_user或任何查询工具，只按已有evidence解释窗口内其他变更事件使比较不成立，不给数值变化结论。needs_clarification必须为false，引用真实冲突事件ID。'
        if any(r['status']=='ok' and r['data'].get('chart') for r in state.get('results',[])):
            extra+='\n图表已经完成并在页面展示。简要说明数据点数、实际记录首末日期和单位即可，不新增均值、正常范围或粗略区间，不编造链接或展示内部chart_id。'
        return prompt.replace('最终按指定JSON结构输出',extra+('\nREPLY_LANGUAGE=en. Reply and ask questions in English. Use ISO dates; original source quotes remain unchanged.\n' if self.language=='en' else '')+'\n最终按指定JSON结构输出',1)

    def _events(self,state):
        return {e['event_id']:e for result in state['results'] if result['status']=='ok' for e in result.get('data',{}).get('events',[])}

    def _event_allowed(self,state,event_id):
        events=self._events(state)
        if event_id not in events:return False
        if state.get('selected_event')==event_id:return True
        text='\n'.join(state['user_texts']);event=events[event_id]
        if re.search(r'(?<![A-Za-z0-9])'+re.escape(event_id)+r'(?![A-Za-z0-9])',text,re.I):return True
        day=event['event_date'];y,m,d=map(int,day.split('-'))
        dates=[day,f'{y}年{m}月{d}日',f'{y}/{m}/{d}']
        names=[e['medication_name'] for side in ('old_medication','new_medication') if (e:=event.get(side))]
        # This validates user authorization against actual records; it never selects a tool or computes an answer.
        from health_agent.date_intent import mentioned_dates
        from health_agent.i18n import english_medication
        return (day in mentioned_dates(text,self.tools.context.reference_date) or any(x in text for x in dates)) and any(n.casefold() in text.casefold() or english_medication(n).casefold() in text.casefold() for n in names)

    def _context_allowed(self,state,value):
        text='\n'.join(state['user_texts']).casefold()
        mentioned=[(m.start(),key) for key,aliases in CONTEXTS.items() for alias in aliases for m in re.finditer(re.escape(alias) if re.search(r'[\u4e00-\u9fff]',alias) else r'(?<![a-z-])'+re.escape(alias)+r'(?![a-z])',text) if not (key=='fasting' and re.search(r'(?:not|non)[ -]+$',text[:m.start()]))]
        if mentioned:return max(mentioned)[1]==value
        return state.get('slots',{}).get('indicator')=='glucose' and state['slots'].get('measurement_context')==value

    def _queue(self,state,question,field,choices=None,*,calls=None,origin='policy_guard'):
        # Clarifications collect typed input; model prose is not verified personal evidence.
        # Never display a fabricated result/measurement embedded in a question or choice.
        questions={
            'zh':{'mode':'你想查看最近一次测量，还是一段时间的记录？','measurement_context':'请选择测量条件：空腹、餐后、随机或条件未知。','event_id':'你想比较哪次用药调整？请选择真实事件ID。','indicator':'你想查询哪个健康指标？','start_date':'请提供有效的查询开始日期，格式为YYYY-MM-DD。'},
            'en':{'mode':'Would you like the latest measurement or a time series?','measurement_context':'Which measurement condition: fasting, postprandial, random, or unknown?','event_id':'Which medication event should be compared? Choose an event ID.','indicator':'Which health indicator would you like to query?','start_date':'Please specify a valid query start date in YYYY-MM-DD format.'}}
        question=questions[self.language].get(field,'Please provide the missing query condition.' if self.language=='en' else '请补充查询条件。')
        choices={'mode':['latest','series'],'measurement_context':list(CONTEXTS),'indicator':list(self.tools.config.indicators)+['blood_pressure'],'start_date':[],'event_id':sorted(self._events(state))}.get(field,[])
        if field=='event_id':
            from health_agent.i18n import english_medication
            question+=' '+ '; '.join(eid+': '+e['event_date']+' '+(english_medication(name) if self.language=='en' else name) for eid,e in self._events(state).items() if (name:=next((m['medication_name'] for side in ('new_medication','old_medication') if (m:=e.get(side))),'')) is not None)
        payload={'question':question,'choices':choices or [],'missing_fields':[field],
                 'pending_task_id':state['task_id'],'origin':origin}
        messages=list(state['messages']);trace=list(state['trace']);results=list(state['results'])
        for call in calls or []:
            result=ToolResult('needs_clarification',data=payload).to_dict()
            messages.append({'type':'function_call_output','call_id':call['call_id'],'output':json.dumps(result,ensure_ascii=False)})
            args=strict_json(call['arguments'])
            trace.append({'tool':call['name'],'call_id':call['call_id'],'arguments':args,'status':'needs_clarification',
                          'result_id':result['result_id'],'evidence_ids':[],'duration_ms':0,'error_code':'user_confirmation_required','origin':origin})
            results.append(result)
        if not calls or not any(c['name']=='ask_user' for c in calls):
            trace.append({'tool':'ask_user','call_id':'policy_'+uuid4().hex,'arguments':payload,'status':'needs_clarification',
                          'result_id':None,'evidence_ids':[],'duration_ms':0,'error_code':None,'origin':origin})
        return {'pending':payload,'messages':messages,'trace':trace,'results':results,'calls':[],'final':None}

    def _reject_event(self,state,allowed):
        messages=list(state['messages']);trace=list(state['trace']);results=list(state['results'])
        corrections=state['correction_count']+1
        for call in state['calls']:
            result=ToolResult('error',data={'allowed_event_ids':allowed},
                error={'code':'event_mismatch','message':'用户已经明确了唯一匹配的用药事件，候选ID已列出。请使用该event_id调用compare_event；测量条件不明确时传null，先由工具检查事件冲突，无冲突时再追问条件。不要让用户重复确认已明确的药物和日期。'}).to_dict()
            messages.append({'type':'function_call_output','call_id':call['call_id'],'output':json.dumps(result,ensure_ascii=False)})
            try:args=strict_json(call['arguments'])
            except (ValueError,TypeError):args={}
            trace.append({'tool':call['name'],'call_id':call['call_id'],'arguments':args,'status':'error','result_id':result['result_id'],
                          'evidence_ids':[],'duration_ms':0,'error_code':'event_mismatch','origin':'policy_guard'})
            results.append(result)
        update={'messages':messages,'trace':trace,'results':results,'calls':[],
                'tool_count':state['tool_count']+len(state['calls']),'correction_count':corrections}
        if corrections>1:update['final']=self._stopped('tool_correction_limit','模型未能正确使用已明确的事件条件，本轮停止。')
        return update

    def _tools(self,state):
        if any(r['status']=='conflicting_events' for r in state['results']):
            return {'final':self._stopped('business_state_terminal','窗口内存在其他用药变更，本轮不再执行额外查询或追问；已保留冲突证据。')}
        for call in state['calls']:
            if call['name']!='search_health_documents':continue
            try:args=strict_json(call['arguments'])
            except (ValueError,TypeError):continue
            if not isinstance(args,dict):continue
            user_text='\n'.join(state['user_texts'])
            required={}
            if '去年' in user_text or re.search(r'\blast year\b',user_text,re.I):
                year=self.tools.context.reference_date.year
                required={'start_date':f'{year-1}-01-01','end_date':f'{year}-01-01'}
            elif re.search(r'(?<![A-Za-z0-9])R[0-9A-F]+(?![A-Za-z0-9])',user_text) and not any(word in user_text.casefold() for word in ['年','月','去年','今年','最近','之前','之后','以前','以后','last year','this year','before','after','during','in 20']):
                required={'start_date':None,'end_date':None}
            if args.get('mode')=='mention_check' and not any(word in user_text.casefold() for word in ['提到','提及','出现','是否','有没有','完整检查','mention','contain','include','full scan','full text','full-text','entire report']):
                required['mode']='semantic'
            if any(args.get(k)!=v for k,v in required.items()):
                result=ToolResult('error',data={'required_parameters':required},error={'code':'report_date_mismatch','message':'检索条件与用户范围不一致。去年是上一自然年；仅指定报告编号不能增加日期过滤；报告内容问答用semantic，全文是否提及才用mention_check。请按required_parameters纠正。'}).to_dict()
                messages=list(state['messages']);results=list(state['results']);trace=list(state['trace'])
                for c in state['calls']:
                    messages.append({'type':'function_call_output','call_id':c['call_id'],'output':json.dumps(result,ensure_ascii=False)})
                results.append(result)
                trace.append({'tool':call['name'],'call_id':call['call_id'],'arguments':args,'status':'error','result_id':result['result_id'],'evidence_ids':[],'duration_ms':0,'error_code':'report_date_mismatch','origin':'policy_guard'})
                corrections=state['correction_count']+1
                update={'messages':messages,'results':results,'trace':trace,'calls':[],'tool_count':state['tool_count']+len(state['calls']),'correction_count':corrections}
                if corrections>1:update['final']=self._stopped('tool_correction_limit','报告参数纠正失败，未返回错误范围的结论。')
                return update
        asks=[c for c in state['calls'] if c['name']=='ask_user']
        if asks:
            if len(state['calls'])!=1:return {'final':self._stopped('invalid_tool_protocol','追问必须单独调用，不能同时执行查询。')}
            try:
                args=strict_json(asks[0]['arguments'])
                if isinstance(args,dict):
                    events=self._events(state)
                    allowed=[eid for eid in events if self._event_allowed(state,eid)]
                    options=args.get('choices',[])
                    fields=args.get('missing_fields',[])
                    event_question=fields==['event_id'] or (isinstance(options,list) and any(isinstance(c,str) and c in events for c in options))
                    checked_comparison=any(x.get('data',{}).get('parameters',{}).get('mode')=='compare_event' for x in state['results'])
                    if len(allowed)==1 and (event_question or (fields==['measurement_context'] and not checked_comparison)):
                        return self._reject_event(state,allowed)
                    if fields==[] and event_question:
                        args['missing_fields']=['event_id']
                    # Task identity is never accepted from a model; ignore legacy adapters' hints.
                    args.pop('pending_task_id',None)
                if (not isinstance(args,dict) or set(args)-set(ASK_SPEC['parameters']['properties']) or not isinstance(args.get('question'),str)
                    or not 1<=len(args['question'].strip())<=500 or not isinstance(args.get('choices',[]),list)
                    or len(args.get('choices',[]))>20 or not all(isinstance(c,str) and len(c)<=200 for c in args.get('choices',[]))
                    or not isinstance(args.get('missing_fields'),list) or len(args['missing_fields'])!=1
                    or args['missing_fields'][0] not in ASK_SPEC['parameters']['properties']['missing_fields']['items']['enum']):raise ValueError()
                field=args['missing_fields'][0];choices=args.get('choices',[])
                if field=='event_id':
                    named=set(re.findall(r'\bE\d+\b',args['question']+' '+ ' '.join(choices)))
                    if named-set(self._events(state)):raise ValueError()
                if field=='event_id' and not self._events(state):
                    result=ToolResult('error',data={'required_tool':'query_medications','required_parameters':{'mode':'events'}},error={'code':'events_required_before_clarification','message':'First call query_medications(mode=events) to read the real medication events before asking the user to choose an event ID. 先读取真实事件再追问。'}).to_dict()
                    update={'messages':state['messages']+[{'type':'function_call_output','call_id':asks[0]['call_id'],'output':json.dumps(result,ensure_ascii=False)}],'results':state['results']+[result],'trace':state['trace']+[{'tool':'ask_user','call_id':asks[0]['call_id'],'arguments':args,'status':'error','result_id':result['result_id'],'evidence_ids':[],'duration_ms':0,'error_code':'events_required_before_clarification','origin':'dependency_guard'}],'calls':[],'tool_count':state['tool_count']+1,'correction_count':state['correction_count']+1}
                    if update['correction_count']>1:update['final']=self._stopped('tool_correction_limit','必须先读取真实用药事件。')
                    return update
                if field=='event_id':
                    known=set(self._events(state))
                    named=set(re.findall(r'\bE\d+\b',args['question']+' '+ ' '.join(choices)))
                    if named-known:raise ValueError()
                    choices=list(known) if known else []
                    choices.sort()
                return self._queue(state,args['question'].strip(),field,choices,calls=asks,origin='model_tool')
            except (ValueError,TypeError,KeyError):return {'final':self._stopped('invalid_ask_user','追问参数无效，本轮已停止。')}
        clear_context_ids=set()
        for call in state['calls']:
            # Missing task type is a validation guard after model tool selection, not a query router.
            raw=re.sub(r'[\s。，！？!?]','',state['user_texts'][0])
            vague=raw in ('帮我看看血压','看看血压','帮我看下血压','看看我的血压','帮我看看我的血压') or bool(re.fullmatch(r'(?:helpme(?:lookat|check)|check|showme)(?:my)?bloodpressure[.!?]?',raw,re.I))
            clarified=len(state['user_texts'])>1 and any(w in '\n'.join(state['user_texts'][1:]) for w in ('latest','series','最近一次','最新','趋势','时间序列'))
            if call['name']=='query_health_data' and vague and not state.get('slots',{}).get('mode') and not clarified:
                return self._queue(state,'你想查看最近一次血压，还是一段时间的血压记录？','mode',['latest','series'],calls=state['calls'])
            try:args=strict_json(call['arguments'])
            except (ValueError,TypeError):continue
            if not isinstance(args,dict):continue
            if call['name']=='query_health_data' and args.get('mode')=='compare_event':
                # Keep the base graph's prior-decision dependency check for unknown event IDs.
                if args.get('event_id') not in state['confirmed_events']:continue
                if not self._event_allowed(state,args.get('event_id')):
                    events=self._events(state)
                    allowed=[eid for eid in events if self._event_allowed(state,eid)]
                    if len(allowed)==1:
                        return self._reject_event(state,allowed)
                    labels='；'.join(f"{k}：{e['event_date']} {next((m['medication_name'] for side in ('new_medication','old_medication') if (m:=e.get(side))), '')}" for k,e in events.items())
                    return self._queue(state,'你想比较哪次用药调整？请选择事件ID：'+labels,'event_id',list(events),calls=state['calls'])
                if args.get('indicator') in ('glucose','血糖') and (not args.get('measurement_context') or not self._context_allowed(state,args['measurement_context'])):
                    clear_context_ids.add(call['call_id'])
        normalized=deepcopy(state)
        bound_handles={}
        for call in normalized['calls']:
            try: args=strict_json(call['arguments'])
            except (ValueError,TypeError): continue
            if call['name']=='plot_health_trend' and isinstance(args,dict) and args.get('result_id') in self._plot_handles(state):
                bound_handles[call['call_id']]=args['result_id'];args['result_id']=self._plot_handles(state)[args['result_id']];call['arguments']=json.dumps(args,ensure_ascii=False)
            if call['name']=='query_medications' and isinstance(args,dict):
                from health_agent.i18n import canonical_medication
                if args.get('medication_name'):args['medication_name']=canonical_medication(args['medication_name']);call['arguments']=json.dumps(args,ensure_ascii=False)
            if call['name']=='query_health_data' and isinstance(args,dict):
                if args.get('mode')=='compare_event':
                    days=re.findall(r'\b(\d{1,2})\s+days?\b','\n'.join(state['user_texts']),re.I)
                    if len(days)==1:args['window_days']=int(days[0]);call['arguments']=json.dumps(args,ensure_ascii=False)
                if call['call_id'] in clear_context_ids:
                    args['measurement_context']=None
                    call['arguments']=json.dumps(args,ensure_ascii=False)
                text='\n'.join(state['user_texts'])
                # An explicit single unit is a user constraint, even when the model omits it.
                # Preserve numeric values; the existing tool rejects unsupported conversions.
                from health_agent.verification import UNIT_SPELLINGS
                unit_labels={'mg/dl':'mg/dL','mmol/l':'mmol/L','mmhg':'mmHg','kg':'kg','lb':'lb',**{k:v for k,v in UNIT_SPELLINGS.items() if v in ('mg/dL','mmol/L','mmHg','kg','lb')}}
                unit_pattern=r'(?<![A-Za-z])('+ '|'.join(re.escape(k) for k in sorted(unit_labels,key=len,reverse=True))+r')(?![A-Za-z])'
                explicit_units={unit_labels[m.casefold()] for m in re.findall(unit_pattern,text,re.I)}
                if len(explicit_units)==1:
                    args['requested_unit']=explicit_units.pop();call['arguments']=json.dumps(args,ensure_ascii=False)
                if args.get('indicator')=='glucose' and '分开' in text and not any(word in text for word in ['空腹','餐后','随机','未知']):
                    args['measurement_context']=None
                    call['arguments']=json.dumps(args,ensure_ascii=False)
                if args.get('indicator')=='glucose' and args.get('mode','latest')=='latest' and args.get('measurement_context') and not self._context_allowed(state,args['measurement_context']):
                    args['measurement_context']=None
                    call['arguments']=json.dumps(args,ensure_ascii=False)
                if args.get('mode','latest') in ('latest','series','summary'):
                    from health_agent.date_intent import explicit_bounds
                    try:bounds=explicit_bounds('\n'.join(state['user_texts']),self.tools.context.reference_date)
                    except ValueError:
                        return self._queue(state,'日期范围无效，请提供有效的起止日期。','start_date',[],calls=state['calls'])
                    if bounds:
                        args.update(bounds)
                        call['arguments']=json.dumps(args,ensure_ascii=False)
                non_glucose={a.casefold() for key,spec in self.tools.config.indicators.items() if key!='glucose' for a in [key,*spec['aliases']]}|{'blood_pressure','blood pressure','bp','血压'}
                if isinstance(args.get('indicator'),str) and args['indicator'].casefold() in non_glucose:
                    args['measurement_context']=None
                    call['arguments']=json.dumps(args,ensure_ascii=False)
        actual_arguments={c['call_id']:c['arguments'] for c in normalized['calls']}
        for message in normalized['messages']:
            if message.get('type')=='function_call' and message.get('call_id') in actual_arguments:
                message['arguments']=actual_arguments[message['call_id']]
        # Identical calls in one model decision reuse one immutable result, but all call IDs get outputs.
        unique=[];duplicates=[];seen={}
        bounded=len(normalized['calls'])<=self.tools.config.max_tool_calls-state['tool_count']
        for call in normalized['calls']:
            try:key=(call['name'],json.dumps(strict_json(call['arguments']),sort_keys=True,ensure_ascii=False))
            except (ValueError,TypeError):key=(call['name'],call['call_id'])
            if bounded and key in seen:duplicates.append((call,seen[key]))
            else:seen[key]=call['call_id'];unique.append(call)
        normalized['calls']=unique
        updated=super()._tools(normalized)
        for entry in updated.get('trace',[]):
            if entry['call_id'] in bound_handles:entry['model_source_handle']=bound_handles[entry['call_id']]
        if duplicates and not updated.get('final'):
            outputs={m['call_id']:m['output'] for m in updated['messages'] if m.get('type')=='function_call_output'}
            entries={t['call_id']:t for t in updated['trace']}
            for call,original in duplicates:
                updated['messages'].append({'type':'function_call_output','call_id':call['call_id'],'output':outputs[original]})
                entry=deepcopy(entries[original]);entry.update(call_id=call['call_id'],duration_ms=0,origin='same_batch_result_reuse');updated['trace'].append(entry)
            updated['tool_count']+=len(duplicates)

        slots=dict(state.get('slots',{}))
        for result in updated.get('results',[])[len(state['results']):]:
            if result['status']!='error' and 'indicator' in result.get('data',{}).get('parameters',{}):
                slots=dict(result['data']['parameters'])
                if result['status']=='ok':slots['result_id']=result['result_id']
        updated['slots']=slots
        new_results=updated.get('results',[])[len(state['results']):]
        if len(new_results)==1 and new_results[0]['status'] in ('no_data','unsupported','insufficient_data','conflicting_events') and not updated.get('final'):
            from health_agent.business_receipts import business_receipt
            receipt=business_receipt(new_results[0],self.language);updated['final']=receipt
            updated['dialogue']=state['dialogue']+[{'role':'assistant','content':receipt['answer']}]

        latest_text='\n'.join(state.get('user_texts',[])).casefold()
        wants_analysis=any(w in latest_text for w in ['分析','解释','均值','平均','比较','趋势','增长','原因','历史','analy','explain','mean','average','compar','trend','growth','why','history'])
        report_goal=any(w in latest_text for w in ['报告','report']) and not any(w in latest_text for w in ['按数据库','using the database','from the database','database result'])
        requested_reports={x.upper() for x in re.findall(r'(?<![a-z0-9])r(?:[0-9]{3}|[0-9a-f]{12})(?![a-z0-9])',latest_text)}
        report_mentioned=bool(requested_reports) or any(w in latest_text for w in ['报告','report'])
        read_reports={rid for result in updated.get('results',[]) if result['status']=='ok' for rid in result.get('data',{}).get('scope_report_ids',[])}
        report_ready=bool(read_reports) and requested_reports<=read_reports
        metric_count=sum(bool(re.search(pattern,latest_text,re.I)) for pattern in [r'血糖|\bglucose\b|blood sugar',r'体重|\bweight\b',r'血压|收缩压|舒张压|高压|低压|blood pressure|\bbp\b|systolic|diastolic'])
        if len(new_results)==1 and not updated.get('final') and not self._plot_requested(state) and not wants_analysis and not report_goal and (not report_mentioned or report_ready) and metric_count<=1:
            from health_agent.business_receipts import latest_receipt
            receipt=latest_receipt(new_results[0],self.language)
            if receipt:
                updated['final']=receipt;updated['dialogue']=state['dialogue']+[{'role':'assistant','content':receipt['answer']}]

        # Simple record-list completion is code-owned; analysis and plotting still continue.
        text='\n'.join(state.get('user_texts',[]))
        if len(new_results)==1 and not updated.get('final') and not self._plot_requested(state) and not any(w in text.casefold() for w in ['分析','解释','均值','平均','比较','趋势','增长','原因','报告','analy','explain','mean','average','compar','trend','growth','why','report']):
            from health_agent.business_receipts import series_receipt
            receipt=series_receipt(new_results[0],self.language)
            if receipt:
                updated['final']=receipt
                updated['dialogue']=state['dialogue']+[{'role':'assistant','content':receipt['answer']}]

        charts=[r for r in updated.get('results',[])[len(state['results']):] if r['status']=='ok' and r['data'].get('chart')]
        if charts and self._chart_only(state):
            chart=charts[-1]['data']['chart']
            units='、'.join(sorted({p['unit'] for p in chart['panels']}))
            text='图表已生成。'+str(chart['point_count'])+'个原始数据点，单位：'+units+'。实际记录首末时间：'+chart['date_range']['first_local_time']+' 至 '+chart['date_range']['last_local_time']+'。'
            if all(p['point_count']==1 for p in chart['panels']):text+='仅单点记录，无法判断趋势。'
            if self.language=='en':
                text='Chart generated with '+str(chart['point_count'])+' original data points. Unit: '+units+'. Actual first and last record times: '+chart['date_range']['first_local_time']+' to '+chart['date_range']['last_local_time']+'.'
                if all(p['point_count']==1 for p in chart['panels']):text+=' Only one point: a trend cannot be determined.'
            updated['final']={'status':'ok','answer':text,'citations':list(dict.fromkeys(e['source_id'] for e in charts[-1]['evidence'])),
                'needs_clarification':False,'error':None,'answer_origin':'tool_receipt','model_answer_accepted':None,'artifact_completed':True}
            updated['dialogue']=state['dialogue']+[{'role':'assistant','content':text}]

        if not updated.get('final'):
            for result in updated.get('results',[])[len(state['results']):]:
                if result['status']=='needs_clarification':
                    combined=dict(state,**updated)
                    fields=result.get('data',{}).get('missing_fields',[])
                    field='measurement_context' if 'context' in (result.get('error') or {}).get('code','') else (fields[0] if fields else 'start_date')
                    queued=self._queue(combined,(result.get('error') or {}).get('message','请补充查询所需条件。'),field,list(CONTEXTS) if field=='measurement_context' else [])
                    updated.update(queued);break
        return updated

    def _finish(self,state):
        # Language is enforced independently of model-generated values and citations.
        from health_agent.i18n import wrong_language
        try:language_candidate=strict_json(state['model_text'])
        except (ValueError,TypeError):language_candidate={}
        if isinstance(language_candidate,dict) and isinstance(language_candidate.get('answer'),str) and wrong_language(language_candidate['answer'],self.language):
            if state['final_corrections']<1:return {'messages':state['messages']+[{'role':'user','content':('Reply in English' if self.language=='en' else '请用中文回答')+'. Rewrite the final JSON once using only the existing evidence; do not make new queries or change values.'}],'final_corrections':1}
            return {'final':self._stopped('language_mismatch','回答语言不符合请求，未展示未经核实的翻译。')}
        # Confirmation takes precedence over wording correction: no unsupported comparison is shown.
        try:proposed=strict_json(state['model_text'])
        except (ValueError,TypeError):proposed={}
        if isinstance(proposed,dict) and not proposed.get('needs_clarification') and isinstance(proposed.get('answer'),str):
            if self._events(state) and not any(r.get('data',{}).get('comparisons') for r in state['results']) and re.search(r'\d+(?:\.\d+)?\s*[%％]|(?:均值|平均值|下降|上升)[^\n]{0,12}\d',proposed['answer']):
                return self._queue(state,'请先确认要比较的用药事件，再查询前后统计。','event_id',list(self._events(state)))
        if self._plot_requested(state) and self._plot_sources(state) and not any(r['status']=='ok' and r['data'].get('chart') for r in state['results']):
            if state['final_corrections']<1:
                return {'messages':state['messages']+[{'role':'user','content':'用户要求的图表尚未生成。请调用plot_health_trend，使用已有真实result_id：'+json.dumps(list(self._plot_handles(state)))+'。不要重复查询，不要只用文字代替图表。'}],'final_corrections':1}
            return {'final':self._stopped('chart_not_completed','图表任务未完成，已有查询事实保留供查看。')}
        updated=super()._finish(state)
        final=updated.get('final')
        if final and final.get('needs_clarification'):
            # Compatibility with providers that return a question instead of calling ask_user.
            field='event_id' if self._events(state) else ('measurement_context' if any(w in final['answer'] for w in ['空腹','餐后','测量条件','fasting','postprandial','measurement condition']) else 'mode')
            return self._queue(state,final['answer'],field,list(self._events(state)) if field=='event_id' else [])
        if final and final['status']=='ok':
            semantic=any(r.get('data',{}).get('coverage')=='top_k_only' for r in state['results'])
            negative=bool(re.search(r'未找到|未检索到|未发现|没有找到|没有[^。]{0,20}提及|not found|no (?:support|evidence)|does not mention',final['answer'],re.I))
            if semantic and negative and not re.search(r'片段|excerpt|retrieved (?:text|passage|chunk)',final['answer'],re.I):
                if state['final_corrections']<1:
                    return {'messages':state['messages']+[{'role':'user','content':'semantic仅覆盖检索片段。请重写最终JSON，明确“已检索片段中未找到支持依据，不能据此断言整份报告未提及”，保留真实引用；不要新增查询。'}],'final_corrections':1}
                return {'final':self._stopped('unsupported_absence_claim','回答未通过检索覆盖范围校验；请查看已保留的原文片段。')}
            has_comparison=any(r.get('data',{}).get('comparisons') for r in state['results'])
            claims_comparison=bool(re.search(r'\d+(?:\.\d+)?\s*[%％]|(?:均值|平均值|下降|上升)[^\n]{0,12}\d',final['answer']))
            if self._events(state) and not has_comparison and claims_comparison:
                return self._queue(state,'请先确认要比较的用药事件，再查询前后统计。','event_id',list(self._events(state)))
            updated['dialogue']=state['dialogue']+[{'role':'assistant','content':final['answer']}]
        return updated

    def _ask_user(self,state):
        # No tool or model execution before interrupt: replaying this node is idempotent.
        answer=interrupt(deepcopy(state['pending']))
        payload=state['pending'];selected=state.get('selected_event')
        if payload['missing_fields']==['event_id']:
            choices=payload['choices'];raw=answer.strip()
            if raw in choices:selected=raw
            elif raw.isdigit() and 1<=int(raw)<=len(choices):selected=choices[int(raw)-1]
        return {'messages':state['messages']+[{'role':'user','content':answer}],
                'dialogue':state['dialogue']+[{'role':'assistant','content':payload['question']},{'role':'user','content':answer}],
                'language':self.language,'user_texts':state['user_texts']+[answer],'selected_event':selected,'pending':None,'final':None,
                'model_count':0,'tool_count':0,'retry_count':0,'correction_count':0,'final_corrections':0,'usage':{}}

    def run(self,question,*,language=None):
        if not isinstance(question,str) or not question.strip() or len(question)>4000:
            from health_agent.i18n import language_for,english_error
            target=language_for(question if isinstance(question,str) else '',preferred=language)
            result=self._stopped('invalid_question',english_error('invalid_question') if target=='en' else '请输入不超过4000字的非空问题。');result['language']=target;return result
        with self.lock_path.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return self._run_locked(question.strip(),language=language)

    def _run_locked(self,question,*,language=None):
        snapshot=self.graph.get_state(self.graph_config);previous=snapshot.values or {}
        from health_agent.i18n import language_for
        self.language=language_for(question,previous.get('language','zh'),language)
        self.tools.language=self.language
        started=monotonic()
        if question.casefold() in ('取消','取消任务','停止当前任务','cancel','cancel task','stop current task'):
            self.saver.delete_thread(self.thread_id)
            if hasattr(self.tools.cache,'clear'):self.tools.cache.clear(context=self.tools.context)
            return {'status':'ok','answer':('The current task and session conditions have been cleared.' if self.language=='en' else '已取消当前任务并清空本会话条件。'),'citations':[],'needs_clarification':False,'error':None,'trace':[],'tool_results':[],'thread_id':self.thread_id,'pending':None,'session_state':{},'language':self.language}
        if snapshot.next and previous.get('pending'):
            output=self.graph.invoke(Command(resume=question),config=self.graph_config)
        else:
            dialogue=previous.get('dialogue',[])[-12:]+[{'role':'user','content':question}]
            state={'language':self.language,'messages':[deepcopy(m) if m['role']=='user' else {'role':'assistant','content':'上一轮任务已结束。后续仅承接查询条件；新的个人事实需要重新查询。'} for m in dialogue],'calls':[],'results':[],'trace':[],'confirmed_events':[],
                   'tool_count':0,'model_count':0,'correction_count':0,'retry_count':0,'final_corrections':0,
                   'usage':{},'final':None,'model_text':'','pending':None,'task_id':uuid4().hex,
                   'dialogue':dialogue,'user_texts':[question],'slots':previous.get('slots',{}),'selected_event':None}
            output=self.graph.invoke(state,config=self.graph_config)
        result=deepcopy(output.get('final'))
        if output.get('__interrupt__'):
            pending=output['__interrupt__'][0].value
            result={'status':'needs_clarification','answer':pending['question'],'citations':[],
                    'needs_clarification':True,'error':None,'pending':pending}
        result.update({'trace':output['trace'],'tool_results':output['results'],'tool_calls':output['tool_count'],
                       'model_calls':output['model_count'],'retries':output['retry_count'],'usage':output['usage'],
                       'duration_ms':round((monotonic()-started)*1000),'thread_id':self.thread_id,
                       'pending_task_id':output['task_id'],'pending':output.get('pending'),
                       'session_state':deepcopy(output.get('slots',{})),
                       'reference_date':self.tools.context.reference_date.isoformat(),'created_at':datetime.now(timezone.utc).isoformat()})
        result['language']=self.language
        if result['status']=='error' and self.language=='en':
            from health_agent.i18n import english_error
            result['answer']=english_error((result.get('error') or {}).get('code',''))
            if result.get('error'):result['error']['message']=result['answer']
        return result
