"""
Improved abstract base class for database storage operations.
This module defines the interface that all database store implementations must follow.
"""

import builtins
from abc import ABC, abstractmethod
from typing import Any, Union

from openhands.storage.store import Store


class DBStore(Store, ABC):
    """
    Abstract base class for database storage operations.

    This class extends the basic Store interface with additional methods
    specifically designed for database operations, including event storage
    and retrieval capabilities.
    """

    # Basic file-like operations inherited from Store
    @abstractmethod
    def write(self, path: str, contents: Union[str, bytes]) -> None:
        """
        Write content to a specific path in the database.

        Args:
            path: Unique identifier/path for the content
            contents: Data to store (string or bytes)

        Raises:
            DatabaseError: If write operation fails
        """
        pass

    @abstractmethod
    def read(self, path: str) -> str:
        """
        Read content from a specific path in the database.

        Args:
            path: Unique identifier/path for the content

        Returns:
            str: The stored content

        Raises:
            FileNotFoundError: If path doesn't exist
            DatabaseError: If read operation fails
        """
        pass

    @abstractmethod
    def list(self, path: str) -> list[str]:
        """
        List all paths that start with the given prefix.

        Args:
            path: Path prefix to search for

        Returns:
            List[str]: List of matching paths

        Raises:
            DatabaseError: If list operation fails
        """
        pass

    @abstractmethod
    def delete(self, path: str) -> None:
        """
        Delete content at a specific path.

        Args:
            path: Unique identifier/path to delete

        Raises:
            FileNotFoundError: If path doesn't exist
            DatabaseError: If delete operation fails
        """
        pass

    # Event-specific methods for structured event storage
    @abstractmethod
    def write_event(
        self, session_id: str, event_index: int, event_data: dict[str, Any]
    ) -> None:
        """
        Store an event for a specific session with an index.

        Events are stored in a structured way with session_id and event_index
        forming a composite key. This allows for ordered event retrieval.

        Args:
            session_id: Unique identifier for the session
            event_index: Sequential index of the event within the session
            event_data: Dictionary containing the event information

        Raises:
            DatabaseError: If write operation fails
        """
        pass

    @abstractmethod
    def read_event(self, session_id: str, event_index: int) -> dict[str, Any]:
        """
        Retrieve a specific event from a session.

        Args:
            session_id: Unique identifier for the session
            event_index: Sequential index of the event to retrieve

        Returns:
            Dict[str, Any]: The event data

        Raises:
            FileNotFoundError: If the event doesn't exist
            DatabaseError: If read operation fails
        """
        pass

    @abstractmethod
    def list_events(self, session_id: str) -> builtins.list[int]:
        """
        List all event indices for a given session in ascending order.

        Args:
            session_id: Unique identifier for the session

        Returns:
            List[int]: Sorted list of event indices

        Raises:
            DatabaseError: If list operation fails
        """
        pass

    @abstractmethod
    def delete_event(self, session_id: str, event_index: int) -> None:
        """
        Delete a specific event from a session.

        Args:
            session_id: Unique identifier for the session
            event_index: Sequential index of the event to delete

        Raises:
            FileNotFoundError: If the event doesn't exist
            DatabaseError: If delete operation fails
        """
        pass

    # Optional health check method that implementations can override
    def health_check(self) -> bool:
        """
        Check if the database connection is healthy.

        This is an optional method that implementations can override
        to provide health checking capabilities.

        Returns:
            bool: True if healthy, False otherwise
        """
        return True

    # Optional cleanup method for resource management
    def close(self) -> None:
        """
        Clean up any resources used by the store.

        This method should be called when the store is no longer needed
        to ensure proper cleanup of connections, pools, etc.
        """
        pass
