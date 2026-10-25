"""Two explicitly live smoke checks. No scripted-model fallback is provided."""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

from health_agent.config import PROJECT_ROOT

SMOKE_QUESTIONS = {
    'T01': '我最近一次血糖是多少？请说明测量时间、单位与测量条件。',
    'T10': '比较2026年10月1日二甲双胍调整前后各14天的空腹血糖。请给出样本数、前后均值和百分比变化，并说明比较局限。',
}


def grade_smoke(case_id: str, result: dict) -> dict:
    checks = {'agent_completed': result.get('status') == 'ok'}
    traces = result.get('trace', [])
    numbers = {Decimal(value) for value in re.findall(r'(?<![A-Za-z])\d+(?:\.\d+)?', result.get('answer', ''))}
    if case_id == 'T01':
        candidates = [r for r in result.get('tool_results', []) if any(e['source_id'] == 'G032' for e in r['evidence'])]
        checks.update({
            'health_tool_used': any(t['tool'] == 'query_health_data' for t in traces),
            'correct_latest_record': any((r.get('data', {}).get('records') or [{}])[0].get('record_id') == 'G032' for r in candidates),
            'correct_value_in_answer': Decimal('6.9') in numbers and 'mmol/L' in result.get('answer', ''),
            'correct_source_cited': 'G032' in result.get('citations', []),
        })
    elif case_id == 'T10':
        names = [t['tool'] for t in traces]
        ordered = 'query_medications' in names and 'query_health_data' in names and names.index('query_medications') < names.index('query_health_data')
        comparisons = [item for r in result.get('tool_results', [])
                       if r.get('data', {}).get('event', {}).get('event_id') == 'E007'
                       for item in r['data'].get('comparisons', [])]
        checks.update({
            'dependency_order': ordered,
            'golden_comparison': any(item['before']['count'] == 8 and item['after']['count'] == 8
                                     and item['before']['mean'] == '7.95' and Decimal(item['after']['mean']) == 7
                                     and (item.get('display') or {}).get('percent_change') == '-11.95'
                                     for item in comparisons),
            'computed_values_in_answer': {Decimal('7.95'), Decimal('7'), Decimal('11.95')} <= numbers,
            'event_cited': 'E007' in result.get('citations', []),
            'health_evidence_cited': bool(set(result.get('citations', [])) & {f'G{i:03}' for i in range(1, 17)}),
        })
    else:
        raise ValueError('Unsupported smoke case')
    return {'case_id': case_id, 'passed': all(checks.values()), 'checks': checks}


def live_smoke(*, output_dir: Path | None = None) -> dict:
    from health_agent.app_support import create_session_agent
    from health_agent.model_config import ModelSettings
    settings = ModelSettings.load()
    output_dir = output_dir or PROJECT_ROOT / 'artifacts' / 'day3' / 'live'
    reports = []
    for case_id, question in SMOKE_QUESTIONS.items():
        # New application context for each independent live scenario.
        result = create_session_agent().run(question)
        report = grade_smoke(case_id, result)
        report['result'] = result
        reports.append(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = {'mode': 'live_' + settings.provider, 'model': settings.model,
              'passed': all(r['passed'] for r in reports), 'cases': reports}
    (output_dir / 'smoke_results.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return output
