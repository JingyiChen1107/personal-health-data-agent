"""Load explicit local model settings without logging or executing secret values."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from health_agent.config import PROJECT_ROOT


class ModelConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelSettings:
    model: str
    api_key: str = field(repr=False)
    provider: str = 'openai'
    max_output_tokens: int = 4096
    base_url: str = 'http://127.0.0.1:11435'
    timeout_seconds: int = 180

    @classmethod
    def load(cls, *, env: Mapping[str, str] | None = None, dotenv_path: Path | None = None):
        values = {} if env is not None else dict(os.environ)
        path = dotenv_path if dotenv_path is not None else PROJECT_ROOT / '.env'
        if path.is_file():
            from dotenv import dotenv_values
            values = {key: value for key, value in dotenv_values(path, interpolate=False).items() if value is not None} | values
        if env is not None:
            values |= dict(env)
        provider = values.get('LLM_PROVIDER', 'openai').strip().lower()
        if provider not in ('openai', 'ollama'):
            raise ModelConfigError('LLM_PROVIDER仅支持openai或ollama。')
        model = values.get('LLM_MODEL', '').strip()
        key = (values.get('LLM_API_KEY') or values.get('OPENAI_API_KEY') or '').strip()
        if provider == 'ollama':
            key = ''  # Never forward cloud credentials to the local service.
        missing = [name for name, value in [('LLM_MODEL', model)] if not value]
        if provider == 'openai' and not key:
            missing.insert(0, 'LLM_API_KEY')
        if missing:
            raise ModelConfigError('请在项目.env中配置：' + '、'.join(missing) + '。')
        try:
            tokens = int(values.get('LLM_MAX_OUTPUT_TOKENS', '4096'))
        except (ValueError, TypeError):
            raise ModelConfigError('LLM_MAX_OUTPUT_TOKENS必须为512至8192的整数。') from None
        if not 512 <= tokens <= 8192:
            raise ModelConfigError('LLM_MAX_OUTPUT_TOKENS必须为512至8192的整数。')
        if len(model) > 200 or any(c.isspace() for c in model):
            raise ModelConfigError('LLM_MODEL必须是有效的模型标识。')
        base_url = values.get('OLLAMA_BASE_URL', 'http://127.0.0.1:11435').rstrip('/')
        timeout = 180
        if provider == 'ollama':
            url = urlsplit(base_url)
            if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
                    or url.username or url.password or url.path or url.query or url.fragment):
                raise ModelConfigError('OLLAMA_BASE_URL必须是本机HTTP地址，不含路径或认证信息。')
            if model.endswith('-cloud') or '/' in model:
                raise ModelConfigError('本地模式仅允许已下载的本机模型名称。')
            try:
                timeout = int(values.get('OLLAMA_TIMEOUT_SECONDS', '180'))
                if not 10 <= timeout <= 600:
                    raise ValueError
            except (ValueError, TypeError):
                raise ModelConfigError('OLLAMA_TIMEOUT_SECONDS必须为10至600的整数。') from None
        return cls(model=model, api_key=key, provider=provider, max_output_tokens=tokens,
                   base_url=base_url, timeout_seconds=timeout)


def model_readiness(*, env=None, dotenv_path=None) -> dict:
    try:
        settings = ModelSettings.load(env=env, dotenv_path=dotenv_path)
        return {'configured': True, 'provider': settings.provider, 'model': settings.model, 'message': '模型配置已读取；尚不代表API连通性验证通过。'}
    except ModelConfigError as exc:
        return {'configured': False, 'provider': 'openai', 'model': None, 'message': str(exc)}
