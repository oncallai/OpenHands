"""
Improved PostgreSQL implementation of the database store.
This module provides a robust, production-ready PostgreSQL backend with
proper connection management, error handling, and resource cleanup.
"""

import builtins
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, Generator, Optional, Union

import psycopg2
import psycopg2.extras
import psycopg2.pool
import psycopg2.sql
from pydantic import TypeAdapter

from openhands.storage.db import DBStore

# Set up logging for this module
logger = logging.getLogger(__name__)


@dataclass
class DatabaseConfig:
    """
    Configuration class for PostgreSQL database connection.

    This class encapsulates all database connection parameters and provides
    validation to ensure required configuration is present.
    """

    host: str
    port: int
    user: str
    password: str
    dbname: str
    table: Optional[str] = None

    # Connection pool settings
    min_connections: int = 1
    max_connections: int = 20

    @classmethod
    def from_env(cls) -> 'DatabaseConfig':
        """
        Create configuration from environment variables.

        Returns:
            DatabaseConfig: Validated configuration object

        Raises:
            ValueError: If required environment variables are missing
        """
        # Check for required environment variables
        required_vars = [
            'SUPABASE_HOST',
            'SUPABASE_USER',
            'SUPABASE_PASSWORD',
            'SUPABASE_DBNAME',
        ]
        missing_vars = [var for var in required_vars if not os.environ.get(var)]

        if missing_vars:
            raise ValueError(f'Missing required environment variables: {missing_vars}')

        # Parse port with validation
        port_str = os.environ.get('SUPABASE_PORT', '5432')
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError(f'Invalid port number: {port_str}')

        return cls(
            host=os.environ['SUPABASE_HOST'],
            port=port,
            user=os.environ['SUPABASE_USER'],
            password=os.environ['SUPABASE_PASSWORD'],
            dbname=os.environ['SUPABASE_DBNAME'],
            table=os.environ.get('SUPABASE_TABLE'),
        )

    def get_connection_key(self) -> str:
        """Generate a unique key for this database configuration."""
        return f'{self.host}:{self.port}:{self.user}:{self.dbname}'


class DatabaseError(Exception):
    """Custom exception for database-related errors."""

    pass


class ConnectionPoolManager:
    """
    Singleton manager for database connection pools.

    This ensures that multiple PostgresStore instances with the same configuration
    share the same connection pool, optimizing resource usage.
    """

    _instance: Optional['ConnectionPoolManager'] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        """Initialize the manager attributes."""
        self._pools: dict[str, psycopg2.pool.ThreadedConnectionPool] = {}
        self._pool_refs: dict[str, int] = {}
        self._pools_lock = threading.Lock()

    def __new__(cls) -> 'ConnectionPoolManager':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def get_pool(self, config: DatabaseConfig) -> psycopg2.pool.ThreadedConnectionPool:
        """
        Get or create a connection pool for the given configuration.

        Args:
            config: Database configuration

        Returns:
            Connection pool instance

        Raises:
            DatabaseError: If pool creation fails
        """
        pool_key = config.get_connection_key()
        logger.debug(f'Requesting connection pool for {pool_key}')

        with self._pools_lock:
            if pool_key not in self._pools:
                logger.info(f'Creating new connection pool for {pool_key}')
                try:
                    connection_params = {
                        'host': config.host,
                        'port': config.port,
                        'user': config.user,
                        'password': config.password,
                        'dbname': config.dbname,
                    }

                    logger.debug(
                        f'Connecting to PostgreSQL: {config.host}:{config.port}/{config.dbname}'
                    )
                    pool = psycopg2.pool.ThreadedConnectionPool(
                        config.min_connections,
                        config.max_connections,
                        **connection_params,
                    )

                    self._pools[pool_key] = pool
                    self._pool_refs[pool_key] = 0
                    logger.info(
                        f'Created new PostgreSQL connection pool for {pool_key} '
                        f'with {config.min_connections}-{config.max_connections} connections'
                    )
                except psycopg2.Error as e:
                    logger.error(
                        f'Failed to create connection pool for {pool_key}: {e}'
                    )
                    raise DatabaseError(
                        f'Failed to create connection pool for {pool_key}: {e}'
                    )
            else:
                logger.debug(f'Reusing existing connection pool for {pool_key}')

            # Increment reference count
            self._pool_refs[pool_key] += 1
            logger.debug(
                f'Pool {pool_key} reference count: {self._pool_refs[pool_key]}'
            )
            return self._pools[pool_key]

    def release_pool(self, config: DatabaseConfig) -> None:
        """
        Release a reference to a connection pool.

        Args:
            config: Database configuration
        """
        pool_key = config.get_connection_key()
        logger.debug(f'Releasing pool reference for {pool_key}')

        with self._pools_lock:
            if pool_key in self._pool_refs:
                self._pool_refs[pool_key] -= 1
                logger.debug(
                    f'Pool {pool_key} reference count after release: {self._pool_refs[pool_key]}'
                )

                # If no more references, close the pool
                if self._pool_refs[pool_key] <= 0:
                    logger.info(f'No more references for pool {pool_key}, closing it')
                    if pool_key in self._pools:
                        try:
                            self._pools[pool_key].closeall()
                            logger.info(f'Closed connection pool for {pool_key}')
                        except Exception as e:
                            logger.error(
                                f'Error closing connection pool for {pool_key}: {e}'
                            )
                        finally:
                            del self._pools[pool_key]
                            del self._pool_refs[pool_key]
                            logger.debug(f'Removed pool {pool_key} from manager')
            else:
                logger.warning(
                    f'Attempted to release non-existent pool reference: {pool_key}'
                )

    def get_pool_stats(self) -> dict[str, dict[str, Any]]:
        """Get statistics for all managed pools."""
        logger.debug('Getting connection pool statistics')
        with self._pools_lock:
            stats = {}
            for pool_key, pool in self._pools.items():
                stats[pool_key] = {
                    'references': self._pool_refs.get(pool_key, 0),
                    'min_connections': pool.minconn,
                    'max_connections': pool.maxconn,
                    # Note: psycopg2 doesn't expose current connection count easily
                }
            logger.debug(f'Retrieved stats for {len(stats)} connection pools')
            return stats


class PostgresStore(DBStore):
    """
    PostgreSQL implementation of DBStore with optimized connection pooling and proper error handling.

    This implementation uses a singleton connection pool manager to efficiently share
    database connections across multiple store instances with the same configuration.
    """

    def __init__(
        self, table: Optional[str] = None, config: Optional[DatabaseConfig] = None
    ) -> None:
        """
        Initialize PostgreSQL store with optimized connection pooling.

        Args:
            table: Default table name to use for operations
            config: Database configuration (if None, will load from environment)

        Raises:
            ValueError: If configuration is invalid
            DatabaseError: If connection pool creation fails
        """
        self.config = config or DatabaseConfig.from_env()
        self.table = table or self.config.table
        self._pool_manager = ConnectionPoolManager()
        self._pool = self._pool_manager.get_pool(self.config)
        self._closed = False

        if not self.table:
            logger.warning(
                'No default table specified. Table must be provided for each operation.'
            )

    @contextmanager
    def get_connection(self) -> Generator[psycopg2.extensions.connection, None, None]:
        """
        Context manager for getting database connections from the pool.

        Yields:
            psycopg2.extensions.connection: Database connection

        Raises:
            DatabaseError: If unable to get connection from pool
        """
        if self._closed:
            raise DatabaseError('PostgresStore has been closed')

        conn = None
        try:
            conn = self._pool.getconn()
            if conn is None:
                raise DatabaseError('Unable to get connection from pool')
            yield conn
        except psycopg2.Error as e:
            if conn:
                conn.rollback()
            raise DatabaseError(f'Database operation failed: {e}')
        finally:
            if conn:
                self._pool.putconn(conn)

    def _validate_table_name(self, table: Optional[str] = None) -> str:
        """
        Validate and return table name to use for operations.

        Args:
            table: Table name to validate (uses default if None)

        Returns:
            str: Validated table name

        Raises:
            ValueError: If no table name is available
        """
        table_name = table or self.table
        if not table_name:
            raise ValueError('Table name must be specified')
        return table_name

    # Basic file-like operations
    def write(self, path: str, contents: Union[str, bytes]) -> None:
        """
        Write content to database using upsert operation.

        Args:
            path: Unique identifier for the content
            contents: Content to store

        Raises:
            DatabaseError: If write operation fails
        """
        table_name = self._validate_table_name()

        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    # Use psycopg2.sql for safe table name handling
                    query = psycopg2.sql.SQL(
                        'INSERT INTO {} (id, agent_state) VALUES (%s, %s) '
                        'ON CONFLICT (id) DO UPDATE SET agent_state = EXCLUDED.agent_state'
                    ).format(psycopg2.sql.Identifier(table_name))

                    cur.execute(query, (path, contents))
                    conn.commit()
                    logger.debug(f'Successfully wrote content to path: {path}')

                except psycopg2.Error as e:
                    conn.rollback()
                    raise DatabaseError(f'Failed to write to path {path}: {e}')

    def read(self, path: str) -> str:
        """
        Read content from database.

        Args:
            path: Unique identifier for the content

        Returns:
            str: The stored content

        Raises:
            FileNotFoundError: If path doesn't exist
            DatabaseError: If read operation fails
        """
        table_name = self._validate_table_name()

        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = psycopg2.sql.SQL(
                        'SELECT agent_state FROM {} WHERE id = %s'
                    ).format(psycopg2.sql.Identifier(table_name))

                    cur.execute(query, (path,))
                    row = cur.fetchone()

                    if not row:
                        raise FileNotFoundError(f'No record found for id={path}')

                    logger.debug(f'Successfully read content from path: {path}')
                    return row[0]

                except psycopg2.Error as e:
                    raise DatabaseError(f'Failed to read from path {path}: {e}')

    def list(self, path: str) -> list[str]:
        """
        List all IDs that start with the given path prefix.

        Args:
            path: Path prefix to search for

        Returns:
            List[str]: List of matching paths

        Raises:
            DatabaseError: If list operation fails
        """
        table_name = self._validate_table_name()

        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    # Prepare the LIKE pattern
                    prefix = path.rstrip('/')
                    like_pattern = prefix + '%' if prefix else '%'

                    query = psycopg2.sql.SQL(
                        'SELECT id FROM {} WHERE id LIKE %s ORDER BY id'
                    ).format(psycopg2.sql.Identifier(table_name))

                    cur.execute(query, (like_pattern,))
                    result = [row[0] for row in cur.fetchall()]

                    logger.debug(f'Listed {len(result)} items with prefix: {path}')
                    return result

                except psycopg2.Error as e:
                    raise DatabaseError(f'Failed to list paths with prefix {path}: {e}')

    def delete(self, path: str) -> None:
        """
        Delete content at the specified path.

        Args:
            path: Unique identifier to delete

        Raises:
            FileNotFoundError: If path doesn't exist
            DatabaseError: If delete operation fails
        """
        table_name = self._validate_table_name()

        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = psycopg2.sql.SQL('DELETE FROM {} WHERE id = %s').format(
                        psycopg2.sql.Identifier(table_name)
                    )

                    cur.execute(query, (path,))

                    if cur.rowcount == 0:
                        raise FileNotFoundError(f'No record found for id={path}')

                    conn.commit()
                    logger.debug(f'Successfully deleted path: {path}')

                except psycopg2.Error as e:
                    conn.rollback()
                    raise DatabaseError(f'Failed to delete path {path}: {e}')

    # Conversation-specific methods for openhands_sessions table
    def upsert_conversation(
        self, session_id: str, metadata_dict: dict[str, Any]
    ) -> None:
        """
        Insert or update conversation metadata.

        Args:
            session_id: Unique session identifier
            metadata_dict: Conversation metadata to store

        Raises:
            DatabaseError: If upsert operation fails
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = """
                        INSERT INTO openhands_sessions (id, metadata, created_at, updated_at)
                        VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        ON CONFLICT (id) DO UPDATE SET
                            metadata = EXCLUDED.metadata,
                            updated_at = CURRENT_TIMESTAMP
                    """
                    cur.execute(
                        query, (session_id, psycopg2.extras.Json(metadata_dict))
                    )
                    conn.commit()
                    logger.debug(f'Successfully upserted conversation: {session_id}')

                except psycopg2.Error as e:
                    conn.rollback()
                    raise DatabaseError(
                        f'Failed to upsert conversation {session_id}: {e}'
                    )

    def get_conversation(self, session_id: str) -> dict[str, Any]:
        """
        Get conversation metadata as raw dictionary.

        Args:
            session_id: Unique session identifier

        Returns:
            Dict[str, Any]: Raw conversation metadata

        Raises:
            FileNotFoundError: If conversation doesn't exist
            DatabaseError: If read operation fails
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = 'SELECT metadata FROM openhands_sessions WHERE id = %s'
                    cur.execute(query, (session_id,))
                    row = cur.fetchone()

                    if not row:
                        raise FileNotFoundError(f'Conversation not found: {session_id}')

                    logger.debug(f'Successfully retrieved conversation: {session_id}')
                    return row[0]

                except psycopg2.Error as e:
                    raise DatabaseError(f'Failed to get conversation {session_id}: {e}')

    def get_conversation_metadata(
        self, session_id: str, type_adapter: TypeAdapter
    ) -> Any:
        """
        Get conversation metadata with type validation.

        Args:
            session_id: Unique session identifier
            type_adapter: Pydantic type adapter for validation

        Returns:
            Validated conversation metadata object

        Raises:
            FileNotFoundError: If conversation doesn't exist or invalid
            DatabaseError: If read operation fails
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = 'SELECT metadata FROM openhands_sessions WHERE id = %s'
                    cur.execute(query, (session_id,))
                    row = cur.fetchone()

                    if not row:
                        raise FileNotFoundError(f'Conversation not found: {session_id}')

                    json_obj = row[0]

                    # Validate required fields
                    if 'created_at' not in json_obj:
                        raise FileNotFoundError(
                            f'Invalid conversation metadata for {session_id}'
                        )

                    # Clean up deprecated fields
                    if 'github_user_id' in json_obj:
                        json_obj = json_obj.copy()  # Don't modify the original
                        json_obj.pop('github_user_id')

                    # Validate and return typed object
                    result = type_adapter.validate_python(json_obj)
                    logger.debug(
                        f'Successfully retrieved and validated conversation: {session_id}'
                    )
                    return result

                except psycopg2.Error as e:
                    raise DatabaseError(
                        f'Failed to get conversation metadata {session_id}: {e}'
                    )

    def delete_conversation(self, session_id: str) -> None:
        """
        Delete a conversation and all associated data.

        Args:
            session_id: Unique session identifier

        Raises:
            FileNotFoundError: If conversation doesn't exist
            DatabaseError: If delete operation fails
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = 'DELETE FROM openhands_sessions WHERE id = %s'
                    cur.execute(query, (session_id,))

                    if cur.rowcount == 0:
                        raise FileNotFoundError(f'Conversation not found: {session_id}')

                    conn.commit()
                    logger.debug(f'Successfully deleted conversation: {session_id}')

                except psycopg2.Error as e:
                    conn.rollback()
                    raise DatabaseError(
                        f'Failed to delete conversation {session_id}: {e}'
                    )

    def exists_conversation(self, session_id: str) -> bool:
        """
        Check if a conversation exists.

        Args:
            session_id: Unique session identifier

        Returns:
            bool: True if conversation exists, False otherwise

        Raises:
            DatabaseError: If check operation fails
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = 'SELECT 1 FROM openhands_sessions WHERE id = %s'
                    cur.execute(query, (session_id,))
                    result = cur.fetchone() is not None
                    logger.debug(
                        f'Conversation existence check for {session_id}: {result}'
                    )
                    return result

                except psycopg2.Error as e:
                    raise DatabaseError(
                        f'Failed to check conversation existence {session_id}: {e}'
                    )

    def list_conversations(self) -> builtins.list[str]:
        """
        List all conversation IDs.

        Returns:
            List[str]: List of conversation IDs

        Raises:
            DatabaseError: If list operation fails
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = 'SELECT id FROM openhands_sessions ORDER BY created_at DESC'
                    cur.execute(query)
                    result = [row[0] for row in cur.fetchall()]
                    logger.debug(f'Listed {len(result)} conversations')
                    return result

                except psycopg2.Error as e:
                    raise DatabaseError(f'Failed to list conversations: {e}')

    # Event-specific methods for openhands_events table
    def write_event(
        self, session_id: str, event_index: int, event_data: dict[str, Any]
    ) -> None:
        """
        Store an event for a session.

        Args:
            session_id: Unique session identifier
            event_index: Sequential event index
            event_data: Event data to store

        Raises:
            DatabaseError: If write operation fails
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = """
                        INSERT INTO openhands_events (session_id, event_index, event_data)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (session_id, event_index)
                        DO UPDATE SET event_data = EXCLUDED.event_data
                    """
                    cur.execute(
                        query,
                        (session_id, event_index, psycopg2.extras.Json(event_data)),
                    )
                    conn.commit()
                    logger.debug(
                        f'Successfully wrote event {event_index} for session {session_id}'
                    )

                except psycopg2.Error as e:
                    conn.rollback()
                    raise DatabaseError(
                        f'Failed to write event {event_index} for session {session_id}: {e}'
                    )

    def read_event(self, session_id: str, event_index: int) -> dict[str, Any]:
        """
        Read a specific event from a session.

        Args:
            session_id: Unique session identifier
            event_index: Sequential event index

        Returns:
            Dict[str, Any]: Event data

        Raises:
            FileNotFoundError: If event doesn't exist
            DatabaseError: If read operation fails
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = """
                        SELECT event_data FROM openhands_events
                        WHERE session_id = %s AND event_index = %s
                    """
                    cur.execute(query, (session_id, event_index))
                    row = cur.fetchone()

                    if not row:
                        raise FileNotFoundError(
                            f'No event found for session_id={session_id} event_index={event_index}'
                        )

                    logger.debug(
                        f'Successfully read event {event_index} for session {session_id}'
                    )
                    return row[0]

                except psycopg2.Error as e:
                    raise DatabaseError(
                        f'Failed to read event {event_index} for session {session_id}: {e}'
                    )

    def list_events(self, session_id: str) -> builtins.list[int]:
        """
        List all event indices for a session in ascending order.

        Args:
            session_id: Unique session identifier

        Returns:
            List[int]: Sorted list of event indices

        Raises:
            DatabaseError: If list operation fails
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = """
                        SELECT event_index FROM openhands_events
                        WHERE session_id = %s ORDER BY event_index ASC
                    """
                    cur.execute(query, (session_id,))
                    result = [row[0] for row in cur.fetchall()]
                    logger.debug(
                        f'Listed {len(result)} events for session {session_id}'
                    )
                    return result

                except psycopg2.Error as e:
                    raise DatabaseError(
                        f'Failed to list events for session {session_id}: {e}'
                    )

    def delete_event(self, session_id: str, event_index: int) -> None:
        """
        Delete a specific event from a session.

        Args:
            session_id: Unique session identifier
            event_index: Sequential event index

        Raises:
            FileNotFoundError: If event doesn't exist
            DatabaseError: If delete operation fails
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = """
                        DELETE FROM openhands_events
                        WHERE session_id = %s AND event_index = %s
                    """
                    cur.execute(query, (session_id, event_index))

                    if cur.rowcount == 0:
                        raise FileNotFoundError(
                            f'No event found for session_id={session_id} event_index={event_index}'
                        )

                    conn.commit()
                    logger.debug(
                        f'Successfully deleted event {event_index} for session {session_id}'
                    )

                except psycopg2.Error as e:
                    conn.rollback()
                    raise DatabaseError(
                        f'Failed to delete event {event_index} for session {session_id}: {e}'
                    )

    # Health check and resource management
    def health_check(self) -> bool:
        """
        Check if the database connection is healthy.

        Returns:
            bool: True if healthy, False otherwise
        """
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT 1')
                    result = cur.fetchone() is not None
                    logger.debug(f'Health check result: {result}')
                    return result
        except Exception as e:
            logger.warning(f'Health check failed: {e}')
            return False

    def close(self) -> None:
        """
        Clean up connection pool and resources.

        This method releases the reference to the shared connection pool,
        allowing proper cleanup when no longer needed.
        """
        if not self._closed:
            try:
                self._pool_manager.release_pool(self.config)
                self._closed = True
                logger.info('PostgreSQL store closed successfully')
            except Exception as e:
                logger.error(f'Error closing PostgreSQL store: {e}')

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with automatic cleanup."""
        self.close()
