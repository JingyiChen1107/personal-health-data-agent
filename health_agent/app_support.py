"""Small application factory shared by UI, CLI and tests."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from health_agent.config import DEFAULT_DATABASE, load_config
from health_agent.model_config import ModelSettings, model_readiness
from health_agent.results import ToolContext


def configuration_status():
    return model_readiness()


def create_session_agent(*, session_id: str | None = None, database: Path = DEFAULT_DATABASE, event_sink=None):
    from health_agent.conversation import ConversationAgent
    from health_agent.provider import OpenAIProvider
    from health_agent.tools import HealthTools
    cfg = load_config()
    settings = ModelSettings.load()
    context = ToolContext(cfg.patient_id, session_id or uuid4().hex, cfg.reference_date)
    from health_agent.cache import SQLiteResultCache
    cache = SQLiteResultCache(Path(database).parent/'result_cache.sqlite3',namespace=str(Path(database).resolve()))
    tools = HealthTools(database, context=context, config=cfg, cache=cache)
    if settings.provider == 'ollama':
        from health_agent.ollama_provider import OllamaProvider
        provider = OllamaProvider(settings)
    else:
        provider = OpenAIProvider(settings, timeout_seconds=cfg.request_timeout_seconds)
    return ConversationAgent(tools, provider, event_sink=event_sink)
