"""Conservative numeric checks against cited tool facts, not a general semantic proof."""
from decimal import Decimal,InvalidOperation
import re

NUMBER=r'[-+]?\d+(?:\.\d+)?'
VALUE_UNIT=re.compile(r'('+NUMBER+r')\s*(mmol\s*/\s*L|mg\s*/\s*dL|mmHg|kg|lbs?|mcg|µg|mg|g|[%％])',re.I)
DATES=re.compile(r'\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2}|Z)?)?|\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|\d{1,2}:\d{2}(?::\d{2})?')
ID=re.compile(r'(?<![A-Za-z0-9])(?:R[0-9A-F]+(?::(?:C|L)\d+|:SCAN)?|res_[a-f0-9]+|chart_[a-f0-9]+|(?:SBP|DBP|BP|G|W|R|E|M|P)\d+(?::[A-Z]\d+)?)(?![A-Za-z0-9])',re.I)

UNIT_SPELLINGS={'micrograms':'mcg','microgram':'mcg','grams':'g','gram':'g','µg':'mcg','μg':'mcg','mg per dL':'mg/dL','mmol per L':'mmol/L','millimoles per liter':'mmol/L','millimoles per litre':'mmol/L','milligrams per deciliter':'mg/dL','milligrams per decilitre':'mg/dL','millimeters of mercury':'mmHg','millimetres of mercury':'mmHg','kilograms':'kg','kilogram':'kg','milligrams':'mg','milligram':'mg','pounds':'lb','pound':'lb','公斤':'kg','千克':'kg','磅':'lb','毫米汞柱':'mmHg','毫摩尔每升':'mmol/L','毫克每分升':'mg/dL'}

UNIT_SPELLINGS={key.casefold():value for key,value in UNIT_SPELLINGS.items()}

def normalize_unit_words(text):
    # Spelling normalization only: values are never converted or rescaled.
    names='|'.join(re.escape(k) for k in sorted(UNIT_SPELLINGS,key=len,reverse=True))
    return re.sub(r'('+NUMBER+r')\s*('+names+r')(?![A-Za-z])',lambda m:m.group(1)+' '+UNIT_SPELLINGS[m.group(2).casefold()],text,flags=re.I)

def dec(value):
    try:return Decimal(str(value))
    except InvalidOperation:return None

def scalar_numbers(value):
    """Only actual numeric/decimal facts, never IDs, hashes or timestamp components."""
    result=set()
    if isinstance(value,bool) or value is None:return result
    if isinstance(value,(float,int)):result.add(Decimal(str(value)))
    elif isinstance(value,str):
        if re.fullmatch(NUMBER,value):result.add(Decimal(value))
    elif isinstance(value,list):
        for v in value:result.update(scalar_numbers(v))
    elif isinstance(value,dict):
        for k,v in value.items():
            if k in ('patient_id','record_id','record_ids','measurement_id','source_id','result_id','event_id','report_id','content_hash','start_char','end_char','start_line','end_line','score'):continue
            result.update(scalar_numbers(v))
    return result

def validate_answer(answer,citations,results):
    answer=normalize_unit_words(answer.replace('−','-').replace('－','-'))
    used=set(citations);selected=[r for r in results if used & {e['source_id'] for e in r['evidence']}]
    health_rows=[];units={};numbers=set();document_numbers=set();report_units={};comparisons=[];issues=[]
    date_values=set();years=set()
    def collect_dates(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key not in ('content_hash','source_id','record_id','result_id'):collect_dates(item)
        elif isinstance(value,list):
            for item in value:collect_dates(item)
        elif isinstance(value,str):
            for day in re.findall(r'\d{4}-\d{2}-\d{2}',value):date_values.add(day);years.add(day[:4])
    for result in results:
        if result in selected or result['status']=='no_data':collect_dates(result['data']);collect_dates(result['evidence'])
    from health_agent.date_intent import english_dates
    try:answer=english_dates(answer,int(max(years) if years else '2026'))
    except ValueError:issues.append({'code':'unsupported_english_date'})
    for match in re.finditer(r'\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|\d{4}年',answer):
        value=match.group(0);parts=re.findall(r'\d+',value)
        if len(parts)==3:valid=f'{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}' in date_values
        elif len(parts)==2:valid=any(d.endswith(f'-{int(parts[0]):02d}-{int(parts[1]):02d}') for d in date_values)
        else:valid=parts[0] in years
        if not valid:issues.append({'code':'unsupported_date','value':value})
    for result in results:
        if result['status']=='no_data':numbers.update(scalar_numbers(result['data']))
    for result in selected:
        data=result['data'];numbers.update(scalar_numbers(data));comparisons.extend(data.get('comparisons',[]) if result['status']=='ok' else [])
        for e in result['evidence']:
            scan_support=e['source_type']=='report_text' and (e.get('record',{}).get('report_id','')+':SCAN') in used
            if e['source_id'] not in used and e['source_type']!='medication' and not scan_support:continue
            record=e.get('record',{})
            if e['source_type']=='health_record':
                health_rows.append(record);units.setdefault(record['unit'].lower(),set()).add(dec(record['value']))
            elif e['source_type']=='medication':
                numbers.update(Decimal(v) for v in re.findall(NUMBER,record.get('dose','')))
                for value,unit in VALUE_UNIT.findall(normalize_unit_words(record.get('dose',''))):units.setdefault(unit.lower(),set()).add(Decimal(value))
            elif e['source_type']=='report_text':
                text=normalize_unit_words(record.get('text',''));document_numbers.update(Decimal(v) for v in re.findall(NUMBER,DATES.sub('',text)))
                for value,unit in VALUE_UNIT.findall(text):report_units.setdefault(re.sub(r'\s','',unit).lower(),set()).add(Decimal(value))
        # Aggregate values come from the tool, not recalculated by the model.
        for summary in data.get('summaries',[]):units.setdefault(summary['unit'].lower(),set()).update(scalar_numbers({k:v for k,v in summary['statistics'].items() if k not in ('count','record_ids')}))
        for comparison in (data.get('comparisons',[]) if result['status']=='ok' else []):
            units.setdefault(comparison['unit'].lower(),set()).update(scalar_numbers({'before':{k:v for k,v in comparison['before'].items() if k not in ('count','record_ids')},'after':{k:v for k,v in comparison['after'].items() if k not in ('count','record_ids')},'delta':comparison.get('delta')}))
            if comparison.get('percent_change') is not None:
                units.setdefault('%',set()).update([abs(Decimal(comparison['percent_change'])),abs(Decimal(comparison['display']['percent_change']))])
        chart=data.get('chart')
        if chart:
            for panel in chart['panels']:units.setdefault(panel['unit'].lower(),set()).update(dec(p['value']) for p in panel['points'])
    # Source line ranges are provenance, not signed measurement values (e.g. 第1-3行).
    # Remove them only after checking the exact range against cited evidence.
    report_locators={(e.get('record',{}).get('report_id'),e.get('locator',{}).get('start_line'),e.get('locator',{}).get('end_line')) for result in selected for e in result['evidence'] if e['source_id'] in used and e['source_type']=='report_text'}
    cited_reports={rid for rid,start,end in report_locators if rid}
    def source_line_range(match):
        rid=match.group(1).upper() if match.group(1) else (next(iter(cited_reports)) if len(cited_reports)==1 else None)
        start=int(match.group(2) or match.group(4));end=int(match.group(3) or match.group(5))
        if (rid,start,end) not in report_locators:issues.append({'code':'unsupported_source_locator','value':match.group(0)})
        return ''
    answer=re.sub(r'(?:(R(?:[0-9]{3}|[0-9A-F]{12}))\s*)?(?:第\s*([0-9]+)\s*[-–—至到]\s*([0-9]+)\s*行|\blines?\b\s+([0-9]+)\s*(?:[-–—]|to|through)\s*([0-9]+))',source_line_range,answer,flags=re.I)
    local_hours={int(str(row['local_time'])[11:13]) for result in selected for row in result['data'].get('records',[]) if row.get('local_time')}
    def time_phrase(match):
        hour=int(match.group(2));period=match.group(1) or ''
        if period in ('下午','晚上') and hour<12:hour+=12
        if hour not in local_hours:issues.append({'code':'unsupported_local_hour','value':str(hour)})
        return ''
    def english_time_phrase(match):
        hour=int(match.group(1))%12+(12 if match.group(3).casefold()=='pm' else 0);minute=int(match.group(2) or 0)
        known={(int(str(row['local_time'])[11:13]),int(str(row['local_time'])[14:16])) for result in selected for row in result['data'].get('records',[]) if row.get('local_time')}
        if (hour,minute) not in known:issues.append({'code':'unsupported_local_time','value':str(hour)+':'+str(minute)})
        return ''
    answer=re.sub(r'\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b',english_time_phrase,answer,flags=re.I)
    answer=re.sub(r'(上午|下午|晚上|早上)?(\d{1,2})点(?:整)?',time_phrase,answer)
    allowed_intervals=set()
    for result in selected:
        data=result['data'];ranges=[]
        if data.get('range'):ranges.append(data['range'])
        ranges.extend(data.get('windows',{}).values())
        if data.get('chart',{}).get('query_range'):ranges.append(data['chart']['query_range'])
        for item in ranges:
            if item.get('start_date') and item.get('end_date_exclusive'):allowed_intervals.add((item['start_date'],item['end_date_exclusive']))
    def normal_date(text,previous=None):
        parts=[int(v) for v in re.findall(r'\d+',text)]
        if len(parts)==3:return f'{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}'
        if previous and len(parts)==1:return previous[:8]+f'{parts[0]:02d}'
        if previous and len(parts)==2:return previous[:5]+f'{parts[0]:02d}-{parts[1]:02d}'
        return None
    range_pattern=r'(\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月\d{1,2}日)[^\d。；\n]{0,12}(\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日|\d{1,2}日)'
    for marker in re.finditer(r'右端(?:日期)?(?:不含|不包含)|不含(?:当日|当天|结束日)',answer):
        prefix=answer[max(0,marker.start()-140):marker.start()]
        pairs=list(re.finditer(range_pattern,prefix))
        if pairs:
            last=pairs[-1];left=normal_date(last.group(1));right=normal_date(last.group(2),left)
            if (left,right) not in allowed_intervals:issues.append({'code':'wrong_exclusive_range','start_date':left or '', 'end_date':right or '', 'allowed_ranges':str(sorted(allowed_intervals))})
    for sentence in re.split(r'[。；\n]',answer):
        report_role=any(w in sentence.casefold() for w in ['报告','文档','原文','引文','测试文本','指令测试','report','document','excerpt','quotation','untrusted text','checkup','check-up'])
        for value,unit in VALUE_UNIT.findall(sentence):
            key=re.sub(r'\s','',unit).lower().replace('％','%');number=Decimal(value)
            allowed=units.get(key,set())
            if key=='%':allowed=allowed | {-v for v in allowed}
            directional_magnitude=any(c.get('delta') is not None and Decimal(c['delta'])<0 and number==abs(Decimal(c['delta'])) and key==c['unit'].lower() and any(w in sentence.casefold() for w in ['下降','降低','减少','幅度','绝对差','decrease','decreased','drop','reduction','difference']) for c in comparisons)
            if number not in allowed and not directional_magnitude and not (report_role and number in report_units.get(key,set())):
                issues.append({'code':'unsupported_value_unit','value':value,'unit':unit})
        scrubbed=ID.sub('',DATES.sub('',sentence));scrubbed=re.sub(r'\d{4}年','',scrubbed);scrubbed=re.sub(r'(\d{1,2})日',lambda m:'' if any(int(d[-2:])==int(m.group(1)) for d in date_values) else m.group(0),scrubbed);scrubbed=re.sub(r'^\s*\d+(?:[.)]\s+|、\s*)','',scrubbed)
        for token in re.findall(NUMBER,scrubbed):
            number=Decimal(token)
            if number not in numbers and -number not in numbers and not (report_role and number in document_numbers):
                issues.append({'code':'unsupported_number','value':token})
    blocked=any(r['status'] in ('insufficient_data','conflicting_events') for r in selected)
    if blocked:
        for clause in re.split(r'[。；\n]',answer):
            if re.search(r'(?:下降|上升|降幅|升幅|减少了|增加了|变化率|均值从|均值由)[^。]{0,12}\d|\d+(?:\.\d+)?\s*[%％]',clause) and not any(w in clause for w in ['不能','无法','不支持','不作','未给出']):issues.append({'code':'business_state_has_no_change_conclusion'})
    health_queries=[r for r in selected if r['data'].get('parameters',{}).get('indicator')]
    latest_only=bool(health_queries) and all(r['data']['parameters'].get('mode','latest')=='latest' for r in health_queries)
    if latest_only:
        for clause in re.split(r'[。；;\n]|(?<=[.!?])\s+',answer):
            low=clause.casefold()
            limited=bool(re.search(r'query|returned|retrieved|result|本次查询|返回|查询结果|检索结果',low))
            english_claim=bool(re.search(r'\b(?:only|sole|no other)\b.{0,65}\b(?:records?|readings?|measurements?|data points?)\b',low))
            chinese_claim=bool(re.search(r'(?:系统|数据库|历史)[^。]{0,12}(?:唯一|仅有|只有)|(?:唯一|仅有一条|只有一条)[^。]{0,20}(?:历史|系统|数据库)',clause))
            if not limited and (english_claim or chinese_claim):issues.append({'code':'unsupported_history_uniqueness'})
    semantic_text='\n'.join(item.get('text','') for result in selected if result['data'].get('coverage')=='top_k_only' for item in result['data'].get('matches',[]))
    if semantic_text:
        for clause in re.split(r'[。；\n]',answer):
            if '报告' in clause and not any(w in clause for w in ['片段','不能','无法','不代表','不足','不支持']):
                for predicate in re.findall(r'(?:没有|不包含|未提及|未提到)[^，,。；]{1,32}',clause):
                    literal=predicate.replace('的','') in semantic_text.replace('的','')
                    translated_synthetic=bool(re.fullmatch(r'(?:没有|不包含)(?:任何)?真实患者数据',predicate.strip()) and re.search(r'\bno real patient data\b',semantic_text,re.I))
                    if not literal and not translated_synthetic:issues.append({'code':'unsupported_whole_report_absence'})
    computed_means={Decimal(str(summary['statistics']['mean'])) for result in selected for summary in result['data'].get('summaries',[]) if summary['statistics'].get('mean') is not None}
    computed_means.update(Decimal(c[side]['mean']) for c in comparisons for side in ('before','after') if c[side].get('mean') is not None)
    if computed_means:
        for value in re.findall(r'(?:均值|平均值|平均(?:空腹|餐后|随机)?(?:血糖|体重|血压)?)(?:为|是|约|：|:)?\s*('+NUMBER+r')',answer):
            number=Decimal(value)
            if not any(abs(number-expected)<Decimal('0.0000001') for expected in computed_means):issues.append({'code':'wrong_mean_attribute','value':value})
    # Current latest/series health measurements cannot borrow report numbers as personal data.
    for match in re.finditer(r'(?:您|你|我|个人|最近一次|最新)[^。；\n]{0,25}?('+NUMBER+r')\s*(mmol/L|kg|mmHg)',answer):
        fragment=match.group(0)
        sentence=answer[:match.start()].split('。')[-1]+fragment
        if any(w in sentence.casefold() for w in ['报告','文档','原文','测试']):continue
        value=Decimal(match.group(1));unit=match.group(2).lower()
        if value not in units.get(unit,set()):issues.append({'code':'report_number_as_measurement','value':str(value)})
    pairs=[p for result in selected for p in result['data'].get('measurements',[])]
    if pairs:
        latest=max(pairs,key=lambda p:(p['measured_at'],p['measurement_id']))
        for match in re.finditer(r'(?:最近一次|最新)[^。；\n]{0,40}?('+NUMBER+r')\s*/\s*('+NUMBER+r')\s*mmHg',answer):
            if (Decimal(match.group(1)),Decimal(match.group(2)))!=(Decimal(str(latest['systolic'])),Decimal(str(latest['diastolic']))):issues.append({'code':'wrong_latest_bp_pair'})
    for indicator,label,unit in [('glucose','血糖','mmol/L'),('weight','体重','kg')]:
        rows=[row for result in selected for row in result['data'].get('records',[]) if row.get('indicator')==indicator]
        if rows:
            latest=max(rows,key=lambda row:(row['measured_at'],row['record_id']))
            for clause in re.split(r'[。；\n]',answer):
                if any(w in clause for w in ['报告','文档','测试文本','引文']):continue
                pattern=r'(?:最近一次|最新)[^。]{0,12}'+label+r'[^\d]{0,10}('+NUMBER+r')\s*'+re.escape(unit)
                for value in re.findall(pattern,clause):
                    if Decimal(value)!=Decimal(str(latest['value'])):issues.append({'code':'wrong_latest_value','indicator':indicator,'value':value})
    for clause in re.split(r'[。；\n]',answer):
        if re.search(r'(?:建议|应该|请|可以)[^。]{0,8}(?:停药|加量|减量|换药|自行调整剂量)',clause) and not any(w in clause for w in ['不能','不应','不要','不建议','不可','不提供','不用于']):
            issues.append({'code':'out_of_scope_medication_instruction'})
    # One comparison series: reject the common before/after swap and opposite direction.
    if len(comparisons)==1:
        comp=comparisons[0]
        for side,label in [('before','前'),('after','后')]:
            pattern=r'(?<!前)(?:调整|用药|换药)?'+label+r'(?:窗)?(?:的)?(?:平均值|平均|均值)[^\d从由]{0,8}('+NUMBER+r')'
            for value in re.findall(pattern,answer):
                expected={Decimal(comp[side]['mean']),Decimal(comp[side]['display']['mean'])}
                if Decimal(value) not in expected:issues.append({'code':'wrong_comparison_side','side':side,'value':value})
        delta=Decimal(comp['delta']) if comp.get('delta') is not None else None
        if delta is not None and ((delta<0 and re.search(r'(?:上升|升高|增加)\s*'+NUMBER+r'\s*[%％]',answer)) or (delta>0 and re.search(r'(?:下降|降低|减少)\s*'+NUMBER+r'\s*[%％]',answer))):issues.append({'code':'wrong_change_direction'})
    charts=[r['data']['chart'] for r in selected if 'chart' in r['data']]
    if charts and all(p['point_count']==1 for chart in charts for p in chart['panels']):
        if re.search(r'(?:呈现|显示|表明|出现|总体|持续)[^。]{0,8}(?:上升|下降|增长|降低)趋势',answer):issues.append({'code':'single_point_trend_claim'})
    if health_rows and not any(r['data'].get('summaries') or r['data'].get('comparisons') for r in selected):
        for sentence in re.split(r'[。；\n]',answer):
            if re.search(r'(?:均值|平均值|平均|最大值|最小值|总和)[^。]{0,12}\d',sentence) and not any(w in sentence for w in ['不能','无法','未计算','未提供']):
                issues.append({'code':'unsupported_derived_statistic'})
    if re.search(r'(?:已生成|已绘制|已画出)[^。]{0,12}(?:图|曲线)',answer) and not charts:
        issues.append({'code':'missing_chart_artifact'})
    if charts and all(p['point_count']==1 for c in charts for p in c['panels']):
        for sentence in re.split(r'[。；\n]',answer):
            if re.search(r'(?:上升|下降|增长|降低)趋势',sentence) and not any(w in sentence for w in ['不能','无法','不足','不支持']):issues.append({'code':'single_point_trend_claim'})
    issues.extend(english_checks(answer,selected+[r for r in results if r['status']=='no_data' and r not in selected],health_rows,comparisons,charts))
    unique=list({tuple(sorted(i.items())):i for i in issues}.values())
    return {'passed':not unique,'issues':unique,'scope':'numeric tokens and units against cited tool facts; selected comparison and single-point rules; not complete natural-language semantic verification'}

def fallback_facts(results):
    facts=[];seen=set()
    for result in results:
        for evidence in result['evidence']:
            if evidence['source_id'] in seen:continue
            seen.add(evidence['source_id']);facts.append(evidence)
    return facts


def english_checks(answer,selected,health_rows,comparisons,charts):
    """Selected English semantic checks complement the shared numeric/provenance checks."""
    lower=answer.casefold();issues=[]
    clauses=re.split(r'[。；;\n]|(?<=[.!?])\s+',lower)
    def denied(text):return bool(re.search(r"\b(?:cannot|can't|unable|insufficient|not|no|never|without)\b",text))
    for clause in clauses:
        if re.search(r'\b(?:recommend|should|please|must|you can)\b.{0,35}\b(?:increase|decrease|stop|switch|adjust|change)\b.{0,25}\b(?:dose|dosage|medication|metformin|amlodipine|atorvastatin)\b',clause) and not denied(clause):issues.append({'code':'out_of_scope_medication_instruction'})
    unknown=any(r.get('indicator')=='glucose' and r.get('measurement_context')=='unknown' for r in health_rows)
    if unknown:
        for clause in clauses:
            nonfasting=bool(re.search(r'\b(?:non[- ]?fasting|not fasting)\b',clause))
            fasting_claim=bool(re.search(r'\bfasting (?:blood )?glucose\b|\b(?:was|is|recorded as|taken while)\s+fasting\b',clause))
            explicit_limit=bool(re.search(r"cannot|can't|unable|not (?:known|determined)|does not (?:mean|imply)|not (?:the same|equivalent)",clause))
            if (nonfasting or fasting_claim) and not explicit_limit:issues.append({'code':'unknown_context_misrepresented'})
    for label,indicator,unit in [('(?:blood )?glucose','glucose','mmol/L'),('weight','weight','kg')]:
        rows=[row for row in health_rows if row.get('indicator')==indicator]
        if rows:
            latest=max(rows,key=lambda row:(row['measured_at'],row['record_id']))
            pattern=r'\b(?:latest|most recent)\s+(?:fasting\s+)?'+label+r'[^\d\n]{0,35}('+NUMBER+r')\s*'+re.escape(unit)
            for value in re.findall(pattern,answer,re.I):
                if Decimal(value)!=Decimal(str(latest['value'])):issues.append({'code':'wrong_latest_value','indicator':indicator,'value':value})
    pairs=[p for result in selected for p in result['data'].get('measurements',[])]
    if pairs and re.search(r'latest|most recent',lower):
        latest=max(pairs,key=lambda p:(p['measured_at'],p['measurement_id']))
        for a,b in re.findall(r'('+NUMBER+r')\s*/\s*('+NUMBER+r')\s*mmHg',answer,re.I):
            if (Decimal(a),Decimal(b))!=(Decimal(str(latest['systolic'])),Decimal(str(latest['diastolic']))):issues.append({'code':'wrong_latest_bp_pair'})
        for label,key in [('systolic','systolic'),('diastolic','diastolic')]:
            for value in re.findall(label+r'[^\d\n]{0,18}('+NUMBER+r')',lower):
                if Decimal(value)!=Decimal(str(latest[key])):issues.append({'code':'wrong_latest_bp_component','component':key,'value':value})
    means={Decimal(str(summary['statistics']['mean'])) for result in selected for summary in result['data'].get('summaries',[]) if summary['statistics'].get('mean') is not None}
    means.update(Decimal(c[side]['mean']) for c in comparisons for side in ('before','after') if c[side].get('mean') is not None)
    for clause in clauses:
        for value in re.findall(r'\b(?:mean|average)(?: (?:blood glucose|glucose|weight|value))?(?: (?:is|was|of))?\s*[:=]?\s*('+NUMBER+r')',clause):
            if means and Decimal(value) not in means:issues.append({'code':'wrong_mean_attribute','value':value})
        if health_rows and not means and re.search(r'\b(?:mean|average|maximum|minimum|sum)\b.{0,25}\d',clause) and not denied(clause):issues.append({'code':'unsupported_derived_statistic'})
    if len(comparisons)==1:
        comp=comparisons[0]
        for side in ('before','after'):
            pattern=r'\b'+side+r'(?: (?:the )?(?:change|adjustment|window|treatment))?[, :]*(?:(?:the )?(?:mean|average)(?: (?:blood )?glucose)?(?: (?:is|was|of))?)[ :]*('+NUMBER+r')'
            for value in re.findall(pattern,lower):
                if Decimal(value) not in {Decimal(comp[side]['mean']),Decimal(comp[side]['display']['mean'])}:issues.append({'code':'wrong_comparison_side','side':side,'value':value})
        for a,b in re.findall(r'\b(?:mean|average)\b.{0,25}\bfrom\s+('+NUMBER+r')(?:\s*mmol/l)?\s+to\s+('+NUMBER+r')',lower):
            if (Decimal(a),Decimal(b))!=(Decimal(comp['before']['mean']),Decimal(comp['after']['mean'])):issues.append({'code':'wrong_comparison_order'})
        delta=Decimal(comp['delta'])
        if (delta<0 and re.search(r'\b(?:increase[ds]?|rose|risen)\b.{0,15}\d+(?:\.\d+)?\s*%',lower)) or (delta>0 and re.search(r'\b(?:decrease[ds]?|fell|drop(?:ped)?)\b.{0,15}\d+(?:\.\d+)?\s*%',lower)):issues.append({'code':'wrong_change_direction'})
    if any(r['status'] in ('insufficient_data','conflicting_events') for r in selected):
        for clause in clauses:
            if re.search(r'\b(?:increase[ds]?|decrease[ds]?|rose|fell|change rate|improved)\b.{0,25}\d|\d+(?:\.\d+)?\s*%',clause) and not denied(clause):issues.append({'code':'business_state_has_no_change_conclusion'})
    if charts and all(p['point_count']==1 for chart in charts for p in chart['panels']):
        for clause in clauses:
            if re.search(r'\b(?:upward|downward|increasing|decreasing|rising|falling)\s+trend\b',clause) and not denied(clause):issues.append({'code':'single_point_trend_claim'})
    if re.search(r'\b(?:chart|graph|plot)\b.{0,18}\b(?:generated|created|drawn|ready)\b',lower) and not charts:issues.append({'code':'missing_chart_artifact'})
    semantic='\n'.join(x.get('text','') for result in selected if result['data'].get('coverage')=='top_k_only' for x in result['data'].get('matches',[]))
    if semantic:
        supported=[(r'real patient data',r'(?i)no real patient data|不包含真实患者数据|没有真实患者数据'),(r'(?:new|specific|concrete|actual|numeric|numerical)\s+(?:measurement\s+)?(?:values|measurements|numbers)',r'不包含(?:新的|具体的)?测量数值'),(r'(?:medication|drug|dosage|treatment).{0,25}(?:effects|effectiveness|outcome)',r'不说明任何用药调整的效果')]
        for clause in clauses:
            if re.search(r'\b(?:report|document)\b.{0,35}\b(?:does not|doesn.t|contains no|has no|did not)\b',clause) and not re.search(r'\b(?:excerpt|passage|chunk|cannot|insufficient)\b',clause):
                if not any(re.search(english,clause) and re.search(source,semantic) for english,source in supported):issues.append({'code':'unsupported_whole_report_absence'})
    # English right-open prose is checked against the same exact query windows.
    allowed=set()
    for result in selected:
        data=result['data'];ranges=([data['range']] if data.get('range') else [])+list(data.get('windows',{}).values())
        for scope in ranges:
            if scope.get('start_date'):allowed.add((scope['start_date'],scope['end_date_exclusive']))
    for clause in clauses:
        if re.search(r'end (?:date )?(?:excluded|exclusive)|right[- ]open',clause):
            days=re.findall(r'\d{4}-\d{2}-\d{2}',clause)
            if len(days)>=2 and tuple(days[-2:]) not in allowed:issues.append({'code':'wrong_exclusive_range','start_date':days[-2],'end_date':days[-1]})
    return issues

