import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from datetime import datetime, timezone

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
from openhands.server.session.sse_session import SSESession, SSEMessage  # Import SSE session instead
from openhands.storage.conversation.conversation_store import ConversationStore
from openhands.storage.data_models.conversation_metadata import ConversationMetadata
from openhands.storage.data_models.settings import Settings
from openhands.storage.store import Store
from openhands.utils.async_utils import GENERAL_TIMEOUT, call_async_from_sync, wait_all
from openhands.utils.conversation_summary import (
    auto_generate_title,
    get_default_conversation_title,
)
from openhands.utils.import_utils import get_impl
from openhands.utils.shutdown_listener import should_continue

from .conversation_manager import ConversationManager

_CLEANUP_INTERVAL = 15
UPDATED_AT_CALLBACK_ID = 'updated_at_callback_id'


@dataclass
class SSEStandaloneConversationManager(ConversationManager):
    """Manages conversations in standalone mode (single server instance) with SSE support."""

    config: OpenHandsConfig
    file_store: Store
    server_config: ServerConfig
    # Fields below have default values
    sio: Any = None  # SSE doesn't use socketio but parameter is required for compatibility
    # Defaulting monitoring_listener for temp backward compatibility.
    monitoring_listener: MonitoringListener = MonitoringListener()
    _local_agent_loops_by_sid: dict[str, SSESession] = field(default_factory=dict)  # Changed to SSESession
    _local_connection_id_to_session_id: dict[str, str] = field(default_factory=dict)
    _active_conversations: dict[str, tuple[ServerConversation, int]] = field(
        default_factory=dict
    )
    _detached_conversations: dict[str, tuple[ServerConversation, float]] = field(
        default_factory=dict
    )
    _conversations_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _cleanup_task: asyncio.Task | None = None
    _conversation_store_class: type[ConversationStore] | None = None

    async def __aenter__(self):
        self._cleanup_task = asyncio.create_task(self._cleanup_stale())
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None

    async def attach_to_conversation(
        self, sid: str, user_id: str | None = None
    ) -> ServerConversation | None:
        start_time = time.time()
        if not await session_exists(sid, self.file_store, user_id=user_id):
            return None

        async with self._conversations_lock:
            # Check if we have an active conversation we can reuse
            if sid in self._active_conversations:
                conversation, count = self._active_conversations[sid]
                self._active_conversations[sid] = (conversation, count + 1)
                logger.info(
                    f'Reusing active conversation {sid}', extra={'session_id': sid}
                )
                return conversation

            # Check if we have a detached conversation we can reuse
            if sid in self._detached_conversations:
                conversation, _ = self._detached_conversations.pop(sid)
                self._active_conversations[sid] = (conversation, 1)
                logger.info(
                    f'Reusing detached conversation {sid}', extra={'session_id': sid}
                )
                return conversation

            # Create new conversation if none exists
            c = ServerConversation(
                sid, file_store=self.file_store, config=self.config, user_id=user_id
            )
            try:
                await c.connect()
            except AgentRuntimeUnavailableError as e:
                logger.error(
                    f'Error connecting to conversation {c.sid}: {e}',
                    extra={'session_id': sid},
                )
                await c.disconnect()
                return None
            end_time = time.time()
            logger.info(
                f'ServerConversation {c.sid} connected in {end_time - start_time} seconds'
            )
            self._active_conversations[sid] = (c, 1)
            return c

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
        # Note: No socketio room joining for SSE, just track connection
        self._local_connection_id_to_session_id[connection_id] = sid
        agent_loop_info = await self.maybe_start_agent_loop(sid, settings, user_id)
        return agent_loop_info

    async def detach_from_conversation(self, conversation: ServerConversation):
        sid = conversation.sid
        async with self._conversations_lock:
            if sid in self._active_conversations:
                conv, count = self._active_conversations[sid]
                if count > 1:
                    self._active_conversations[sid] = (conv, count - 1)
                    return
                else:
                    self._active_conversations.pop(sid)
                    self._detached_conversations[sid] = (conversation, time.time())

    async def _cleanup_stale(self):
        while should_continue():
            try:
                async with self._conversations_lock:
                    # Create a list of items to process to avoid modifying dict during iteration
                    items = list(self._detached_conversations.items())
                    for sid, (conversation, detach_time) in items:
                        await conversation.disconnect()
                        self._detached_conversations.pop(sid, None)

                close_threshold = time.time() - self.config.sandbox.close_delay
                running_loops = list(self._local_agent_loops_by_sid.items())
                running_loops.sort(key=lambda item: item[1].last_active_ts)
                sid_to_close: list[str] = []
                for sid, session in running_loops:
                    state = session.agent_session.get_state()
                    if session.last_active_ts < close_threshold and state not in [
                        AgentState.RUNNING,
                        None,
                    ]:
                        sid_to_close.append(sid)

                connections = await self.get_connections(
                    filter_to_sids=set(sid_to_close)  # get_connections expects a set
                )
                connected_sids = {sid for _, sid in connections.items()}
                sid_to_close = [
                    sid for sid in sid_to_close if sid not in connected_sids
                ]
                await wait_all(
                    (self._close_session(sid) for sid in sid_to_close),
                    timeout=WAIT_TIME_BEFORE_CLOSE,
                )
                await asyncio.sleep(_CLEANUP_INTERVAL)
            except asyncio.CancelledError:
                async with self._conversations_lock:
                    for conversation, _ in self._detached_conversations.values():
                        await conversation.disconnect()
                    self._detached_conversations.clear()
                await wait_all(
                    self._close_session(sid) for sid in self._local_agent_loops_by_sid
                )
                return
            except Exception:
                logger.error('error_cleaning_stale')
                await asyncio.sleep(_CLEANUP_INTERVAL)

    async def _get_conversation_store(self, user_id: str | None) -> ConversationStore:
        conversation_store_class = self._conversation_store_class
        if not conversation_store_class:
            self._conversation_store_class = conversation_store_class = get_impl(
                ConversationStore,
                self.server_config.conversation_store_class,
            )
        store = await conversation_store_class.get_instance(self.config, user_id)
        return store

    async def get_running_agent_loops(
        self, user_id: str | None = None, filter_to_sids: set[str] | None = None
    ) -> set[str]:
        """Get the running session ids in chronological order (oldest first).

        If a user is supplied, then the results are limited to session ids for that user.
        If a set of filter_to_sids is supplied, then results are limited to these ids of interest.

        Returns:
            A set of session IDs
        """
        # Get all items and convert to list for sorting
        items: Iterable[tuple[str, SSESession]] = self._local_agent_loops_by_sid.items()

        # Filter items if needed
        if filter_to_sids is not None:
            items = (item for item in items if item[0] in filter_to_sids)
        if user_id:
            items = (item for item in items if item[1].user_id == user_id)

        sids = {sid for sid, _ in items}
        return sids

    async def get_connections(
        self, user_id: str | None = None, filter_to_sids: set[str] | None = None
    ) -> dict[str, str]:
        connections = dict(**self._local_connection_id_to_session_id)
        if filter_to_sids is not None:
            connections = {
                connection_id: sid
                for connection_id, sid in connections.items()
                if sid in filter_to_sids
            }
        if user_id:
            for connection_id, sid in list(connections.items()):
                session = self._local_agent_loops_by_sid.get(sid)
                if not session or session.user_id != user_id:
                    connections.pop(connection_id)
        return connections

    async def maybe_start_agent_loop(
        self,
        sid: str,
        settings: Settings,
        user_id: str | None,
        initial_user_msg: MessageAction | None = None,
        replay_json: str | None = None,
    ) -> AgentLoopInfo:
        logger.info(f'maybe_start_agent_loop:{sid}', extra={'session_id': sid})
        session = self._local_agent_loops_by_sid.get(sid)
        if not session:
            session = await self._start_agent_loop(
                sid, settings, user_id, initial_user_msg, replay_json
            )
        return self._agent_loop_info_from_session(session)

    async def _start_agent_loop(
        self,
        sid: str,
        settings: Settings,
        user_id: str | None,
        initial_user_msg: MessageAction | None = None,
        replay_json: str | None = None,
    ) -> SSESession:
        logger.info(f'starting_agent_loop:{sid}', extra={'session_id': sid})

        response_ids = await self.get_running_agent_loops(user_id)
        if len(response_ids) >= self.config.max_concurrent_conversations:
            logger.info(
                f'too_many_sessions_for:{user_id or ""}',
                extra={'session_id': sid, 'user_id': user_id},
            )
            # Get the conversations sorted (oldest first)
            conversation_store = await self._get_conversation_store(user_id)
            conversations = await conversation_store.get_all_metadata(response_ids)
            conversations.sort(key=_last_updated_at_key, reverse=True)

            while len(conversations) >= self.config.max_concurrent_conversations:
                oldest_conversation_id = conversations.pop().conversation_id
                logger.debug(
                    f'closing_from_too_many_sessions:{user_id or ""}:{oldest_conversation_id}',
                    extra={'session_id': oldest_conversation_id, 'user_id': user_id},
                )
                # Send status message to SSE session and close it
                session_to_close = self._local_agent_loops_by_sid.get(oldest_conversation_id)
                if session_to_close:
                    await session_to_close.send_error(
                        'Too many conversations at once. If you are still using this one, try reactivating it by prompting the agent to continue'
                    )
                await self.close_session(oldest_conversation_id)

        # Create SSE session instead of regular Session
        session = SSESession(
            sid=sid,
            file_store=self.file_store,
            config=self.config,
            user_id=user_id,
        )
        self._local_agent_loops_by_sid[sid] = session
        asyncio.create_task(
            session.initialize_agent(settings, initial_user_msg, replay_json)
        )
        # This does not get added when resuming an existing conversation
        try:
            session.agent_session.event_stream.subscribe(
                EventStreamSubscriber.SERVER,
                self._create_conversation_update_callback(user_id, sid, settings),
                UPDATED_AT_CALLBACK_ID,
            )
        except ValueError:
            pass  # Already subscribed - take no action
        return session

    async def send_to_event_stream(self, connection_id: str, data: dict):
        # If there is a local session running, send to that
        sid = self._local_connection_id_to_session_id.get(connection_id)
        if not sid:
            raise RuntimeError(f'no_connected_session:{connection_id}')

        session = self._local_agent_loops_by_sid.get(sid)
        if session:
            await session.dispatch(data)
            return

        raise RuntimeError(f'no_connected_session:{connection_id}:{sid}')

    async def send_to_conversation(self, conversation_id: str, data: dict):
        """Send data directly to a conversation using its ID.

        This bypasses the connection_id lookup and sends directly to the session
        associated with the conversation ID.

        Args:
            conversation_id: The conversation ID
            data: The data to send

        Raises:
            RuntimeError: If no active session exists for this conversation
        """
        # Get the session directly by conversation ID
        session = self._local_agent_loops_by_sid.get(conversation_id)

        if not session:
            raise RuntimeError(f'No active session for this conversation')

        # Check if session is fully initialized and ready for messages
        if not getattr(session, '_initialization_complete', False):
            raise RuntimeError(f'Session exists but is not fully initialized')

        # Dispatch the message
        await session.dispatch(data)
        return

    async def disconnect_from_session(self, connection_id: str):
        sid = self._local_connection_id_to_session_id.pop(connection_id, None)
        logger.info(
            f'disconnect_from_session:{connection_id}:{sid}', extra={'session_id': sid}
        )
        if not sid:
            # This can occur if the init action was never run.
            logger.warning(
                f'disconnect_from_uninitialized_session:{connection_id}',
                extra={'session_id': sid},
            )
            return

    async def close_session(self, sid: str):
        session = self._local_agent_loops_by_sid.get(sid)
        if session:
            await self._close_session(sid)

    async def _close_session(self, sid: str):
        logger.info(f'_close_session:{sid}', extra={'session_id': sid})

        # Clear up local variables
        connection_ids_to_remove = list(
            connection_id
            for connection_id, conn_sid in self._local_connection_id_to_session_id.items()
            if sid == conn_sid
        )
        logger.info(
            f'removing connections: {connection_ids_to_remove}',
            extra={'session_id': sid},
        )
        for connection_id in connection_ids_to_remove:
            self._local_connection_id_to_session_id.pop(connection_id, None)

        session = self._local_agent_loops_by_sid.pop(sid, None)
        if not session:
            logger.warning(f'no_session_to_close:{sid}', extra={'session_id': sid})
            return

        logger.info(f'closing_session:{session.sid}', extra={'session_id': sid})
        await session.close()
        logger.info(f'closed_session:{session.sid}', extra={'session_id': sid})

    @classmethod
    def get_instance(
        cls,
        sio: Any, # Added to match parent signature, but not used for SSE
        config: OpenHandsConfig,
        file_store: Store,
        server_config: ServerConfig,
        monitoring_listener: MonitoringListener,
    ) -> ConversationManager:
        # SSE doesn't use socketio but we keep the parameter for signature compatibility
        return SSEStandaloneConversationManager(
            config=config,
            file_store=file_store,
            server_config=server_config,
            sio=sio,  # Pass sio but it won't be used
            monitoring_listener=monitoring_listener,
        )

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
        conversation_store = await self._get_conversation_store(user_id)
        conversation = await conversation_store.get_metadata(conversation_id)
        conversation.last_updated_at = datetime.now(timezone.utc)

        # Update cost/token metrics if event has llm_metrics
        if event and hasattr(event, 'llm_metrics') and event.llm_metrics:
            metrics = event.llm_metrics

            # Update accumulated cost
            if hasattr(metrics, 'accumulated_cost'):
                conversation.accumulated_cost = metrics.accumulated_cost

            # Update token usage
            if hasattr(metrics, 'accumulated_token_usage'):
                token_usage = metrics.accumulated_token_usage
                conversation.prompt_tokens = token_usage.prompt_tokens
                conversation.completion_tokens = token_usage.completion_tokens
                conversation.total_tokens = (
                    token_usage.prompt_tokens + token_usage.completion_tokens
                )
        default_title = get_default_conversation_title(conversation_id)
        if (
            conversation.title == default_title
        ):  # attempt to autogenerate if default title is in use
            title = await auto_generate_title(
                conversation_id, user_id, self.file_store, settings
            )
            if title and not title.isspace():
                conversation.title = title
                try:
                    # Send title update via SSE session instead of socketio
                    session = self._local_agent_loops_by_sid.get(conversation_id)
                    if session:
                        await session._queue_event(SSEMessage(
                            event="oh_event",
                            data={
                                'status_update': True,
                                'type': 'info',
                                'message': conversation_id,
                                'conversation_title': conversation.title,
                            }
                        ))
                except Exception as e:
                    logger.error(f'Error emitting title update event: {e}')
            else:
                conversation.title = default_title

        await conversation_store.save_metadata(conversation)

    async def get_agent_loop_info(
        self, user_id: str | None = None, filter_to_sids: set[str] | None = None
    ):
        results = []
        for session in self._local_agent_loops_by_sid.values():
            if user_id and session.user_id != user_id:
                continue
            if filter_to_sids and session.sid not in filter_to_sids:
                continue
            results.append(self._agent_loop_info_from_session(session))
        return results

    def _agent_loop_info_from_session(self, session: SSESession):
        return AgentLoopInfo(
            conversation_id=session.sid,
            url=self._get_conversation_url(session.sid),
            session_api_key=None,
            event_store=session.agent_session.event_stream,
        )

    def _get_conversation_url(self, conversation_id: str):
        return f'/api/sse/conversations/{conversation_id}'  # Updated URL for SSE

    # ============================================================================
    # SSE-specific methods
    # ============================================================================

    async def get_sse_session(self, sid: str) -> SSESession | None:
        """Get SSE session by ID"""
        return self._local_agent_loops_by_sid.get(sid)

    async def create_sse_stream(self, sid: str, request):
        """Create SSE stream for a conversation"""
        session = self._local_agent_loops_by_sid.get(sid)
        if not session:
            raise RuntimeError(f'No SSE session found for conversation {sid}')

        return session.create_sse_stream(request)

    async def dispatch_to_sse_session(self, sid: str, data: dict):
        """Dispatch data to SSE session"""
        session = self._local_agent_loops_by_sid.get(sid)
        if not session:
            raise RuntimeError(f'No SSE session found for conversation {sid}')

        await session.dispatch(data)

    def get_sse_session_status(self, sid: str) -> dict:
        """Get SSE session status"""
        session = self._local_agent_loops_by_sid.get(sid)
        if not session:
            return {
                'active': False,
                'session_exists': False,
                'connections': 0,
                'last_active': None,
                'agent_state': None,
                'ready_for_messages': False
            }

        # Check if the session is fully initialized and the Docker container is ready
        docker_ready = False
        agent_state = None
        ready_for_messages = False

        if hasattr(session, '_initialization_complete') and session._initialization_complete:
            ready_for_messages = True

        if hasattr(session, '_agent_state'):
            agent_state = session._agent_state
            # If the agent is awaiting input or other active states, it's ready
            if agent_state in ['awaiting_user_input', 'ready', 'READY']:
                ready_for_messages = True

        if hasattr(session, '_agent_controller') and session._agent_controller:
            if hasattr(session._agent_controller, 'docker_container') and session._agent_controller.docker_container:
                docker_ready = True

        return {
            'active': session.is_alive,
            'session_exists': True,
            'connections': session.active_connections,
            'last_active': session.last_active_ts,
            'queue_size': session.event_queue.qsize() if session.event_queue else 0,
            'agent_state': agent_state,
            'docker_ready': docker_ready,
            'ready_for_messages': ready_for_messages
        }


def _last_updated_at_key(conversation: ConversationMetadata) -> float:
    last_updated_at = conversation.last_updated_at
    if last_updated_at is None:
        return 0.0
    return last_updated_at.timestamp()
