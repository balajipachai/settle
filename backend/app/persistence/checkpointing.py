"""LangGraph checkpointer with an explicit allow-list of state types."""

from __future__ import annotations

import enum
import inspect
import sqlite3
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel

from app.domain import errors, models, states


def _allowed_types() -> list[tuple[str, str]]:
    allowed = []
    for module in (models, errors, states):
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ == module.__name__ and issubclass(obj, BaseModel | enum.Enum):
                allowed.append((obj.__module__, obj.__name__))
    return allowed


def make_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=_allowed_types())


def make_checkpointer(target: Path | str) -> BaseCheckpointSaver:
    """SQLite file path (default) or a postgresql:// URL (requires langgraph-checkpoint-postgres)."""
    if isinstance(target, str) and target.startswith("postgres"):
        from langgraph.checkpoint.postgres import PostgresSaver  # optional dependency
        from psycopg import Connection
        from psycopg.rows import dict_row

        conn = Connection.connect(target, autocommit=True, prepare_threshold=0, row_factory=dict_row)
        saver = PostgresSaver(conn, serde=make_serializer())
        saver.setup()
        return saver
    conn = sqlite3.connect(str(target), check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return SqliteSaver(conn, serde=make_serializer())
