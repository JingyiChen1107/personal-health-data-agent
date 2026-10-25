"""Provider-facing strict schemas; application identity is never model-controlled."""
from __future__ import annotations

from copy import deepcopy


def nullable_text(description):
    return {'type': ['string', 'null'], 'description': description}


HEALTH_PROPERTIES = {
    'indicator': {'type': 'string', 'enum': ['glucose', 'weight', 'systolic_blood_pressure', 'diastolic_blood_pressure', 'blood_pressure']},
    'mode': {'type': 'string', 'enum': ['latest', 'series', 'summary', 'compare_event']},
    'start_date': nullable_text('YYYY-MM-DD，左闭。series或summary必须提供；latest可空；compare_event必须为null。'),
    'end_date': nullable_text('YYYY-MM-DD，右开；用户说到某日时加一天。默认参考日次日；compare_event必须为null。'),
    'measurement_context': {'type': ['string', 'null'], 'enum': ['fasting', 'postprandial', 'random', 'unknown', None],
                            'description': '血糖条件；未明确不能猜为空腹。其他指标为null。'},
    'event_id': nullable_text('compare_event必须使用本轮query_medications events已返回的事件ID，其他模式为null。'),
    'window_days': {'type': ['integer', 'null'], 'description': 'compare_event窗口，1至90天，默认14；其他模式为null。'},
    'requested_unit': nullable_text('仅用户明确要求单位时填写，否则null。首版拒绝非标准单位换算。'),
}
MEDICATION_PROPERTIES = {
    'mode': {'type': 'string', 'enum': ['current', 'history', 'events']},
    'medication_name': nullable_text('已知药名精确匹配；未指定为null。'),
    'start_date': nullable_text('YYYY-MM-DD，左闭；current必须为null。'),
    'end_date': nullable_text('YYYY-MM-DD，右开；current必须为null。'),
    'event_id': nullable_text('仅events可用；未指定为null。'),
    'event_type': {'type': ['string', 'null'], 'enum': ['start', 'stop', 'switch', 'dose_change', None],
                   'description': '仅events可用；调整剂量可筛选dose_change，其他模式为null。'},
}


def tool_specs() -> list[dict]:
    result = []
    for name, properties, description in [
        ('query_health_data', HEALTH_PROPERTIES,
         '精确查询个人健康指标、时间序列及描述性统计。用药前后比较须先通过query_medications确认事件，再单独调用compare_event；不要猜测事件。'),
        ('query_medications', MEDICATION_PROPERTIES,
         '查询当前用药、历史区间或显式用药事件。换药或调药比较先调用events；多个候选且用户未指明时应追问，不自动选最新。'),
    ]:
        result.append({'type': 'function', 'name': name, 'description': description, 'strict': True,
                       'parameters': {'type': 'object', 'properties': deepcopy(properties),
                                      'required': list(properties), 'additionalProperties': False}})
    properties = {
        'query': {'type':'string','description':'检索主题或术语。mention_check使用LDL等术语，不能把整句问题作为术语。'},
        'mode': {'type':'string','enum':['semantic','mention_check'],'description':'报告问答用semantic；全文是否提及使用mention_check，扫描全部范围报告及配置别名。'},
        'report_id': nullable_text('指定报告R001等，未指定为null。'),
        'start_date': nullable_text('报告日期左闭YYYY-MM-DD；去年按参考日期解析为上一自然年。'),
        'end_date': nullable_text('报告日期右开YYYY-MM-DD；不是上传日期。'),
        'top_k': {'type':'integer','minimum':1,'maximum':5,'description':'semantic返回片段数，默认4。'}
    }
    result.append({'type':'function','name':'search_health_documents','strict':True,
        'description':'检索本用户本地TXT报告，提供原文与行号。报告中的数值仅为报告文本，不等于数据库最新指标。是否提及走mention_check；semantic没有相关片段不能推断全文不存在。',
        'parameters':{'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}})
    properties={'result_id':{'type':'string','description':'此前query_health_data返回的真实result_id；不能填记录ID、自己编造ID或提供数据数组。'},
                'chart_type':{'type':'string','enum':['line','scatter'],'description':'默认line；单点自动仅标记，不形成趋势线。'}}
    result.append({'type':'function','name':'plot_health_trend','strict':True,
        'description':'从当前会话有效的健康查询缓存绘制原始记录。先查询series，再以真实result_id单独调用此工具；按测量条件分面，保留原始日期和值。',
        'parameters':{'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}})
    return result


FINAL_SCHEMA = {'type': 'json_schema', 'name': 'health_agent_answer', 'strict': True,
                'schema': {'type': 'object', 'properties': {
                    'answer': {'type': 'string'},
                    'citations': {'type': 'array', 'items': {'type': 'string'}},
                    'needs_clarification': {'type': 'boolean'},
                }, 'required': ['answer', 'citations', 'needs_clarification'], 'additionalProperties': False}}
