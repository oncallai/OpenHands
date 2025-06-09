from typing import Any, Iterable

from openhands.events.event import Event
from openhands.events.stream import EventStream
from events.saas_event_store import SaasEventStore


class SaasEventStream(EventStream):
    """
    An EventStream that uses SaasEventStore for persistent event storage,
    but inherits all async/subscriber/queue logic from EventStream.
    """

    def __init__(
        self, sid: str, file_store, user_id: str | None = None, cache_size: int = 25
    ):
        # Call EventStream constructor (file_store is still needed for interface, but will not be used for persistence)
        super().__init__(sid, file_store, user_id)
        # Compose with DBEventStore for persistence
        self._saas_event_store = SaasEventStore(sid, file_store, user_id, cache_size)
        self.sid = sid
        self.user_id = user_id

    # ---- Persistence overrides ----

    def add_event(self, event: Event, source=None) -> None:
        # Match EventStream signature: (event, source)
        from datetime import datetime

        from openhands.events.event import Event as EventClass

        if event.id != EventClass.INVALID_ID:
            raise ValueError(
                f'Event already has an ID:{event.id}. It was probably added back to the EventStream from inside a handler, triggering a loop.'
            )
        event._timestamp = datetime.now().isoformat()
        if source is not None:
            event._source = source  # type: ignore [attr-defined]
        with self._lock:
            event._id = self._saas_event_store.cur_id  # type: ignore [attr-defined]
            self._saas_event_store.cur_id += 1
        self._saas_event_store.add_event(event)
        self._queue.put(event)

    def get_event(self, event_index: int) -> Event:
        return self._saas_event_store.get_event(event_index)

    def search_events(
        self,
        start_id: int = 0,
        end_id: int | None = None,
        reverse: bool = False,
        filter: Any = None,
        limit: int | None = None,
    ) -> Iterable[Event]:
        return self._saas_event_store.search_events(
            start_id=start_id,
            end_id=end_id,
            reverse=reverse,
            filter=filter,
            limit=limit,
        )

    def delete_event(self, event_index: int) -> None:
        return self._saas_event_store.delete_event(event_index)

    def get_latest_event(self) -> Event:
        return self._saas_event_store.get_latest_event()

    def get_latest_event_id(self) -> int:
        return self._saas_event_store.get_latest_event_id()

    # ---- File/caching-specific method overrides ----
    def _store_cache_page(self, current_write_page: list[dict]):
        # No-op for DB backend
        return

    def _get_filename_for_id(self, event_id, user_id=None):
        # Not used in DB backend
        return None

    def _get_filename_for_cache(self, start, end):
        # Not used in DB backend
        return None
