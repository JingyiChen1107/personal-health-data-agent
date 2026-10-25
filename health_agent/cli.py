"""Dataset utilities, deterministic tools and the Day 3 model-backed agent."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from health_agent.config import DEFAULT_DATABASE, load_config
from health_agent.results import ToolContext
from health_agent.seed import initialize_database
from health_agent.tools import HealthTools
from health_agent.validation import validate_database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Synthetic health-agent dataset, tools and chat (Day 4)')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'validate'):
        command = commands.add_parser(name)
        command.add_argument('--db', type=Path, default=DEFAULT_DATABASE)
        if name == 'init':
            command.add_argument('--reset', action='store_true', help='Replace this project\'s synthetic database')
    for name in ('health', 'medications'):
        command = commands.add_parser(name, help='Execute a deterministic tool with JSON arguments')
        command.add_argument('--db', type=Path, default=DEFAULT_DATABASE)
        command.add_argument('--args', required=True, help='JSON object; end_date is exclusive')
    commands.add_parser('index-reports', help='Build local report embeddings and index')
    commands.add_parser('model-status', help='Check configuration without printing keys or making requests')
    commands.add_parser('live-smoke', help='Call the configured real model for T01 and T10; writes artifacts/day3/live')
    chat = commands.add_parser('chat', help='Run a real model-backed single-agent turn')
    chat.add_argument('--question', required=True)
    chat.add_argument('--session-id', help='Reuse this application-bound session to resume a paused task')
    chat.add_argument('--db', type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args(argv)
    try:
        if args.command == 'index-reports':
            from health_agent.rag import build_index
            result = build_index(DEFAULT_DATABASE)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if result['ok'] else 1
        if args.command == 'model-status':
            from health_agent.app_support import configuration_status
            result = configuration_status()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['configured'] else 1
        if args.command == 'live-smoke':
            from health_agent.evaluation import live_smoke
            result = live_smoke()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['passed'] else 1
        if args.command == 'chat':
            from health_agent.app_support import create_session_agent
            agent = create_session_agent(database=args.db, session_id=args.session_id)
            try:
                result = agent.run(args.question)
            finally:
                agent.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result['status'] == 'error' else 0
        if args.command == 'init':
            result = initialize_database(args.db, reset=args.reset)
        elif args.command == 'validate':
            result = validate_database(args.db)
        else:
            arguments = json.loads(args.args)
            cfg = load_config()
            context = ToolContext(cfg.patient_id, 'cli_' + uuid4().hex, cfg.reference_date)
            tools = HealthTools(args.db, context=context, config=cfg)
            result = (tools.query_health_data(arguments) if args.command == 'health'
                      else tools.query_medications(arguments))
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command in ('health', 'medications'):
        return 1 if result['status'] == 'error' else 0
    return 0 if result['ok'] else 1
