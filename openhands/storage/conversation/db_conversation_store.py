from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from pydantic import TypeAdapter

from openhands.storage.conversation.conversation_store import ConversationStore
from openhands.storage.data_models.conversation_metadata import ConversationMetadata
from openhands.storage.data_models.conversation_metadata_result_set import ConversationMetadataResultSet
from openhands.storage.postgres_supabase import PostgresSupabaseStore as PostgresStore

conversation_metadata_type_adapter = TypeAdapter(ConversationMetadata)

@dataclass
class DBConversationStore(ConversationStore):
    db_store: PostgresStore

    async def save_metadata(self, metadata: ConversationMetadata) -> None:
        json_str = conversation_metadata_type_adapter.dump_json(metadata)
        await self._run_in_executor(self.db_store.write, metadata.conversation_id, json_str)

    async def get_metadata(self, conversation_id: str) -> ConversationMetadata:
        json_str = await self._run_in_executor(self.db_store.read, conversation_id)
        json_obj = json.loads(json_str)
        if 'created_at' not in json_obj:
            raise FileNotFoundError(conversation_id)
        if 'github_user_id' in json_obj:
            json_obj.pop('github_user_id')
        result = conversation_metadata_type_adapter.validate_python(json_obj)
        return result

    async def delete_metadata(self, conversation_id: str) -> None:
        await self._run_in_executor(self.db_store.delete, conversation_id)

    async def exists(self, conversation_id: str) -> bool:
        try:
            await self.get_metadata(conversation_id)
            return True
        except FileNotFoundError:
            return False

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
