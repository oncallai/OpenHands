import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from openhands.core.logger import openhands_logger as logger
from openhands.events.action import NullAction
from openhands.events.action.agent import RecallAction
from openhands.events.async_event_store_wrapper import AsyncEventStoreWrapper
from openhands.events.event_store import EventStore
from openhands.events.observation import NullObservation
from openhands.events.observation.agent import AgentStateChangedObservation
from openhands.events.serialization import event_to_dict
from openhands.integrations.service_types import ProviderType
from openhands.server.dependencies import get_dependencies
from openhands.server.listen_socket import setup_init_convo_settings, _invalid_session_api_key
from openhands.server.shared import conversation_manager, config, file_store, server_config
from openhands.server.monitoring import MonitoringListener
from openhands.server.conversation_manager.sse_standalone_conversation_manager import SSEStandaloneConversationManager
from openhands.server.user_auth import get_user_id
from openhands.storage.conversation.conversation_validator import create_conversation_validator

app = APIRouter(prefix='/api/sse', dependencies=get_dependencies())

# Create global SSE conversation manager
sse_conversation_manager = SSEStandaloneConversationManager(
    config=config,
    file_store=file_store,
    server_config=server_config,
    sio=None,  # SSE doesn't use socketio
    monitoring_listener=MonitoringListener(),
)

class SSEActionRequest(BaseModel):
    action: str
    args: dict[str, Any] = {}

@app.get('/conversations/{conversation_id}/events')
async def stream_conversation_events(
    request: Request,
    conversation_id: str,
    latest_event_id: int = -1,
):
    """Stream events from a conversation using Server-Sent Events (SSE).

    Enhanced version with full SSE session management.
    """
    try:
        logger.info(f'SSE connection requested for conversation {conversation_id}')

        # Extract query parameters
        query_params = dict(request.query_params)
        providers_param = query_params.get('providers_set', '')
        providers_list = [p for p in providers_param.split(',') if p]
        providers_set = [ProviderType(p) for p in providers_list]

        # Parse latest_event_id if provided in URL
        if latest_event_id == -1 and 'latest_event_id' in query_params:
            try:
                latest_event_id = int(query_params['latest_event_id'])
            except ValueError:
                logger.debug(f'Invalid latest_event_id, defaulting to -1')
                latest_event_id = -1

        # Validate inputs
        if not conversation_id:
            raise HTTPException(status_code=400, detail="No conversation_id provided")

        # Check API key
        if _invalid_session_api_key({k: [v] for k, v in query_params.items()}):
            raise HTTPException(status_code=401, detail="Invalid session API key")

        # Extract authentication
        cookies_str = request.headers.get('cookie', '')
        authorization_header = request.headers.get('authorization')

        # Validate conversation access
        conversation_validator = create_conversation_validator()
        user_id = await conversation_validator.validate(
            conversation_id, cookies_str, authorization_header
        )
        logger.info(f'User {user_id} allowed to connect to conversation {conversation_id}')

        # Generate connection ID
        connection_id = f"sse_{uuid.uuid4()}"

        # Initialize conversation settings
        conversation_init_data = await setup_init_convo_settings(user_id, providers_set)

        # Join conversation via SSE manager
        agent_loop_info = await sse_conversation_manager.join_conversation(
            conversation_id,
            connection_id,
            conversation_init_data,
            user_id,
        )

        if agent_loop_info is None:
            raise HTTPException(status_code=404, detail="Failed to join conversation")

        logger.info(f'Connected to conversation {conversation_id} with SSE session')

        # Create enhanced event generator
        async def enhanced_event_generator():
            try:
                # Send connection event
                connection_event = {
                    "type": "connection_established",
                    "connection_id": connection_id,
                    "conversation_id": conversation_id
                }
                yield f"event: connection\ndata: {json.dumps(connection_event)}\n\n"

                # Replay historical events
                agent_state_changed = None
                # Ensure event_store is not None before creating wrapper
                if agent_loop_info.event_store is None:
                    logger.warning(f"Event store is None for {conversation_id}")
                    # No events to replay
                    async_store = None
                else:
                    # Cast the EventStoreABC to EventStore to satisfy type checking
                    # We know this is safe because the implementation meets the requirements
                    event_store = agent_loop_info.event_store
                    if isinstance(event_store, EventStore):
                        async_store = AsyncEventStoreWrapper(
                            event_store, latest_event_id + 1
                        )
                    else:
                        # Type error: Can't use EventStoreABC directly
                        logger.warning(f"Event store type {type(event_store)} not compatible with AsyncEventStoreWrapper")
                        async_store = None

                # Process events only if we have an event store
                if async_store is not None:
                    async for event in async_store:
                        logger.debug(f'SSE replay event: {event.__class__.__name__}')
                        if isinstance(event, (NullAction, NullObservation, RecallAction)):
                            continue
                        elif isinstance(event, AgentStateChangedObservation):
                            agent_state_changed = event
                        else:
                            event_data = event_to_dict(event)
                            yield f"data: {json.dumps(event_data)}\n\n"

                # Send final agent state
                if agent_state_changed:
                    event_data = event_to_dict(agent_state_changed)
                    yield f"data: {json.dumps(event_data)}\n\n"

                logger.info(f'Finished replaying events for conversation {conversation_id}')

                # Get SSE session for real-time streaming
                sse_session = await sse_conversation_manager.get_sse_session(conversation_id)
                if sse_session:
                    # Stream live events from SSE session
                    async for sse_message in sse_session.create_sse_stream(request):
                        yield sse_message
                else:
                    # Fallback: basic heartbeat
                    while True:
                        await asyncio.sleep(30)
                        yield f": heartbeat\n\n"

            except asyncio.CancelledError:
                logger.info(f"SSE connection {connection_id} cancelled")
                await sse_conversation_manager.disconnect_from_session(connection_id)
                yield f"event: close\ndata: Connection closed\n\n"

            except Exception as e:
                logger.exception(f"Error in SSE stream: {str(e)}")
                yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
                try:
                    await sse_conversation_manager.disconnect_from_session(connection_id)
                except:
                    pass

        return StreamingResponse(
            enhanced_event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "Content-Type": "text/event-stream",
                "Transfer-Encoding": "chunked"
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error establishing SSE connection: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post('/conversations/{conversation_id}/actions')
async def send_action_to_conversation(
    request: Request,
    conversation_id: str,
    action_request: SSEActionRequest,
    user_id: str | None = Depends(get_user_id)
):
    """Send an action to a conversation (equivalent to oh_user_action)"""
    try:
        # Validate conversation access
        cookies_str = request.headers.get('cookie', '')
        authorization_header = request.headers.get('authorization')

        conversation_validator = create_conversation_validator()
        validated_user_id = await conversation_validator.validate(
            conversation_id, cookies_str, authorization_header
        )

        # Create action data
        data = {
            "action": action_request.action,
            "args": action_request.args
        }

        # Send via SSE manager - use conversation ID directly
        await sse_conversation_manager.send_to_conversation(conversation_id, data)

        return {"status": "success", "message": "Action sent to conversation"}

    except RuntimeError as e:
        if "no_connected_session" in str(e):
            raise HTTPException(status_code=404, detail="No active session for this conversation")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f'Error sending action to conversation: {e}')
        raise HTTPException(status_code=500, detail=str(e))


@app.get('/conversations/{conversation_id}/status')
async def get_sse_conversation_status(
    conversation_id: str,
    user_id: str | None = Depends(get_user_id)
):
    """Get SSE conversation status"""
    try:
        status = sse_conversation_manager.get_sse_session_status(conversation_id)
        return status
    except Exception as e:
        logger.error(f'Error getting SSE status: {e}')
        raise HTTPException(status_code=500, detail=str(e))


@app.post('/conversations/{conversation_id}/initialize')
async def initialize_sse_conversation(
    conversation_id: str,
    user_id: str | None = Depends(get_user_id)
):
    """Initialize SSE session for existing conversation"""
    try:
        # Verify conversation exists
        from openhands.server.shared import ConversationStoreImpl
        conversation_store = await ConversationStoreImpl.get_instance(config, user_id)

        try:
            metadata = await conversation_store.get_metadata(conversation_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # Get or create SSE session
        sse_session = await sse_conversation_manager.get_sse_session(conversation_id)
        if not sse_session:
            # Load settings and initialize
            from openhands.server.shared import SettingsStoreImpl
            settings_store = await SettingsStoreImpl.get_instance(config, user_id)
            settings = await settings_store.load()

            if not settings:
                raise HTTPException(status_code=400, detail="User settings not found")

            # Start agent loop which creates the session
            await sse_conversation_manager.maybe_start_agent_loop(
                conversation_id, settings, user_id
            )

        return {
            "status": "success",
            "conversation_id": conversation_id,
            "message": "SSE session ready"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize: {str(e)}")


@app.delete('/conversations/{conversation_id}')
async def close_sse_conversation(
    conversation_id: str,
    user_id: str | None = Depends(get_user_id)
):
    """Close SSE session"""
    try:
        await sse_conversation_manager.close_session(conversation_id)
        return {"status": "success", "message": "SSE session closed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to close: {str(e)}")
