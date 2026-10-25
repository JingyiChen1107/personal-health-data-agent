"""Descriptive calculations. Decimal strings preserve precision in JSON."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext


def display(value: str | Decimal, places: int = 2) -> str:
    result = Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if not result:
        result = abs(result)
    return format(result, f'.{places}f')


def summarize(records: list[dict]) -> dict:
    if not records:
        return {'count': 0, 'sum': None, 'mean': None, 'min': None, 'max': None, 'record_ids': [], 'display': None}
    with localcontext() as ctx:
        ctx.prec = 28
        values = [Decimal(str(row['value'])) for row in records]
        total = sum(values, Decimal(0))
        result = {'count': len(values), 'sum': str(total), 'mean': str(total / len(values)),
                  'min': str(min(values)), 'max': str(max(values)),
                  'record_ids': [row['record_id'] for row in records]}
        result['display'] = {key: display(result[key]) for key in ('sum', 'mean', 'min', 'max')}
        return result


def difference(before: dict, after: dict) -> dict:
    with localcontext() as ctx:
        ctx.prec = 28
        left, right = Decimal(before['mean']), Decimal(after['mean'])
        delta = right - left
        percent = delta / left * 100 if left else None
        return {'delta': str(delta), 'percent_change': str(percent) if percent is not None else None,
                'display': {'delta': display(delta), 'percent_change': display(percent) if percent is not None else None}}
