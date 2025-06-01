import asyncio
import json
import time
import uuid
from copy import deepcopy
from logging import LoggerAdapter
from typing import Any, Optional, AsyncGenerator

from openhands.controller.agent import Agent
from openhands.core.config import OpenHandsConfig
from openhands.core.config.condenser_config import (
    BrowserOutputCondenserConfig,
    CondenserPipelineConfig,
    LLMSummarizingCondenserConfig,
)
from openhands.core.config.mcp_config import MCPConfig, OpenHandsMCPConfigImpl
from openhands.core.exceptions import MicroagentValidationError
from openhands.core.logger import OpenHandsLoggerAdapter
from openhands.core.schema import AgentState
from openhands.events.action import MessageAction, NullAction
from openhands.events.event import Event, EventSource
from openhands.events.observation import (
    AgentStateChangedObservation,
    CmdOutputObservation,
    NullObservation,
)
from openhands.events.observation.agent import RecallObservation
from openhands.events.observation.error import ErrorObservation
from openhands.events.serialization import event_from_dict, event_to_dict
from openhands.events.stream import EventStreamSubscriber
from openhands.llm.llm import LLM
from openhands.server.session.agent_session import AgentSession
from openhands.server.session.conversation_init_data import ConversationInitData
from openhands.storage.data_models.settings import Settings
from openhands.storage.files import FileStore


class SSEMessage:
    """SSE message format"""
    def __init__(
        self,
        event: Optional[str] = None,
        data: Any = None,
        id: Optional[str] = None,
        retry: Optional[int] = None
    ):
        self.event = event
        self.data = data
        self.id = id
        self.retry = retry


class SSESession:
    """SSE Session - complete parallel to Session class"""

    sid: str
    last_active_ts: int = 0
    is_alive: bool = True
    agent_session: AgentSession
    loop: asyncio.AbstractEventLoop
    config: OpenHandsConfig
    file_store: FileStore
    user_id: str | None
    logger: LoggerAdapter

    def __init__(
        self,
        sid: str,
        config: OpenHandsConfig,
        file_store: FileStore,
        user_id: str | None = None,
    ):
        self.sid = sid
        self.last_active_ts = int(time.time())
        self.file_store = file_store
        self.logger = OpenHandsLoggerAdapter(extra={'session_id': sid})
        self.agent_session = AgentSession(
            sid,
            file_store,
            status_callback=self.queue_status_message,
            user_id=user_id,
        )
        self.agent_session.event_stream.subscribe(
            EventStreamSubscriber.SERVER, self.on_event, self.sid
        )
        # Copying this means that when we update variables they are not applied to the shared global configuration!
        self.config = deepcopy(config)
        self.loop = asyncio.get_event_loop()
        self.user_id = user_id

        # SSE specific properties
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.active_connections = 0
        self.max_connections = 3
        self.heartbeat_interval = 30
        self.last_heartbeat = time.time()

    async def close(self) -> None:
        """Close the SSE session - parallel to Session.close()"""
        # Send final agent state via SSE instead of socketio
        try:
            await self._queue_event(SSEMessage(
                event="oh_event",
                data=event_to_dict(
                    AgentStateChangedObservation('', AgentState.STOPPED.value)
                )
            ))
        except:
            pass

        self.is_alive = False
        await self.agent_session.close()

    async def initialize_agent(
        self,
        settings: Settings,
        initial_message: MessageAction | None,
        replay_json: str | None,
    ) -> None:
        """Initialize agent - exact copy of Session.initialize_agent()"""
        self.agent_session.event_stream.add_event(
            AgentStateChangedObservation('', AgentState.LOADING),
            EventSource.ENVIRONMENT,
        )
        agent_cls = self.config.default_agent or settings.agent

        self.config.security.confirmation_mode = (
            self.config.security.confirmation_mode
            if settings.confirmation_mode is None
            else settings.confirmation_mode
        )
        self.config.security.security_analyzer = (
            settings.security_analyzer or self.config.security.security_analyzer
        )
        self.config.sandbox.base_container_image = (
            settings.sandbox_base_container_image
            or self.config.sandbox.base_container_image
        )
        self.config.sandbox.runtime_container_image = (
            settings.sandbox_runtime_container_image
            if settings.sandbox_base_container_image
            or settings.sandbox_runtime_container_image
            else self.config.sandbox.runtime_container_image
        )
        max_iterations = settings.max_iterations or self.config.max_iterations

        # This is a shallow copy of the default LLM config, so changes here will
        # persist if we retrieve the default LLM config again when constructing
        # the agent
        default_llm_config = self.config.get_llm_config()
        default_llm_config.model = settings.llm_model or ''
        default_llm_config.api_key = settings.llm_api_key
        default_llm_config.base_url = settings.llm_base_url
        self.config.search_api_key = settings.search_api_key

        # NOTE: this need to happen AFTER the config is updated with the search_api_key
        self.config.mcp = settings.mcp_config or MCPConfig(
            sse_servers=[], stdio_servers=[]
        )
        # Add OpenHands' MCP server by default
        openhands_mcp_server, openhands_mcp_stdio_servers = (
            OpenHandsMCPConfigImpl.create_default_mcp_server_config(
                self.config.mcp_host, self.config, self.user_id
            )
        )
        if openhands_mcp_server:
            self.config.mcp.sse_servers.append(openhands_mcp_server)
        self.config.mcp.stdio_servers.extend(openhands_mcp_stdio_servers)

        # TODO: override other LLM config & agent config groups (#2075)

        llm = self._create_llm(agent_cls)
        # Ensure agent_cls is not None before passing to get_agent_config
        agent_name = agent_cls if agent_cls is not None else 'agent'
        agent_config = self.config.get_agent_config(agent_name)

        if settings.enable_default_condenser:
            # Default condenser chains a condenser that limits browser the total
            # size of browser observations with a condenser that limits the size
            # of the view given to the LLM. The order matters: with the browser
            # output first, the summarizer will only see the most recent browser
            # output, which should keep the summarization cost down.
            default_condenser_config = CondenserPipelineConfig(
                condensers=[
                    BrowserOutputCondenserConfig(attention_window=2),
                    LLMSummarizingCondenserConfig(
                        llm_config=llm.config, keep_first=4, max_size=80
                    ),
                ]
            )

            self.logger.info(
                f'Enabling pipeline condenser with:'
                f' browser_output_masking(attention_window=2), '
                f' llm(model="{llm.config.model}", '
                f' base_url="{llm.config.base_url}", '
                f' keep_first=4, max_size=80)'
            )
            agent_config.condenser = default_condenser_config

        # Ensure agent_cls is not None before passing to get_cls
        if agent_cls is None:
            agent_cls = self.config.default_agent
        agent = Agent.get_cls(agent_cls)(llm, agent_config)

        git_provider_tokens = None
        selected_repository = None
        selected_branch = None
        custom_secrets = None
        conversation_instructions = None
        if isinstance(settings, ConversationInitData):
            git_provider_tokens = settings.git_provider_tokens
            selected_repository = settings.selected_repository
            selected_branch = settings.selected_branch
            custom_secrets = settings.custom_secrets
            conversation_instructions = settings.conversation_instructions

        try:
            await self.agent_session.start(
                runtime_name=self.config.runtime,
                config=self.config,
                agent=agent,
                max_iterations=max_iterations,
                max_budget_per_task=self.config.max_budget_per_task,
                agent_to_llm_config=self.config.get_agent_to_llm_config_map(),
                agent_configs=self.config.get_agent_configs(),
                git_provider_tokens=git_provider_tokens,
                custom_secrets=custom_secrets,
                selected_repository=selected_repository,
                selected_branch=selected_branch,
                initial_message=initial_message,
                conversation_instructions=conversation_instructions,
                replay_json=replay_json,
            )
        except MicroagentValidationError as e:
            self.logger.exception(f'Error creating agent_session: {e}')
            # For microagent validation errors, provide more helpful information
            await self.send_error(f'Failed to create agent session: {str(e)}')
            return
        except ValueError as e:
            self.logger.exception(f'Error creating agent_session: {e}')
            error_message = str(e)
            # For ValueError related to microagents, provide more helpful information
            if 'microagent' in error_message.lower():
                await self.send_error(
                    f'Failed to create agent session: {error_message}'
                )
            else:
                # For other ValueErrors, just show the error class
                await self.send_error('Failed to create agent session: ValueError')
            return
        except Exception as e:
            self.logger.exception(f'Error creating agent_session: {e}')
            # For other errors, just show the error class to avoid exposing sensitive information
            await self.send_error(
                f'Failed to create agent session: {e.__class__.__name__}'
            )
            return

    def _create_llm(self, agent_cls: str | None) -> LLM:
        """Initialize LLM, extracted for testing - exact copy from Session"""
        agent_name = agent_cls if agent_cls is not None else 'agent'
        return LLM(
            config=self.config.get_llm_config_from_agent(agent_name),
            retry_listener=self._notify_on_llm_retry,
        )

    def _notify_on_llm_retry(self, retries: int, max: int) -> None:
        """LLM retry notification - exact copy from Session"""
        msg_id = 'STATUS$LLM_RETRY'
        self.queue_status_message(
            'info', msg_id, f'Retrying LLM request, {retries} / {max}'
        )

    def on_event(self, event: Event) -> None:
        """Event handler sync wrapper - exact copy from Session"""
        asyncio.get_event_loop().run_until_complete(self._on_event(event))

    async def _on_event(self, event: Event) -> None:
        """Event handler - adapted from Session._on_event() for SSE"""
        if isinstance(event, NullAction):
            return
        if isinstance(event, NullObservation):
            return
        if event.source == EventSource.AGENT:
            await self._queue_event(SSEMessage(
                event="oh_event",
                data=event_to_dict(event)
            ))
        elif event.source == EventSource.USER:
            await self._queue_event(SSEMessage(
                event="oh_event",
                data=event_to_dict(event)
            ))
        # NOTE: ipython observations are not sent here currently
        elif event.source == EventSource.ENVIRONMENT and isinstance(
            event,
            (CmdOutputObservation, AgentStateChangedObservation, RecallObservation),
        ):
            # feedback from the environment to agent actions is understood as agent events by the UI
            event_dict = event_to_dict(event)
            event_dict['source'] = EventSource.AGENT
            await self._queue_event(SSEMessage(
                event="oh_event",
                data=event_dict
            ))
            if (
                isinstance(event, AgentStateChangedObservation)
                and event.agent_state == AgentState.ERROR
            ):
                self.logger.info(
                    'Agent status error',
                    extra={'signal': 'agent_status_error'},
                )
        elif isinstance(event, ErrorObservation):
            # send error events as agent events to the UI
            event_dict = event_to_dict(event)
            event_dict['source'] = EventSource.AGENT
            await self._queue_event(SSEMessage(
                event="oh_event",
                data=event_dict
            ))

    async def dispatch(self, data: dict) -> None:
        """Dispatch user events - exact copy from Session.dispatch()"""
        event = event_from_dict(data.copy())
        # This checks if the model supports images
        if isinstance(event, MessageAction) and event.image_urls:
            controller = self.agent_session.controller
            if controller:
                if controller.agent.llm.config.disable_vision:
                    await self.send_error(
                        'Support for images is disabled for this model, try without an image.'
                    )
                    return
                if not controller.agent.llm.vision_is_active():
                    await self.send_error(
                        'Model does not support image upload, change to a different model or try without an image.'
                    )
                    return
        self.agent_session.event_stream.add_event(event, EventSource.USER)

    async def send_error(self, message: str) -> None:
        """Send error message via SSE - adapted from Session.send_error()"""
        await self._queue_event(SSEMessage(
            event="oh_event",
            data={'error': True, 'message': message}
        ))

    def queue_status_message(self, msg_type: str, id: str, message: str) -> None:
        """Queue status message - adapted from Session.queue_status_message()"""
        asyncio.run_coroutine_threadsafe(
            self._send_status_message(msg_type, id, message), self.loop
        )

    async def _send_status_message(self, msg_type: str, id: str, message: str) -> None:
        """Send status message via SSE - adapted from Session._send_status_message()"""
        if msg_type == 'error':
            agent_session = self.agent_session
            controller = self.agent_session.controller
            if controller is not None and not agent_session.is_closed():
                await controller.set_agent_state_to(AgentState.ERROR)
            self.logger.info(
                'Agent status error',
                extra={'signal': 'agent_status_error'},
            )
        await self._queue_event(SSEMessage(
            event="oh_event",
            data={'status_update': True, 'type': msg_type, 'id': id, 'message': message}
        ))

    # ============================================================================
    # SSE-specific methods (not in original Session)
    # ============================================================================

    async def create_sse_stream(self, request) -> AsyncGenerator[str, None]:
        """Create SSE stream generator"""
        if self.active_connections >= self.max_connections:
            raise RuntimeError(f"Too many connections for session {self.sid}")

        self.active_connections += 1
        client_ip = getattr(request.client, 'host', 'unknown') if hasattr(request, 'client') else 'unknown'
        self.logger.info(f"New SSE connection from {client_ip}")

        try:
            # Send initial connection event
            yield self._format_sse_message(SSEMessage(
                event="connected",
                data={"session_id": self.sid, "timestamp": time.time()},
                id=str(uuid.uuid4())
            ))

            while self.is_alive:
                try:
                    # Check if client disconnected (FastAPI specific)
                    if hasattr(request, 'is_disconnected'):
                        if await request.is_disconnected():
                            self.logger.info("Client disconnected")
                            break

                    try:
                        message = await asyncio.wait_for(
                            self.event_queue.get(),
                            timeout=self.heartbeat_interval
                        )
                        yield self._format_sse_message(message)

                        if message.event == "close":
                            break

                    except asyncio.TimeoutError:
                        # Send heartbeat
                        if time.time() - self.last_heartbeat >= self.heartbeat_interval:
                            yield self._format_sse_message(SSEMessage(
                                event="heartbeat",
                                data={"timestamp": time.time()}
                            ))
                            self.last_heartbeat = time.time()

                except asyncio.CancelledError:
                    self.logger.info("SSE stream cancelled")
                    break
                except Exception as e:
                    self.logger.error(f"Error in SSE stream: {e}")
                    yield self._format_sse_message(SSEMessage(
                        event="error",
                        data={"message": "Internal server error"}
                    ))
                    break

        finally:
            self.active_connections -= 1
            self.logger.info(f"SSE connection closed. Active: {self.active_connections}")

    def _format_sse_message(self, message: SSEMessage) -> str:
        """Format message as SSE"""
        lines = []

        if message.id:
            lines.append(f"id: {message.id}")

        if message.event:
            lines.append(f"event: {message.event}")

        if message.retry:
            lines.append(f"retry: {message.retry}")

        # Handle data
        if message.data is None:
            data = ""
        elif isinstance(message.data, str):
            data = message.data
        else:
            data = json.dumps(message.data, ensure_ascii=False)
            
        # Split multiline data
        for line in data.split('\n'):
            lines.append(f"data: {line}")

        lines.append("")  # Empty line to end message
        return "\n".join(lines) + "\n"

    async def _queue_event(self, message: SSEMessage) -> bool:
        """Queue an event for SSE transmission"""
        if not self.is_alive:
            return False

        try:
            self.event_queue.put_nowait(message)
            self.last_active_ts = int(time.time())
            return True
        except asyncio.QueueFull:
            self.logger.warning("Event queue full, dropping message")
            return False
