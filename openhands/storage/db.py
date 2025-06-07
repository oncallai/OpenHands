from abc import abstractmethod

from openhands.storage.store import Store


class DBStore(Store):
    @abstractmethod
    def write(self, path: str, contents: str | bytes) -> None:
        pass

    @abstractmethod
    def read(self, path: str) -> str:
        pass

    @abstractmethod
    def list(self, path: str) -> list[str]:
        pass

    @abstractmethod
    def delete(self, path: str) -> None:
        pass

    # Event-specific methods
    @abstractmethod
    def write_event(self, session_id: str, event_index: int, event_data) -> None:
        pass

    @abstractmethod
    def read_event(self, session_id: str, event_index: int):
        pass

    @abstractmethod
    def list_events(self, session_id: str):
        pass

    @abstractmethod
    def delete_event(self, session_id: str, event_index: int) -> None:
        pass
