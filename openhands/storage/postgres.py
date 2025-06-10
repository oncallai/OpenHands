"""
Improved PostgreSQL implementation of the database store.
This module provides a robust, production-ready PostgreSQL backend with
proper connection management, error handling, and resource cleanup.
"""

import builtins
import os
import threading
from contextlib import contextmanager
from typing import Any, Generator, Optional, Union

import psycopg2
import psycopg2.extras
import psycopg2.pool
import psycopg2.sql
from pydantic import TypeAdapter

from openhands.core.logger import openhands_logger as logger
from openhands.storage.db import DBStore

# Global registry for connection pools - shared across all instances
_global_pool_registry: dict[str, psycopg2.pool.ThreadedConnectionPool] = {}
_global_pool_refs: dict[str, int] = {}
_global_registry_lock = threading.Lock()

# Global config cache to ensure same configuration object
_global_config_cache: Optional['DatabaseConfig'] = None
_config_cache_lock = threading.Lock()


class DatabaseConfig:
    """
    Configuration class for PostgreSQL database connection.

    This class encapsulates all database connection parameters and provides
    validation to ensure required configuration is present.
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        dbname: str,
        table: Optional[str] = None,
        min_connections: int = 1,
        max_connections: int = 20,
    ):
        """Initialize the database configuration."""
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.dbname = dbname
        self.table = table
        self.min_connections = min_connections
        self.max_connections = max_connections

    @classmethod
    def from_env(cls) -> 'DatabaseConfig':
        """
        Create configuration from environment variables with global caching.

        Returns:
            DatabaseConfig: Shared configuration object

        Raises:
            ValueError: If required environment variables are missing
        """
        global _global_config_cache

        if _global_config_cache is None:
            with _config_cache_lock:
                if _global_config_cache is None:
                    # Check for required environment variables
                    required_vars = [
                        'SUPABASE_HOST',
                        'SUPABASE_USER',
                        'SUPABASE_PASSWORD',
                        'SUPABASE_DBNAME',
                    ]
                    missing_vars = [
                        var for var in required_vars if not os.environ.get(var)
                    ]

                    if missing_vars:
                        raise ValueError(
                            f'Missing required environment variables: {missing_vars}'
                        )

                    # Parse port with validation
                    port_str = os.environ.get('SUPABASE_PORT', '5432')
                    try:
                        port = int(port_str)
                    except ValueError:
                        raise ValueError(f'Invalid port number: {port_str}')

                    _global_config_cache = cls(
                        host=os.environ['SUPABASE_HOST'],
                        port=port,
                        user=os.environ['SUPABASE_USER'],
                        password=os.environ['SUPABASE_PASSWORD'],
                        dbname=os.environ['SUPABASE_DBNAME'],
                        table=os.environ.get('SUPABASE_TABLE'),
                    )
                    logger.debug(
                        f'Created global DatabaseConfig for {_global_config_cache.host}:{_global_config_cache.port}/{_global_config_cache.dbname}'
                    )

        return _global_config_cache

    def get_connection_key(self) -> str:
        """Generate a unique key for this database configuration."""
        return f'{self.host}:{self.port}:{self.user}:{self.dbname}'


class DatabaseError(Exception):
    """Custom exception for database-related errors."""

    pass


def get_connection_pool(config: DatabaseConfig) -> psycopg2.pool.ThreadedConnectionPool:
    """
    Get or create a connection pool using global registry.

    This function ensures that only one connection pool exists per database configuration
    across all processes and threads.

    Args:
        config: Database configuration

    Returns:
        Connection pool instance

    Raises:
        DatabaseError: If pool creation fails
    """
    pool_key = config.get_connection_key()

    with _global_registry_lock:
        if pool_key not in _global_pool_registry:
            logger.debug(f'Creating GLOBAL connection pool for {pool_key}')
            try:
                connection_params = {
                    'host': config.host,
                    'port': config.port,
                    'user': config.user,
                    'password': config.password,
                    'dbname': config.dbname,
                }

                pool = psycopg2.pool.ThreadedConnectionPool(
                    config.min_connections,
                    config.max_connections,
                    **connection_params,
                )

                _global_pool_registry[pool_key] = pool
                _global_pool_refs[pool_key] = 0
                logger.info(
                    f'Created GLOBAL PostgreSQL connection pool for {pool_key} '
                    f'with {config.min_connections}-{config.max_connections} connections '
                    f'(Total global pools: {len(_global_pool_registry)})'
                )
            except psycopg2.Error as e:
                logger.error(f'Failed to create connection pool for {pool_key}: {e}')
                raise DatabaseError(
                    f'Failed to create connection pool for {pool_key}: {e}'
                )
        else:
            logger.debug(
                f'Reusing GLOBAL connection pool for {pool_key} (Total global pools: {len(_global_pool_registry)})'
            )

    # Increment reference count
    _global_pool_refs[pool_key] += 1
    # Only log reference count for new pools or every 100th reference
    if _global_pool_refs[pool_key] == 1:
        logger.debug(f'Global pool {pool_key} first reference')
    elif _global_pool_refs[pool_key] % 100 == 0:
        logger.info(
            f'Global pool {pool_key} usage: {_global_pool_refs[pool_key]} references (Total pools: {len(_global_pool_registry)})'
        )
    return _global_pool_registry[pool_key]


def release_connection_pool(config: DatabaseConfig) -> None:
    """
    Release a reference to a connection pool in the global registry.

    Args:
        config: Database configuration
    """
    pool_key = config.get_connection_key()

    with _global_registry_lock:
        if pool_key in _global_pool_refs:
            _global_pool_refs[pool_key] -= 1
            logger.debug(
                f'Global pool {pool_key} reference count after release: {_global_pool_refs[pool_key]}'
            )

            # If no more references, close the pool
            if _global_pool_refs[pool_key] <= 0:
                logger.info(
                    f'No more references for global pool {pool_key}, closing it'
                )
                if pool_key in _global_pool_registry:
                    try:
                        _global_pool_registry[pool_key].closeall()
                        logger.info(f'Closed global connection pool for {pool_key}')
                    except Exception as e:
                        logger.error(
                            f'Error closing global connection pool for {pool_key}: {e}'
                        )
                    finally:
                        del _global_pool_registry[pool_key]
                        del _global_pool_refs[pool_key]
                        logger.debug(f'Removed global pool {pool_key} from registry')
        else:
            logger.warning(
                f'Attempted to release non-existent global pool reference: {pool_key}'
            )


def get_global_pool_stats() -> dict[str, dict[str, Any]]:
    """Get statistics for all global connection pools."""
    with _global_registry_lock:
        stats = {}
        for pool_key, pool in _global_pool_registry.items():
            stats[pool_key] = {
                'references': _global_pool_refs.get(pool_key, 0),
                'min_connections': pool.minconn,
                'max_connections': pool.maxconn,
            }
        return stats


class ConnectionPoolManager:
    """
    Simplified connection pool manager that uses global registry.

    This class is now just a lightweight wrapper around the global pool functions.
    """

    def __init__(self) -> None:
        """Initialize the manager."""
        logger.debug('ConnectionPoolManager created (uses global registry)')

    def get_pool(self, config: DatabaseConfig) -> psycopg2.pool.ThreadedConnectionPool:
        """Get connection pool from global registry."""
        return get_connection_pool(config)

    def release_pool(self, config: DatabaseConfig) -> None:
        """Release connection pool from global registry."""
        release_connection_pool(config)

    def get_pool_stats(self) -> dict[str, dict[str, Any]]:
        """Get global pool statistics."""
        return get_global_pool_stats()


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
        Store or update conversation metadata.

        Args:
            session_id: Unique session identifier
            metadata_dict: Conversation metadata to store

        Raises:
            DatabaseError: If upsert operation fails
        """
        # Get the table name (defaults to configured table or openhands_sessions)
        table_name = self.table or 'openhands_sessions'

        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = psycopg2.sql.SQL("""
                        INSERT INTO {} (id, metadata, created_at, updated_at)
                        VALUES (%s, %s, NOW(), NOW())
                        ON CONFLICT (id)
                        DO UPDATE SET metadata = EXCLUDED.metadata, updated_at = NOW()
                    """).format(psycopg2.sql.Identifier(table_name))

                    cur.execute(
                        query,
                        (session_id, psycopg2.extras.Json(metadata_dict)),
                    )
                    conn.commit()
                    logger.debug(
                        f'Successfully upserted conversation metadata for session {session_id} in table {table_name}'
                    )

                except psycopg2.Error as e:
                    conn.rollback()
                    raise DatabaseError(
                        f'Failed to upsert conversation {session_id}: {e}'
                    )

    def get_conversation(self, session_id: str) -> dict[str, Any]:
        """
        Get conversation metadata as a raw dictionary.

        Args:
            session_id: Unique session identifier

        Returns:
            Dict[str, Any]: Raw conversation metadata

        Raises:
            FileNotFoundError: If session doesn't exist
            DatabaseError: If read operation fails
        """
        # Get the table name (defaults to configured table or openhands_sessions)
        table_name = self.table or 'openhands_sessions'

        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = psycopg2.sql.SQL(
                        'SELECT metadata FROM {} WHERE id = %s'
                    ).format(psycopg2.sql.Identifier(table_name))
                    cur.execute(query, (session_id,))
                    row = cur.fetchone()

                    if not row:
                        raise FileNotFoundError(f'Session not found: {session_id}')

                    logger.debug(
                        f'Successfully retrieved conversation metadata for session {session_id} from table {table_name}'
                    )
                    return row[0]

                except psycopg2.Error as e:
                    raise DatabaseError(f'Failed to get conversation {session_id}: {e}')

    def get_conversation_metadata(
        self, session_id: str, type_adapter: TypeAdapter
    ) -> Any:
        """
        Get conversation metadata with type conversion.

        Args:
            session_id: Unique session identifier
            type_adapter: Pydantic type adapter for conversion

        Returns:
            Any: Converted conversation metadata

        Raises:
            FileNotFoundError: If session doesn't exist
            DatabaseError: If read operation fails
        """
        # Get the table name (defaults to configured table or openhands_sessions)
        table_name = self.table or 'openhands_sessions'

        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = psycopg2.sql.SQL(
                        'SELECT metadata FROM {} WHERE id = %s'
                    ).format(psycopg2.sql.Identifier(table_name))
                    cur.execute(query, (session_id,))
                    row = cur.fetchone()

                    if not row:
                        raise FileNotFoundError(f'Conversation not found: {session_id}')

                    try:
                        result = type_adapter.validate_python(row[0])
                        logger.debug(
                            f'Successfully retrieved and converted conversation metadata for session {session_id} from table {table_name}'
                        )
                        return result
                    except Exception as e:
                        logger.error(
                            f'Failed to convert metadata for conversation {session_id}: {e}'
                        )
                        raise DatabaseError(
                            f'Failed to convert conversation metadata for {session_id}: {e}'
                        )

                except psycopg2.Error as e:
                    raise DatabaseError(
                        f'Failed to get conversation metadata {session_id}: {e}'
                    )

    def delete_conversation(self, session_id: str) -> None:
        """
        Delete a conversation and all its associated data.

        Args:
            session_id: Unique session identifier

        Raises:
            FileNotFoundError: If session doesn't exist
            DatabaseError: If delete operation fails
        """
        # Get the table name (defaults to configured table or openhands_sessions)
        table_name = self.table or 'openhands_sessions'

        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = psycopg2.sql.SQL('DELETE FROM {} WHERE id = %s').format(
                        psycopg2.sql.Identifier(table_name)
                    )
                    cur.execute(query, (session_id,))

                    if cur.rowcount == 0:
                        raise FileNotFoundError(f'Session not found: {session_id}')

                    conn.commit()
                    logger.debug(
                        f'Successfully deleted conversation {session_id} from table {table_name}'
                    )

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
            bool: True if conversation exists

        Raises:
            DatabaseError: If existence check fails
        """
        # Get the table name (defaults to configured table or openhands_sessions)
        table_name = self.table or 'openhands_sessions'

        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = psycopg2.sql.SQL('SELECT 1 FROM {} WHERE id = %s').format(
                        psycopg2.sql.Identifier(table_name)
                    )
                    cur.execute(query, (session_id,))
                    result = cur.fetchone() is not None
                    logger.debug(
                        f'Conversation existence check for {session_id} in table {table_name}: {result}'
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
        # Get the table name (defaults to configured table or openhands_sessions)
        table_name = self.table or 'openhands_sessions'

        with self.get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    query = psycopg2.sql.SQL(
                        'SELECT id FROM {} ORDER BY created_at DESC'
                    ).format(psycopg2.sql.Identifier(table_name))
                    cur.execute(query)
                    result = [row[0] for row in cur.fetchall()]
                    logger.debug(
                        f'Listed {len(result)} conversations from table {table_name}'
                    )
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

                    # Ensure we return a proper dict, handling various PostgreSQL data types
                    event_data = row[0]
                    if isinstance(event_data, dict):
                        result = event_data
                    elif isinstance(event_data, (str, bytes)):
                        import json

                        result = json.loads(event_data)
                    else:
                        # Handle memoryview or other PostgreSQL-specific types
                        import json

                        result = json.loads(str(event_data))

                    logger.debug(
                        f'Successfully read event {event_index} for session {session_id}'
                    )
                    return result

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
