from typing import Any, Iterable

from openhands.events.event import Event
from openhands.events.event_store_abc import EventStoreABC
from openhands.events.serialization.event import event_from_dict, event_to_dict
from openhands.storage.db import DBStore


class SaasEventStore(EventStoreABC):
    """
    Event store backed by a database event store (e.g., PostgresStore).
    Accepts any Store that implements the event methods (write_event, read_event, list_events, delete_event).
    """

    def __init__(
        self,
        sid: str,
        db_store: DBStore,
        user_id: str | None = None,
        cache_size: int = 25,
    ):
        self.sid = sid
        self.db_store = db_store
        self.user_id = user_id
        self.cache_size = cache_size
        self.cur_id = self._init_cur_id()

    def _init_cur_id(self) -> int:
        try:
            indices = self.db_store.list_events(self.sid)
            if not indices:
                return 0
            return max(int(idx) for idx in indices) + 1
        except Exception:
            return 0  # If DBStore does not have events, start at 0

    def add_event(self, event: Event) -> int:
        event_index = self.cur_id
        event_data = event_to_dict(event)
        self.db_store.write_event(self.sid, event_index, event_data)
        self.cur_id += 1
        return event_index

    def get_event(self, event_index: int) -> Event:
        data = self.db_store.read_event(self.sid, event_index)
        return event_from_dict(data)

    def search_events(
        self,
        start_id: int = 0,
        end_id: int | None = None,
        reverse: bool = False,
        filter: Any = None,
        limit: int | None = None,
    ) -> Iterable[Event]:
        """
        Searches for events in the database event store.

        Args:
            start_id (int): The starting index of the search range.
            end_id (int | None): The ending index of the search range.
            reverse (bool): Whether to search in reverse order.
            filter (Any): A filter to apply to the search results.
            limit (int | None): The maximum number of results to return.

        Yields:
            Event: The events that match the search criteria.
        """
        indices = self.db_store.list_events(self.sid)
        indices = [int(idx) for idx in indices]
        if reverse:
            indices = sorted(indices, reverse=True)
        else:
            indices = sorted(indices)
        if end_id is not None:
            indices = [i for i in indices if start_id <= i <= end_id]
        else:
            indices = [i for i in indices if i >= start_id]
        if limit is not None:
            indices = indices[:limit]
        for idx in indices:
            event = self.get_event(idx)
            if filter is None or filter.include(event):
                yield event

    def delete_event(self, event_index: int) -> None:
        """
        Deletes an event from the database event store.

        Args:
            event_index (int): The index of the event to delete.
        """
        self.db_store.delete_event(self.sid, event_index)

    def get_latest_event(self) -> Event:
        indices = self.db_store.list_events(self.sid)
        if not indices:
            raise FileNotFoundError(f'No events for session {self.sid}')
        latest_index = max(int(idx) for idx in indices)
        return self.get_event(latest_index)

    def get_latest_event_id(self) -> int:
        indices = self.db_store.list_events(self.sid)
        if not indices:
            return -1
        return max(int(idx) for idx in indices)
