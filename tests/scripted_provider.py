"""Explicit test double only. Never used as a UI or CLI live model fallback."""
from copy import deepcopy
import json

from health_agent.provider import ModelReply


def call(name, arguments, identifier='call_1'):
    arguments = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    item = {'type': 'function_call', 'name': name, 'arguments': arguments, 'call_id': identifier}
    return ModelReply(output_items=[item], calls=[{'name': name, 'arguments': arguments, 'call_id': identifier}],
                      usage={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})


def final(answer='你好，可以帮你整理记录。', citations=None, clarify=False):
    text = json.dumps({'answer': answer, 'citations': citations or [], 'needs_clarification': clarify}, ensure_ascii=False)
    return ModelReply(output_items=[{'type': 'message', 'role': 'assistant',
                                     'content': [{'type': 'output_text', 'text': text}]}], text=text,
                      usage={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})


class ScriptedProvider:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def complete(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        if not self.replies:
            raise AssertionError('Unexpected extra model call in test')
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return deepcopy(item)
