from abc import abstractmethod

from openhands.storage.base import Store


class DBStore(Store):
    @abstractmethod
    def insert(self, table: str, data: dict) -> None:
        pass

    @abstractmethod
    def update(self, table: str, key: dict, data: dict) -> None:
        pass

    @abstractmethod
    def query(self, table: str, filters: dict) -> list[dict]:
        pass

    @abstractmethod
    def delete(self, table: str, key: dict) -> None:
        pass
