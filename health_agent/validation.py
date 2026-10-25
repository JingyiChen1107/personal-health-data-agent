"""Day 1 data checks, independent of an LLM, vector store or agent framework."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from health_agent.config import PROJECT_ROOT, load_config
from health_agent.db import APPLICATION_ID, connect


def validate_database(path: Path) -> dict:
    cfg = load_config()
    expected = json.loads((PROJECT_ROOT / 'tests' / 'fixtures' / 'expected.json').read_text(encoding='utf-8'))
    errors = []
    counts = {}
    with connect(path, readonly=True) as con:
        if con.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
            errors.append('Unexpected application_id')
        if con.execute('PRAGMA user_version').fetchone()[0] != 1:
            errors.append('Unexpected schema version')
        if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            errors.append('SQLite integrity check failed')
        if con.execute('PRAGMA foreign_key_check').fetchall():
            errors.append('Foreign key check failed')
        for table, count in expected['counts'].items():
            counts[table] = con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            if (table == 'reports' and not count <= counts[table] <= 5) or (table != 'reports' and counts[table] != count):
                errors.append(f'{table}: expected {count}, found {counts[table]}')
        metadata = dict(con.execute('SELECT key, value FROM dataset_metadata').fetchall())
        if metadata.get('reference_date') != cfg.reference_date.isoformat() or metadata.get('timezone') != str(cfg.timezone):
            errors.append('Database/config date or timezone mismatch; rebuild the demo database')
        if metadata.get('synthetic_only') != 'true':
            errors.append('Synthetic-only marker missing')
        for row in con.execute('SELECT * FROM health_records'):
            spec = cfg.indicators.get(row['indicator'])
            if not spec or row['unit'] != spec['unit'] or row['measurement_context'] not in spec['contexts']:
                errors.append(f'Invalid metric semantics: {row["record_id"]}')
            instant = datetime.strptime(row['measured_at'], '%Y-%m-%dT%H:%M:%SZ')
            if instant.strftime('%Y-%m-%dT%H:%M:%SZ') != row['measured_at']:
                errors.append(f'Noncanonical timestamp: {row["record_id"]}')
        pairs = con.execute('''SELECT measurement_id FROM health_records WHERE measurement_id IS NOT NULL
            GROUP BY patient_id, measurement_id HAVING count(*) <> 2 OR count(DISTINCT indicator) <> 2
            OR count(DISTINCT measured_at) <> 1 OR count(DISTINCT measurement_context) <> 1''').fetchall()
        if pairs:
            errors.append('Incomplete or inconsistent blood-pressure measurement pairs')
        duplicates = con.execute('''SELECT indicator FROM health_records
            GROUP BY patient_id, indicator, measured_at, value, unit, measurement_context HAVING count(*) > 1''').fetchall()
        if duplicates:
            errors.append('Duplicate measurements must be cleaned before demo')
        for row in con.execute('SELECT * FROM medication_events'):
            day = date.fromisoformat(row['event_date'])
            for field, boundary in [('old_medication_id', 'end_date'), ('new_medication_id', 'start_date')]:
                if row[field]:
                    med = con.execute('SELECT * FROM medications WHERE medication_id = ?', (row[field],)).fetchone()
                    if med[boundary] != day.isoformat():
                        errors.append(f'Event/medication boundary mismatch: {row["event_id"]}')
        for row in con.execute('SELECT * FROM reports'):
            source = (PROJECT_ROOT / row['file_path']).resolve()
            if not source.is_relative_to((PROJECT_ROOT / 'sample_data' / 'reports').resolve()):
                errors.append(f'Unsafe report path: {row["report_id"]}')
                continue
            if not source.is_file():
                errors.append(f'Missing report: {row["report_id"]}')
                continue
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != row['content_hash']:
                errors.append(f'Report hash mismatch: {row["report_id"]}')
            if row['index_status'] not in ('pending','ready','failed'):
                errors.append(f'Invalid index status: {row["report_id"]}')
    return {'ok': not errors, 'counts': counts, 'reference_date': cfg.reference_date.isoformat(),
            'timezone': str(cfg.timezone), 'errors': errors}
