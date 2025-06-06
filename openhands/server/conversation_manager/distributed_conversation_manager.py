import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

import socketio

try:
    import redis.asyncio as redis
    from redis.asyncio.lock import Lock as RedisLock
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None
    RedisLock = None

from openhands.core.config.openhands_config import OpenHandsConfig
from openhands.core.exceptions import AgentRuntimeUnavailableError
from openhands.core.logger import openhands_logger as logger
from openhands.core.schema.agent import AgentState
from openhands.events.action import MessageAction
from openhands.events.stream import EventStreamSubscriber, session_exists
from openhands.server.config.server_config import ServerConfig
from openhands.server.data_models.agent_loop_info import AgentLoopInfo
from openhands.server.monitoring import MonitoringListener
from openhands.server.session.agent_session import WAIT_TIME_BEFORE_CLOSE
from openhands.server.session.conversation import ServerConversation
from openhands.server.session.session import ROOM_KEY, Session
from openhands.storage.store import Store
from openhands.storage.conversation.conversation_store import ConversationStore
from openhands.storage.data_models.conversation_metadata import ConversationMetadata
from openhands.storage.data_models.settings import Settings
from openhands.utils.async_utils import GENERAL_TIMEOUT, call_async_from_sync, wait_all
from openhands.utils.conversation_summary import (
    auto_generate_title,
    get_default_conversation_title,
)
from openhands.utils.import_utils import get_impl
from openhands.utils.shutdown_listener import should_continue

from openhands.server.conversation_manager.conversation_manager import ConversationManager

_CLEANUP_INTERVAL = int(os.environ.get('CLEANUP_INTERVAL', '15'))
UPDATED_AT_CALLBACK_ID = 'updated_at_callback_id'
LOCK_TIMEOUT = int(os.environ.get('REDIS_LOCK_TIMEOUT', '30'))
SESSION_TTL = int(os.environ.get('SESSION_TTL', '3600'))


@dataclass
class DistributedConversationManager(ConversationManager):
    """Distributed implementation of ConversationManager for multi-pod Kubernetes deployments.

    This implementation stores all state in Redis to enable horizontal scaling.
    Automatically initializes Redis connection from environment variables.
    """

    sio: socketio.AsyncServer
    config: OpenHandsConfig
    file_store: Store
    server_config: ServerConfig
    monitoring_listener: MonitoringListener = MonitoringListener()

    # Redis client initialized in __post_init__
    redis_client: 'redis.Redis' = field(init=False)

    # Instance identifier for this pod
    pod_id: str = field(default_factory=lambda: f"{os.environ.get('POD_NAME', 'unknown')}-{str(uuid.uuid4())[:8]}")

    # Redis configuration from environment
    redis_url: str = field(default_factory=lambda: os.environ.get('REDIS_URL', 'redis://localhost:6379'))
    redis_db: int = field(default_factory=lambda: int(os.environ.get('REDIS_DB', '0')))
    redis_password: str = field(default_factory=lambda: os.environ.get('REDIS_PASSWORD', ''))

    # Redis key prefixes
    SESSIONS_KEY: str = "oh:sessions"
    CONNECTIONS_KEY: str = "oh:connections"
    ACTIVE_CONVERSATIONS_KEY: str = "oh:active_conversations"
    DETACHED_CONVERSATIONS_KEY: str = "oh:detached_conversations"
    LOCKS_KEY: str = "oh:locks"

    _cleanup_task: asyncio.Task | None = None
    _conversation_store_class: type[ConversationStore] | None = None
    _redis_initialized: bool = False

    def __post_init__(self):
        """Initialize Redis connection after dataclass initialization."""
        if not REDIS_AVAILABLE:
            raise ImportError(
                "Redis is required for DistributedConversationManager. "
                "Install with: pip install redis[hiredis]"
            )
        # Redis client will be initialized in __aenter__

    async def _initialize_redis(self):
        """Initialize Redis connection with retry logic."""
        if self._redis_initialized:
            return

        try:
            redis_kwargs = {
                'db': self.redis_db,
                'decode_responses': True,
                'health_check_interval': 30,
                'retry_on_timeout': True,
                'socket_keepalive': True,
                'socket_keepalive_options': {},
            }

            if self.redis_password:
                redis_kwargs['password'] = self.redis_password

            self.redis_client = redis.from_url(self.redis_url, **redis_kwargs)

            # Test connection
            await self.redis_client.ping()
            logger.info(f"✅ Connected to Redis at {self.redis_url} (Pod: {self.pod_id})")
            self._redis_initialized = True

        except Exception as e:
            logger.error(f"❌ Failed to connect to Redis: {e}")
            raise ConnectionError(f"Could not connect to Redis at {self.redis_url}: {e}")

    async def __aenter__(self):
        await self._initialize_redis()
        self._cleanup_task = asyncio.create_task(self._cleanup_stale_distributed())
        logger.info(f"🚀 DistributedConversationManager started (Pod: {self.pod_id})")
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None

        # Clean up any sessions owned by this pod
        await self._cleanup_pod_sessions()

        # Close Redis connection
        if hasattr(self, 'redis_client') and self.redis_client:
            await self.redis_client.aclose()

        logger.info(f"🛑 DistributedConversationManager stopped (Pod: {self.pod_id})")

    async def _get_distributed_lock(self, key: str, timeout: int = LOCK_TIMEOUT) -> RedisLock:
        """Get a distributed Redis lock for coordination across pods."""
        lock_key = f"{self.LOCKS_KEY}:{key}"
        return RedisLock(self.redis_client, lock_key, timeout=timeout)

    async def _store_session_data(self, sid: str, session_data: dict):
        """Store session data in Redis with TTL."""
        session_data['pod_id'] = self.pod_id
        session_data['last_active'] = time.time()
        await self.redis_client.hset(
            self.SESSIONS_KEY,
            sid,
            json.dumps(session_data)
        )
        await self.redis_client.expire(f"{self.SESSIONS_KEY}:{sid}", SESSION_TTL)

    async def _get_session_data(self, sid: str) -> dict | None:
        """Retrieve session data from Redis."""
        data = await self.redis_client.hget(self.SESSIONS_KEY, sid)
        if data:
            return json.loads(data)
        return None

    async def _remove_session_data(self, sid: str):
        """Remove session data from Redis."""
        await self.redis_client.hdel(self.SESSIONS_KEY, sid)

    async def _store_connection_mapping(self, connection_id: str, sid: str):
        """Store connection ID to session ID mapping in Redis."""
        await self.redis_client.hset(self.CONNECTIONS_KEY, connection_id, sid)
        await self.redis_client.expire(f"{self.CONNECTIONS_KEY}:{connection_id}", SESSION_TTL)

    async def _get_connection_mapping(self, connection_id: str) -> str | None:
        """Get session ID for a connection ID from Redis."""
        return await self.redis_client.hget(self.CONNECTIONS_KEY, connection_id)

    async def _remove_connection_mapping(self, connection_id: str):
        """Remove connection mapping from Redis."""
        await self.redis_client.hdel(self.CONNECTIONS_KEY, connection_id)

    async def _store_conversation_data(self, sid: str, conversation_data: dict, is_active: bool = True):
        """Store conversation data in Redis."""
        key = self.ACTIVE_CONVERSATIONS_KEY if is_active else self.DETACHED_CONVERSATIONS_KEY
        conversation_data['pod_id'] = self.pod_id
        conversation_data['timestamp'] = time.time()
        await self.redis_client.hset(key, sid, json.dumps(conversation_data))
        await self.redis_client.expire(f"{key}:{sid}", SESSION_TTL)

    async def _get_conversation_data(self, sid: str, is_active: bool = True) -> dict | None:
        """Retrieve conversation data from Redis."""
        key = self.ACTIVE_CONVERSATIONS_KEY if is_active else self.DETACHED_CONVERSATIONS_KEY
        data = await self.redis_client.hget(key, sid)
        if data:
            return json.loads(data)
        return None

    async def _remove_conversation_data(self, sid: str, is_active: bool = True):
        """Remove conversation data from Redis."""
        key = self.ACTIVE_CONVERSATIONS_KEY if is_active else self.DETACHED_CONVERSATIONS_KEY
        await self.redis_client.hdel(key, sid)

    async def _move_conversation_to_detached(self, sid: str):
        """Move conversation from active to detached state."""
        conversation_data = await self._get_conversation_data(sid, is_active=True)
        if conversation_data:
            await self._remove_conversation_data(sid, is_active=True)
            await self._store_conversation_data(sid, conversation_data, is_active=False)

    async def attach_to_conversation(
        self, sid: str, user_id: str | None = None
    ) -> ServerConversation | None:
        start_time = time.time()
        if not await session_exists(sid, self.file_store, user_id=user_id):
            return None

        # Use distributed lock for conversation attachment
        async with await self._get_distributed_lock(f"conversation:{sid}"):
            # Check if we have an active conversation
            conversation_data = await self._get_conversation_data(sid, is_active=True)
            if conversation_data:
                conversation_data['ref_count'] = conversation_data.get('ref_count', 0) + 1
                await self._store_conversation_data(sid, conversation_data, is_active=True)
                logger.info(f'Reusing active conversation {sid}', extra={'session_id': sid})
                # Return a new ServerConversation instance
                return await self._create_server_conversation(sid, user_id)

            # Check if we have a detached conversation
            conversation_data = await self._get_conversation_data(sid, is_active=False)
            if conversation_data:
                await self._remove_conversation_data(sid, is_active=False)
                conversation_data['ref_count'] = 1
                await self._store_conversation_data(sid, conversation_data, is_active=True)
                logger.info(f'Reusing detached conversation {sid}', extra={'session_id': sid})
                return await self._create_server_conversation(sid, user_id)

            # Create new conversation
            c = await self._create_server_conversation(sid, user_id)
            if c:
                conversation_data = {'ref_count': 1, 'user_id': user_id}
                await self._store_conversation_data(sid, conversation_data, is_active=True)
                end_time = time.time()
                logger.info(
                    f'ServerConversation {c.sid} connected in {end_time - start_time} seconds'
                )
            return c

    async def _create_server_conversation(self, sid: str, user_id: str | None) -> ServerConversation | None:
        """Create and connect a ServerConversation instance."""
        c = ServerConversation(
            sid, file_store=self.file_store, config=self.config, user_id=user_id
        )
        try:
            await c.connect()
            return c
        except AgentRuntimeUnavailableError as e:
            logger.error(
                f'Error connecting to conversation {c.sid}: {e}',
                extra={'session_id': sid},
            )
            await c.disconnect()
            return None

    async def join_conversation(
        self,
        sid: str,
        connection_id: str,
        settings: Settings,
        user_id: str | None,
    ) -> AgentLoopInfo:
        logger.info(
            f'join_conversation:{sid}:{connection_id}',
            extra={'session_id': sid, 'user_id': user_id},
        )
        await self.sio.enter_room(connection_id, ROOM_KEY.format(sid=sid))
        await self._store_connection_mapping(connection_id, sid)
        agent_loop_info = await self.maybe_start_agent_loop(sid, settings, user_id)
        return agent_loop_info

    async def detach_from_conversation(self, conversation: ServerConversation):
        sid = conversation.sid
        async with await self._get_distributed_lock(f"conversation:{sid}"):
            conversation_data = await self._get_conversation_data(sid, is_active=True)
            if conversation_data:
                ref_count = conversation_data.get('ref_count', 1) - 1
                if ref_count > 0:
                    conversation_data['ref_count'] = ref_count
                    await self._store_conversation_data(sid, conversation_data, is_active=True)
                else:
                    await self._move_conversation_to_detached(sid)

    async def _cleanup_stale_distributed(self):
        """Distributed cleanup process that coordinates with other pods."""
        while should_continue():
            try:
                # Use a distributed lock to ensure only one pod performs cleanup at a time
                cleanup_lock = await self._get_distributed_lock("cleanup", timeout=_CLEANUP_INTERVAL * 2)

                try:
                    if await cleanup_lock.acquire(blocking=False):
                        await self._perform_cleanup()
                except Exception as e:
                    logger.error(f'Error during cleanup: {e}')
                finally:
                    try:
                        await cleanup_lock.release()
                    except:
                        pass  # Lock may have expired

                await asyncio.sleep(_CLEANUP_INTERVAL)
            except asyncio.CancelledError:
                await self._cleanup_pod_sessions()
                return
            except Exception as e:
                logger.error(f'error_cleaning_stale: {e}')
                await asyncio.sleep(_CLEANUP_INTERVAL)

    async def _perform_cleanup(self):
        """Perform the actual cleanup of stale conversations and sessions."""
        current_time = time.time()

        # Clean up detached conversations
        try:
            detached_conversations = await self.redis_client.hgetall(self.DETACHED_CONVERSATIONS_KEY)
            for sid_bytes, data_str in detached_conversations.items():
                sid = sid_bytes.decode() if isinstance(sid_bytes, bytes) else sid_bytes
                data = json.loads(data_str)
                # Remove detached conversations immediately (you might want different logic)
                conversation = await self._create_server_conversation(sid, data.get('user_id'))
                if conversation:
                    await conversation.disconnect()
                await self._remove_conversation_data(sid, is_active=False)
        except Exception as e:
            logger.error(f"Error cleaning detached conversations: {e}")

        # Clean up stale sessions if sandbox close delay is configured
        if self.config.sandbox.close_delay:
            close_threshold = current_time - self.config.sandbox.close_delay
            await self._cleanup_stale_sessions(close_threshold)

    async def _cleanup_stale_sessions(self, close_threshold: float):
        """Clean up sessions that have been inactive for too long."""
        try:
            all_sessions = await self.redis_client.hgetall(self.SESSIONS_KEY)
            sessions_to_close = []

            for sid_bytes, session_data_str in all_sessions.items():
                sid = sid_bytes.decode() if isinstance(sid_bytes, bytes) else sid_bytes
                try:
                    session_data = json.loads(session_data_str)
                    last_active = session_data.get('last_active', 0)

                    if last_active < close_threshold:
                        # Check if session has active connections
                        connections = await self.get_connections(filter_to_sids={sid})
                        if not connections:  # No active connections
                            sessions_to_close.append(sid)
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Error parsing session data for {sid}: {e}")
                    sessions_to_close.append(sid)  # Clean up corrupted data

            # Close stale sessions
            for sid in sessions_to_close:
                try:
                    await self.close_session(sid)
                except Exception as e:
                    logger.error(f"Error closing stale session {sid}: {e}")
        except Exception as e:
            logger.error(f"Error in cleanup_stale_sessions: {e}")

    async def _cleanup_pod_sessions(self):
        """Clean up sessions owned by this pod during shutdown."""
        try:
            all_sessions = await self.redis_client.hgetall(self.SESSIONS_KEY)
            for sid_bytes, session_data_str in all_sessions.items():
                sid = sid_bytes.decode() if isinstance(sid_bytes, bytes) else sid_bytes
                try:
                    session_data = json.loads(session_data_str)
                    if session_data.get('pod_id') == self.pod_id:
                        await self.close_session(sid)
                except Exception as e:
                    logger.error(f"Error cleaning pod session {sid}: {e}")
        except Exception as e:
            logger.error(f"Error in cleanup_pod_sessions: {e}")

    async def get_running_agent_loops(
        self, user_id: str | None = None, filter_to_sids: set[str] | None = None
    ) -> set[str]:
        """Get running session IDs from Redis."""
        try:
            all_sessions = await self.redis_client.hgetall(self.SESSIONS_KEY)
            sids = set()

            for sid_bytes, session_data_str in all_sessions.items():
                sid = sid_bytes.decode() if isinstance(sid_bytes, bytes) else sid_bytes
                try:
                    session_data = json.loads(session_data_str)

                    # Apply filters
                    if filter_to_sids and sid not in filter_to_sids:
                        continue
                    if user_id and session_data.get('user_id') != user_id:
                        continue

                    sids.add(sid)
                except (json.JSONDecodeError, KeyError):
                    continue  # Skip corrupted data

            return sids
        except Exception as e:
            logger.error(f"Error getting running agent loops: {e}")
            return set()

    async def get_connections(
        self, user_id: str | None = None, filter_to_sids: set[str] | None = None
    ) -> dict[str, str]:
        """Get connection mappings from Redis."""
        try:
            all_connections = await self.redis_client.hgetall(self.CONNECTIONS_KEY)
            connections = {}

            for connection_id_bytes, sid_bytes in all_connections.items():
                connection_id = connection_id_bytes.decode() if isinstance(connection_id_bytes, bytes) else connection_id_bytes
                sid = sid_bytes.decode() if isinstance(sid_bytes, bytes) else sid_bytes

                # Apply SID filter
                if filter_to_sids and sid not in filter_to_sids:
                    continue

                # Apply user filter
                if user_id:
                    session_data = await self._get_session_data(sid)
                    if not session_data or session_data.get('user_id') != user_id:
                        continue

                connections[connection_id] = sid

            return connections
        except Exception as e:
            logger.error(f"Error getting connections: {e}")
            return {}

    async def maybe_start_agent_loop(
        self,
        sid: str,
        settings: Settings,
        user_id: str | None,
        initial_user_msg: MessageAction | None = None,
        replay_json: str | None = None,
    ) -> AgentLoopInfo:
        logger.info(f'maybe_start_agent_loop:{sid}', extra={'session_id': sid})

        # Check if session already exists in Redis
        session_data = await self._get_session_data(sid)
        if not session_data:
            # Start new session
            await self._start_agent_loop(
                sid, settings, user_id, initial_user_msg, replay_json
            )
            # Store session data in Redis
            await self._store_session_data(sid, {
                'user_id': user_id,
                'settings': settings.model_dump() if hasattr(settings, 'model_dump') else settings.__dict__,
                'created_at': time.time()
            })

        return AgentLoopInfo(
            conversation_id=sid,
            url=self._get_conversation_url(sid),
            session_api_key=None,
            event_store=None,  # This might need to be handled differently for distributed setup
        )

    async def _start_agent_loop(
        self,
        sid: str,
        settings: Settings,
        user_id: str | None,
        initial_user_msg: MessageAction | None = None,
        replay_json: str | None = None,
    ) -> Session:
        logger.info(f'starting_agent_loop:{sid}', extra={'session_id': sid})

        # Check concurrent session limits
        response_ids = await self.get_running_agent_loops(user_id)
        if len(response_ids) >= self.config.max_concurrent_conversations:
            await self._handle_too_many_sessions(user_id, response_ids)

        # Create session - in distributed setup, this is transient
        session = Session(
            sid=sid,
            file_store=self.file_store,
            config=self.config,
            sio=self.sio,
            user_id=user_id,
        )

        asyncio.create_task(
            session.initialize_agent(settings, initial_user_msg, replay_json)
        )

        # Subscribe to events for conversation updates
        try:
            session.agent_session.event_stream.subscribe(
                EventStreamSubscriber.SERVER,
                self._create_conversation_update_callback(user_id, sid, settings),
                UPDATED_AT_CALLBACK_ID,
            )
        except ValueError:
            pass  # Already subscribed

        return session

    async def _handle_too_many_sessions(self, user_id: str | None, response_ids: set[str]):
        """Handle the case where a user has too many concurrent sessions."""
        logger.info(
            f'too_many_sessions_for:{user_id or ""}',
            extra={'user_id': user_id},
        )

        # Get conversation metadata to determine which sessions to close
        conversation_store = await self._get_conversation_store(user_id)
        conversations = await conversation_store.get_all_metadata(response_ids)
        conversations.sort(key=_last_updated_at_key, reverse=True)

        while len(conversations) >= self.config.max_concurrent_conversations:
            oldest_conversation_id = conversations.pop().conversation_id
            logger.debug(
                f'closing_from_too_many_sessions:{user_id or ""}:{oldest_conversation_id}',
                extra={'session_id': oldest_conversation_id, 'user_id': user_id},
            )

            # Send status message to client
            status_update_dict = {
                'status_update': True,
                'type': 'error',
                'id': 'AGENT_ERROR$TOO_MANY_CONVERSATIONS',
                'message': 'Too many conversations at once. If you are still using this one, try reactivating it by prompting the agent to continue',
            }
            await self.sio.emit(
                'oh_event',
                status_update_dict,
                to=ROOM_KEY.format(sid=oldest_conversation_id),
            )
            await self.close_session(oldest_conversation_id)

    async def send_to_event_stream(self, connection_id: str, data: dict):
        """Send data to event stream. Routes via Socket.IO rooms for distributed setup."""
        sid = await self._get_connection_mapping(connection_id)
        if not sid:
            raise RuntimeError(f'no_connected_session:{connection_id}')

        # In distributed setup, use Socket.IO rooms for event distribution
        # The Redis adapter will handle routing to the correct pod
        await self.sio.emit('stream_event', data, to=ROOM_KEY.format(sid=sid))

    async def disconnect_from_session(self, connection_id: str):
        sid = await self._get_connection_mapping(connection_id)
        await self._remove_connection_mapping(connection_id)
        logger.info(
            f'disconnect_from_session:{connection_id}:{sid}',
            extra={'session_id': sid}
        )

    async def close_session(self, sid: str):
        """Close a session and clean up all related state."""
        logger.info(f'close_session:{sid}', extra={'session_id': sid})

        try:
            # Remove session data from Redis
            await self._remove_session_data(sid)

            # Remove any connection mappings
            all_connections = await self.redis_client.hgetall(self.CONNECTIONS_KEY)
            connections_to_remove = [
                conn_id for conn_id, conn_sid in all_connections.items()
                if (conn_sid.decode() if isinstance(conn_sid, bytes) else conn_sid) == sid
            ]

            for connection_id_bytes in connections_to_remove:
                connection_id = connection_id_bytes.decode() if isinstance(connection_id_bytes, bytes) else connection_id_bytes
                await self._remove_connection_mapping(connection_id)

            # Remove conversation data
            await self._remove_conversation_data(sid, is_active=True)
            await self._remove_conversation_data(sid, is_active=False)
        except Exception as e:
            logger.error(f"Error closing session {sid}: {e}")

    @classmethod
    def get_instance(
        cls,
        sio: socketio.AsyncServer,
        config: OpenHandsConfig,
        file_store: Store,
        server_config: ServerConfig,
        monitoring_listener: MonitoringListener | None,
    ) -> ConversationManager:
        """Factory method that matches the original interface."""
        return cls(
            sio=sio,
            config=config,
            file_store=file_store,
            server_config=server_config,
            monitoring_listener=monitoring_listener or MonitoringListener(),
        )

    async def _get_conversation_store(self, user_id: str | None) -> ConversationStore:
        conversation_store_class = self._conversation_store_class
        if not conversation_store_class:
            self._conversation_store_class = conversation_store_class = get_impl(
                ConversationStore,
                self.server_config.conversation_store_class,
            )
        store = await conversation_store_class.get_instance(self.config, user_id)
        return store

    def _create_conversation_update_callback(
        self,
        user_id: str | None,
        conversation_id: str,
        settings: Settings,
    ) -> Callable:
        def callback(event, *args, **kwargs):
            call_async_from_sync(
                self._update_conversation_for_event,
                GENERAL_TIMEOUT,
                user_id,
                conversation_id,
                settings,
                event,
            )
        return callback

    async def _update_conversation_for_event(
        self,
        user_id: str,
        conversation_id: str,
        settings: Settings,
        event=None,
    ):
        """Update conversation metadata for an event."""
        try:
            conversation_store = await self._get_conversation_store(user_id)
            conversation = await conversation_store.get_metadata(conversation_id)
            conversation.last_updated_at = datetime.now(timezone.utc)

            # Update cost/token metrics if event has llm_metrics
            if event and hasattr(event, 'llm_metrics') and event.llm_metrics:
                metrics = event.llm_metrics

                if hasattr(metrics, 'accumulated_cost'):
                    conversation.accumulated_cost = metrics.accumulated_cost

                if hasattr(metrics, 'accumulated_token_usage'):
                    token_usage = metrics.accumulated_token_usage
                    conversation.prompt_tokens = token_usage.prompt_tokens
                    conversation.completion_tokens = token_usage.completion_tokens
                    conversation.total_tokens = (
                        token_usage.prompt_tokens + token_usage.completion_tokens
                    )

            # Handle title generation
            default_title = get_default_conversation_title(conversation_id)
            if conversation.title == default_title:
                title = await auto_generate_title(
                    conversation_id, user_id, self.file_store, settings
                )
                if title and not title.isspace():
                    conversation.title = title
                    try:
                        status_update_dict = {
                            'status_update': True,
                            'type': 'info',
                            'message': conversation_id,
                            'conversation_title': conversation.title,
                        }
                        await self.sio.emit(
                            'oh_event',
                            status_update_dict,
                            to=ROOM_KEY.format(sid=conversation_id),
                        )
                    except Exception as e:
                        logger.error(f'Error emitting title update event: {e}')
                else:
                    conversation.title = default_title

            await conversation_store.save_metadata(conversation)
        except Exception as e:
            logger.error(f"Error updating conversation for event: {e}")

    async def get_agent_loop_info(
        self, user_id: str | None = None, filter_to_sids: set[str] | None = None
    ):
        """Get agent loop information from distributed state."""
        results = []
        running_sids = await self.get_running_agent_loops(user_id, filter_to_sids)

        for sid in running_sids:
            results.append(AgentLoopInfo(
                conversation_id=sid,
                url=self._get_conversation_url(sid),
                session_api_key=None,
                event_store=None,  # This might need special handling in distributed setup
            ))

        return results

    def _get_conversation_url(self, conversation_id: str):
        return f'/api/conversations/{conversation_id}'


def _last_updated_at_key(conversation: ConversationMetadata) -> float:
    last_updated_at = conversation.last_updated_at
    if last_updated_at is None:
        return 0.0
    return last_updated_at.timestamp()
