"""SQLite connections always enforce referential integrity."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

APPLICATION_ID = 1212633428


@contextmanager
def connect(path: Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    path = Path(path).resolve()
    if readonly:
        con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    else:
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    try:
        yield con
    finally:
        con.close()
