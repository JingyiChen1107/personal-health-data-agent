"""Build a new database atomically from versioned, deterministic fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from health_agent.config import PROJECT_ROOT, load_config
from health_agent.db import APPLICATION_ID, connect

TABLES = ('patients', 'health_records', 'medications', 'medication_events', 'reports')


def initialize_database(path: Path, *, reset: bool = False) -> dict:
    path = Path(path).resolve()
    if path.exists():
        if not reset:
            raise FileExistsError('Database already exists. Use --reset to rebuild synthetic data.')
        with connect(path, readonly=True) as con:
            if con.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
                raise ValueError('Refusing to replace a database not owned by this project.')
    cfg = load_config()
    fixture = json.loads((PROJECT_ROOT / 'sample_data' / 'seed.json').read_text(encoding='utf-8'))
    for report in fixture['reports']:
        source = (PROJECT_ROOT / report['file_path']).resolve()
        if not source.is_relative_to((PROJECT_ROOT / 'sample_data' / 'reports').resolve()):
            raise ValueError('Report path is outside sample_data/reports')
        content = source.read_bytes()
        content.decode('utf-8')
        report['content_hash'] = hashlib.sha256(content).hexdigest()
        report['index_status'] = 'pending'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix='.seed-', suffix='.sqlite3', dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with connect(temporary) as con:
            con.executescript((Path(__file__).parent / 'schema.sql').read_text(encoding='utf-8'))
            with con:
                for table in TABLES:
                    rows = fixture[table]
                    if not rows:
                        raise ValueError(f'Empty required fixture table: {table}')
                    keys = tuple(rows[0])
                    if any(tuple(row) != keys for row in rows):
                        raise ValueError(f'Inconsistent fixture columns: {table}')
                    columns = ','.join(keys)
                    placeholders = ','.join('?' for _ in keys)
                    con.executemany(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})',
                                    [tuple(row[key] for key in keys) for row in rows])
                con.executemany('INSERT INTO dataset_metadata VALUES (?, ?)', [
                    ('dataset_version', fixture['dataset_version']),
                    ('reference_date', cfg.reference_date.isoformat()),
                    ('timezone', str(cfg.timezone)),
                    ('synthetic_only', 'true'),
                ])
            if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('SQLite integrity check failed')
            if con.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Foreign key check failed')
        from health_agent.validation import validate_database
        summary = validate_database(temporary)
        if not summary['ok']:
            raise ValueError('Seed validation failed: ' + '; '.join(summary['errors']))
        os.replace(temporary, path)
        return summary
    finally:
        temporary.unlink(missing_ok=True)
