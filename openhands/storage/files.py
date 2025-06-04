from abc import abstractmethod


class Store:
    """Abstract base store for OpenHands storage backends."""

    pass


class FileStore(Store):
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
