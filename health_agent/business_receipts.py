"""Terminal business results are explicit code-owned receipts, not LLM-generated facts."""
from health_agent.plotting import LABELS

def business_receipt(result,language="zh"):
    status=result['status'];data=result['data'];message=(result.get('error') or {}).get('message','')
    if status=='unsupported':
        text=message or '首版不支持此请求。'
        if data.get('requested_unit'):text+='请求单位：'+data['requested_unit']+'；支持单位：'+data.get('allowed_unit','标准单位')+'。'
    elif status=='no_data':
        text='当前用户在指定范围没有符合条件的记录。'
        scope=data.get('range',{})
        if scope.get('start_date'):text+='查询区间：['+scope['start_date']+', '+scope['end_date_exclusive']+')，终日不含。'
    elif status=='insufficient_data':
        text=('窗口尚未结束，' if not data.get('window_complete',True) else '样本不足，')+'无法进行有效比较，未给出变化结论。'
        for comp in data.get('comparisons',[]):text+=LABELS.get(comp['indicator'],comp['indicator'])+'：前窗'+str(comp['before']['count'])+'条、后窗'+str(comp['after']['count'])+'条。'
        text+='每指标每窗至少'+str(data.get('minimum_records_per_window',3))+'条。'
    elif status=='conflicting_events':
        events='、'.join(e['event_id']+'（'+e['event_date']+'）' for e in data.get('conflicting_events',[]))
        text='比较窗口内存在其他用药变更事件：'+events+'。无法进行有效比较，未给出变化结论。'
    else:return None
    if language=='en':
        from health_agent.i18n import indicator_label
        if status=='unsupported':
            text='This request is not supported; unit conversion is unavailable.'
            if data.get('requested_unit'):text+=' Requested unit: '+data['requested_unit']+'; supported unit: '+data.get('allowed_unit','standard unit')+'.'
        elif status=='no_data':
            text='No matching records were found for the current patient in the requested range.'
            if data.get('range',{}).get('start_date'):text+=' Query range: ['+data['range']['start_date']+', '+data['range']['end_date_exclusive']+'), end excluded.'
        elif status=='insufficient_data':
            text=('The observation window is incomplete. ' if not data.get('window_complete',True) else 'Insufficient samples. ')+'A valid comparison cannot be made; no change conclusion is provided.'
            for comp in data.get('comparisons',[]):text+=' '+indicator_label(comp['indicator'],'en')+': before '+str(comp['before']['count'])+' records, after '+str(comp['after']['count'])+' records.'
            text+=' At least '+str(data.get('minimum_records_per_window',3))+' records per indicator per window are required.'
        elif status=='conflicting_events':text='Other medication changes occur in the comparison window: '+', '.join(e['event_id']+' ('+e['event_date']+')' for e in data.get('conflicting_events',[]))+'. A valid comparison cannot be made; no change conclusion is provided.'
    return {'status':'ok','answer':text,'citations':list(dict.fromkeys(e['source_id'] for e in result['evidence'])),'needs_clarification':False,
            'error':None,'answer_origin':'business_receipt','business_status':status,'model_answer_accepted':None,'source_result_id':result['result_id']}


def series_receipt(result,language="zh"):
    """A completed raw record list is rendered by the UI without model rewriting."""
    data=result['data'];params=data.get('parameters',{})
    if result['status']!='ok' or params.get('mode')!='series' or not data.get('records'):return None
    scope=data['range']
    text='已查询到'+str(data['count'])+'条原始记录，完整日期、数值、单位和测量条件见下方记录明细表。'
    if scope.get('start_date'):text+='查询区间：['+scope['start_date']+', '+scope['end_date_exclusive']+')，终日不含。'
    if language=='en':
        text='Retrieved '+str(data['count'])+' original records. See the complete record table for dates, values, units and measurement conditions.'
        if scope.get('start_date'):text+=' Query range: ['+scope['start_date']+', '+scope['end_date_exclusive']+'), end excluded.'
    return {'status':'ok','answer':text,'citations':list(dict.fromkeys(e['source_id'] for e in result['evidence'])),
            'needs_clarification':False,'error':None,'answer_origin':'record_receipt','model_answer_accepted':None,'source_result_id':result['result_id']}


def latest_receipt(result,language='zh'):
    """Return the actual latest measurement, never a claim about total history."""
    data=result['data'];params=data.get('parameters',{});rows=data.get('records',[])
    if result['status']!='ok' or params.get('mode')!='latest' or not rows:return None
    from decimal import Decimal
    from health_agent.i18n import indicator_label,CONDITIONS
    def number(value):
        text=format(Decimal(str(value)),'f');return text.rstrip('0').rstrip('.') if '.' in text else text
    if params['indicator']=='blood_pressure':
        pair=data['measurements'][0];value=number(pair['systolic'])+'/'+number(pair['diastolic'])+' '+data['unit'];label='Blood pressure' if language=='en' else '血压'
    else:
        row=rows[0];value=number(row['value'])+' '+row['unit'];label=indicator_label(row['indicator'],language)
    context=rows[0]['measurement_context'];time=rows[0]['local_time']
    ids=list(dict.fromkeys(e['source_id'] for e in result['evidence']));references=' '.join('['+identifier+']' for identifier in ids)
    if language=='en':
        text='Latest '+label.lower()+': '+value+'. Measured at '+time+' (local time). Measurement condition: '+CONDITIONS[context].lower()+'. '+references+' This query returns the latest measurement, not the complete history.'
    else:
        contexts={'fasting':'空腹','postprandial':'餐后','random':'随机','unknown':'条件未知'}
        text='最近一次'+label+'：'+value+'。测量时间：'+time+'（本地时间）。测量条件：'+contexts[context]+'。'+references+' 本次查询只返回最近测量，不代表完整历史。'
    return {'status':'ok','answer':text,'citations':ids,'needs_clarification':False,'error':None,'answer_origin':'record_receipt','record_query_mode':'latest','model_answer_accepted':None,'source_result_id':result['result_id']}
