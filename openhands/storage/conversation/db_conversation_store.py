from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from pydantic import TypeAdapter

from openhands.storage.conversation.conversation_store import ConversationStore
from openhands.storage.data_models.conversation_metadata import ConversationMetadata
from openhands.storage.data_models.conversation_metadata_result_set import ConversationMetadataResultSet
from openhands.storage.postgres import PostgresStore

conversation_metadata_type_adapter = TypeAdapter(ConversationMetadata)

import psycopg2

class DBConversationStore(ConversationStore):
    def __init__(self, db_store: PostgresStore):
        self.db_store = db_store

    @classmethod
    async def get_instance(cls, config, user_id: str | None):
        db_store = PostgresStore(table='openhands_sessions')
        return cls(db_store)

    async def save_metadata(self, metadata: ConversationMetadata) -> None:
        # Upsert metadata in openhands_sessions
        import psycopg2.extras
        session_id = metadata.conversation_id
        # Convert metadata to dict for psycopg2 JSON adaptation
        import dataclasses
        import enum, datetime
        def to_jsonable(obj):
            if isinstance(obj, enum.Enum):
                return obj.value
            elif isinstance(obj, datetime.datetime):
                return obj.isoformat()
            elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                return {k: to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
            elif hasattr(obj, "model_dump"):
                return obj.model_dump(mode="json")
            elif hasattr(obj, "dict"):
                return {k: to_jsonable(v) for k, v in obj.dict().items()}
            elif isinstance(obj, (list, tuple)):
                return [to_jsonable(x) for x in obj]
            elif isinstance(obj, dict):
                return {k: to_jsonable(v) for k, v in obj.items()}
            else:
                return obj
        metadata_dict = to_jsonable(metadata)
        query = '''
            INSERT INTO openhands_sessions (id, metadata, created_at, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET metadata = EXCLUDED.metadata, updated_at = CURRENT_TIMESTAMP
        '''
        def _write():
            with self.db_store.conn.cursor() as cur:
                cur.execute(query, (session_id, psycopg2.extras.Json(metadata_dict)))
                self.db_store.conn.commit()
        await self._run_in_executor(_write)

    async def get_metadata(self, conversation_id: str) -> ConversationMetadata:
        query = 'SELECT metadata FROM openhands_sessions WHERE id = %s'
        def _read():
            with self.db_store.conn.cursor() as cur:
                cur.execute(query, (conversation_id,))
                row = cur.fetchone()
                if not row:
                    raise FileNotFoundError(conversation_id)
                json_obj = row[0]
                if 'created_at' not in json_obj:
                    raise FileNotFoundError(conversation_id)
                if 'github_user_id' in json_obj:
                    json_obj.pop('github_user_id')
                return conversation_metadata_type_adapter.validate_python(json_obj)
        return await self._run_in_executor(_read)

    async def delete_metadata(self, conversation_id: str) -> None:
        query = 'DELETE FROM openhands_sessions WHERE id = %s'
        def _delete():
            with self.db_store.conn.cursor() as cur:
                cur.execute(query, (conversation_id,))
                self.db_store.conn.commit()
        await self._run_in_executor(_delete)

    async def exists(self, conversation_id: str) -> bool:
        query = 'SELECT 1 FROM openhands_sessions WHERE id = %s'
        def _exists():
            with self.db_store.conn.cursor() as cur:
                cur.execute(query, (conversation_id,))
                return cur.fetchone() is not None
        return await self._run_in_executor(_exists)

    async def search(self, page_id: str | None = None, limit: int = 20) -> ConversationMetadataResultSet:
        # Simple implementation: list all, filter by query in JSON, paginate
        all_ids = await self._run_in_executor(self.db_store.list, "")
        offset = int(page_id or 0)
        page = []
        for cid in all_ids[offset:offset+limit]:
            try:
                meta = await self.get_metadata(cid)
                page.append(meta)
            except Exception:
                continue
        next_page_id = str(offset + limit) if (offset + limit) < len(all_ids) else None
        # Assuming ConversationMetadataResultSet takes items, next_page_id as positional args
        return ConversationMetadataResultSet(page, next_page_id)

    async def _run_in_executor(self, fn, *args, **kwargs):
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
