"""
Improved database-backed conversation store implementation.
This module provides a robust conversation storage system with proper error handling,
async execution, and comprehensive logging.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import enum
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

import psycopg2
from pydantic import TypeAdapter

from openhands.storage.conversation.conversation_store import ConversationStore
from openhands.storage.data_models.conversation_metadata import ConversationMetadata
from openhands.storage.data_models.conversation_metadata_result_set import ConversationMetadataResultSet
from openhands.storage.postgres import PostgresStore

# Set up logging for this module
logger = logging.getLogger(__name__)

# Create type adapter for conversation metadata validation
conversation_metadata_type_adapter = TypeAdapter(ConversationMetadata)


class ConversationStoreError(Exception):
    """Custom exception for conversation store operations."""
    pass


class DBConversationStore(ConversationStore):
    """
    Database-backed conversation store with async execution and proper error handling.

    This implementation provides a robust conversation storage system that uses
    PostgreSQL as the backend with connection pooling, proper error handling,
    and async execution for non-blocking operations.
    """

    def __init__(self, db_store: PostgresStore) -> None:
        """
        Initialize the conversation store with a database backend.

        Args:
            db_store: PostgreSQL store instance for database operations
        """
        self.db_store = db_store
        logger.info("Initialized DBConversationStore")

    @classmethod
    async def get_instance(
        cls,
        config: Dict[str, Any],
        user_id: Optional[str]
    ) -> 'DBConversationStore':
        """
        Factory method to create a DBConversationStore instance.

        Args:
            config: Configuration dictionary for the store
            user_id: Optional user identifier for filtering

        Returns:
            DBConversationStore: Configured instance

        Raises:
            ConversationStoreError: If initialization fails
        """
        try:
            # Create PostgreSQL store specifically for sessions table
            db_store = PostgresStore(table='openhands_sessions')

            # Test the connection
            if not db_store.health_check():
                raise ConversationStoreError("Database health check failed during initialization")

            instance = cls(db_store)
            logger.info(f"Created DBConversationStore instance for user: {user_id}")
            return instance

        except Exception as e:
            logger.error(f"Failed to create DBConversationStore instance: {e}")
            raise ConversationStoreError(f"Failed to initialize conversation store: {e}")

    def _prepare_metadata_for_storage(self, metadata: ConversationMetadata) -> Dict[str, Any]:
        """
        Convert metadata object to a JSON-serializable dictionary.

        This method handles complex data types including enums, datetime objects,
        dataclasses, and Pydantic models to ensure they can be stored as JSON.

        Args:
            metadata: Conversation metadata to convert

        Returns:
            Dict[str, Any]: JSON-serializable dictionary
        """
        def to_jsonable(obj: Any) -> Any:
            """
            Recursively convert objects to JSON-serializable format.

            Args:
                obj: Object to convert

            Returns:
                JSON-serializable representation of the object
            """
            if isinstance(obj, enum.Enum):
                # Convert enums to their values
                return obj.value
            elif isinstance(obj, datetime.datetime):
                # Convert datetime to ISO format string
                return obj.isoformat()
            elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                # Convert dataclasses to dictionaries
                return {k: to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
            elif hasattr(obj, "model_dump"):
                # Handle Pydantic v2 models
                return obj.model_dump(mode="json")
            elif hasattr(obj, "dict"):
                # Handle Pydantic v1 models or other objects with dict() method
                return {k: to_jsonable(v) for k, v in obj.dict().items()}
            elif isinstance(obj, (list, tuple)):
                # Recursively handle sequences
                return [to_jsonable(x) for x in obj]
            elif isinstance(obj, dict):
                # Recursively handle dictionaries
                return {k: to_jsonable(v) for k, v in obj.items()}
            else:
                # Return object as-is for basic types (str, int, float, bool, None)
                return obj

        try:
            metadata_dict = to_jsonable(metadata)
            logger.debug(f"Successfully prepared metadata for storage: {metadata.conversation_id}")
            return metadata_dict
        except Exception as e:
            logger.error(f"Failed to prepare metadata for storage: {e}")
            raise ConversationStoreError(f"Failed to serialize metadata: {e}")

    async def save_metadata(self, metadata: ConversationMetadata) -> None:
        """
        Save conversation metadata to the database.

        Args:
            metadata: Conversation metadata to save

        Raises:
            ConversationStoreError: If save operation fails
        """
        try:
            # Convert metadata to JSON-serializable format
            metadata_dict = self._prepare_metadata_for_storage(metadata)
            session_id = metadata.conversation_id

            # Define the database operation
            def _write() -> None:
                """Internal function to perform the database write."""
                self.db_store.upsert_conversation(session_id, metadata_dict)

            # Execute in thread pool to avoid blocking the event loop
            await self._run_in_executor(_write)
            logger.info(f"Successfully saved metadata for conversation: {session_id}")

        except Exception as e:
            logger.error(f"Failed to save metadata for conversation {metadata.conversation_id}: {e}")
            raise ConversationStoreError(f"Failed to save conversation metadata: {e}")

    async def get_metadata(self, conversation_id: str) -> ConversationMetadata:
        """
        Retrieve conversation metadata from the database.

        Args:
            conversation_id: Unique identifier for the conversation

        Returns:
            ConversationMetadata: Retrieved and validated metadata

        Raises:
            FileNotFoundError: If conversation doesn't exist
            ConversationStoreError: If retrieval or validation fails
        """
        try:
            # Define the database operation
            def _read() -> ConversationMetadata:
                """Internal function to perform the database read."""
                return self.db_store.get_conversation_metadata(
                    conversation_id,
                    conversation_metadata_type_adapter
                )

            # Execute in thread pool to avoid blocking the event loop
            result = await self._run_in_executor(_read)
            logger.debug(f"Successfully retrieved metadata for conversation: {conversation_id}")
            return result

        except FileNotFoundError:
            # Re-raise FileNotFoundError as-is for proper handling upstream
            logger.warning(f"Conversation not found: {conversation_id}")
            raise
        except Exception as e:
            logger.error(f"Failed to get metadata for conversation {conversation_id}: {e}")
            raise ConversationStoreError(f"Failed to retrieve conversation metadata: {e}")

    async def delete_metadata(self, conversation_id: str) -> None:
        """
        Delete conversation metadata from the database.

        Args:
            conversation_id: Unique identifier for the conversation

        Raises:
            FileNotFoundError: If conversation doesn't exist
            ConversationStoreError: If deletion fails
        """
        try:
            # Define the database operation
            def _delete() -> None:
                """Internal function to perform the database delete."""
                self.db_store.delete_conversation(conversation_id)

            # Execute in thread pool to avoid blocking the event loop
            await self._run_in_executor(_delete)
            logger.info(f"Successfully deleted metadata for conversation: {conversation_id}")

        except FileNotFoundError:
            # Re-raise FileNotFoundError as-is for proper handling upstream
            logger.warning(f"Attempted to delete non-existent conversation: {conversation_id}")
            raise
        except Exception as e:
            logger.error(f"Failed to delete metadata for conversation {conversation_id}: {e}")
            raise ConversationStoreError(f"Failed to delete conversation metadata: {e}")

    async def exists(self, conversation_id: str) -> bool:
        """
        Check if a conversation exists in the database.

        Args:
            conversation_id: Unique identifier for the conversation

        Returns:
            bool: True if conversation exists, False otherwise

        Raises:
            ConversationStoreError: If existence check fails
        """
        try:
            # Define the database operation
            def _exists() -> bool:
                """Internal function to perform the existence check."""
                return self.db_store.exists_conversation(conversation_id)

            # Execute in thread pool to avoid blocking the event loop
            result = await self._run_in_executor(_exists)
            logger.debug(f"Existence check for conversation {conversation_id}: {result}")
            return result

        except Exception as e:
            logger.error(f"Failed to check existence of conversation {conversation_id}: {e}")
            raise ConversationStoreError(f"Failed to check conversation existence: {e}")

    async def search(
        self,
        page_id: Optional[str] = None,
        limit: int = 20
    ) -> ConversationMetadataResultSet:
        """
        Search and paginate through conversations.

        Args:
            page_id: Pagination cursor (offset as string, defaults to "0")
            limit: Maximum number of conversations to return per page

        Returns:
            ConversationMetadataResultSet: Paginated result set with conversations

        Raises:
            ConversationStoreError: If search operation fails
        """
        try:
            # Get all conversation IDs from the database
            all_ids = await self._run_in_executor(self.db_store.list_conversations)

            # Parse pagination parameters
            try:
                offset = int(page_id or 0)
            except ValueError:
                logger.warning(f"Invalid page_id '{page_id}', using 0")
                offset = 0

            # Ensure offset is non-negative
            offset = max(0, offset)

            # Get the slice of conversation IDs for this page
            page_ids = all_ids[offset:offset + limit]

            # Retrieve metadata for each conversation in the page
            page_conversations: List[ConversationMetadata] = []
            failed_conversations: List[str] = []

            for conversation_id in page_ids:
                try:
                    metadata = await self.get_metadata(conversation_id)
                    page_conversations.append(metadata)
                except FileNotFoundError:
                    # Conversation was deleted between listing and retrieval
                    logger.debug(f"Conversation {conversation_id} not found during search (likely deleted)")
                    failed_conversations.append(conversation_id)
                except Exception as e:
                    # Unexpected error - log but continue with other conversations
                    logger.warning(f"Error loading conversation {conversation_id} during search: {e}")
                    failed_conversations.append(conversation_id)

            # Log any failures for monitoring purposes
            if failed_conversations:
                logger.info(f"Failed to load {len(failed_conversations)} conversations during search: {failed_conversations}")

            # Calculate next page ID
            next_offset = offset + limit
            next_page_id = str(next_offset) if next_offset < len(all_ids) else None

            # Create and return result set
            result = ConversationMetadataResultSet(page_conversations, next_page_id)
            logger.info(
                f"Search completed: returned {len(page_conversations)} conversations "
                f"(offset: {offset}, limit: {limit}, next_page: {next_page_id})"
            )
            return result

        except Exception as e:
            logger.error(f"Failed to search conversations: {e}")
            raise ConversationStoreError(f"Failed to search conversations: {e}")

    async def _run_in_executor(self, fn, *args, **kwargs) -> Any:
        """
        Execute a synchronous function in a thread pool to avoid blocking the event loop.

        This method allows synchronous database operations to be executed asynchronously
        without blocking the main event loop, which is crucial for maintaining
        application responsiveness.

        Args:
            fn: Synchronous function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            Any: Result of the function execution

        Raises:
            ConversationStoreError: If executor execution fails
        """
        try:
            # Get the current event loop
            loop = asyncio.get_running_loop()

            # Execute the function in a thread pool
            # Using lambda to properly handle args and kwargs
            result = await loop.run_in_executor(
                None,
                lambda: fn(*args, **kwargs)
            )

            return result

        except Exception as e:
            logger.error(f"Failed to execute function in thread pool: {e}")
            raise ConversationStoreError(f"Thread pool execution failed: {e}")

    def close(self) -> None:
        """
        Clean up resources used by the conversation store.

        This method should be called when the store is no longer needed
        to ensure proper cleanup of database connections and other resources.
        """
        try:
            if self.db_store:
                self.db_store.close()
                logger.info("DBConversationStore closed successfully")
        except Exception as e:
            logger.error(f"Error closing DBConversationStore: {e}")

    def __enter__(self) -> 'DBConversationStore':
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit with automatic cleanup."""
        self.close()

    async def __aenter__(self) -> 'DBConversationStore':
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit with automatic cleanup."""
        self.close()
