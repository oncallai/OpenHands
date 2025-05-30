"""SSE endpoint for streaming events from a conversation."""

import asyncio
import json
import uuid
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from openhands.core.logger import openhands_logger as logger
from openhands.events.action import NullAction
from openhands.events.action.agent import RecallAction
from openhands.events.async_event_store_wrapper import AsyncEventStoreWrapper
from openhands.events.observation import NullObservation
from openhands.events.observation.agent import AgentStateChangedObservation
from openhands.events.serialization import event_to_dict
from openhands.integrations.service_types import ProviderType
from openhands.server.dependencies import get_dependencies
from openhands.server.listen_socket import setup_init_convo_settings
from openhands.server.shared import conversation_manager
from openhands.storage.conversation.conversation_validator import create_conversation_validator

app = APIRouter(prefix='/api/sse', dependencies=get_dependencies())


@app.get('/conversations/{conversation_id}/events')
async def stream_conversation_events(
    request: Request,
    conversation_id: str,
    latest_event_id: int = -1,
):
    """Stream events from a conversation using Server-Sent Events (SSE).
    
    This endpoint establishes a persistent connection and streams events
    as they occur in the conversation's event store.
    
    Args:
        request: The FastAPI request object
        conversation_id: ID of the conversation to stream events from
        latest_event_id: Optional starting event ID, defaults to -1 (from beginning)
        
    Returns:
        StreamingResponse: An SSE stream of events
    """
    try:
        logger.info(f'SSE connection requested for conversation {conversation_id}')
        
        # Extract query parameters
        query_params = dict(request.query_params)
        providers_param = query_params.get('providers_set', '')
        providers_list = [p for p in providers_param.split(',') if p]
        providers_set = [ProviderType(p) for p in providers_list]
        
        # Parse latest_event_id if it was provided in the URL rather than as a parameter
        if latest_event_id == -1 and 'latest_event_id' in query_params:
            try:
                latest_event_id = int(query_params['latest_event_id'])
            except ValueError:
                logger.debug(
                    f'Invalid latest_event_id value: {query_params["latest_event_id"]}, defaulting to -1'
                )
                latest_event_id = -1
        
        # Check for valid conversation ID
        if not conversation_id:
            logger.error('No conversation_id provided')
            raise HTTPException(status_code=400, detail="No conversation_id provided")
        
        # Check for API key (reusing the socket code's validation function)
        from openhands.server.listen_socket import _invalid_session_api_key
        if _invalid_session_api_key({k: [v] for k, v in query_params.items()}):
            raise HTTPException(status_code=401, detail="Invalid session API key")
        
        # Extract authentication information
        cookies_str = request.headers.get('cookie', '')
        authorization_header = request.headers.get('authorization')
        
        # Validate conversation access
        conversation_validator = create_conversation_validator()
        user_id = await conversation_validator.validate(
            conversation_id, cookies_str, authorization_header
        )
        logger.info(
            f'User {user_id} is allowed to connect to conversation {conversation_id}'
        )
        
        # Generate a unique connection ID for this SSE connection
        connection_id = f"sse_{uuid.uuid4()}"
        
        # Initialize conversation
        conversation_init_data = await setup_init_convo_settings(user_id, providers_set)
        agent_loop_info = await conversation_manager.join_conversation(
            conversation_id,
            connection_id,
            conversation_init_data,
            user_id,
        )
        
        if agent_loop_info is None:
            raise HTTPException(status_code=404, detail="Failed to join conversation")
        
        logger.info(
            f'Connected to conversation {conversation_id} with connection_id {connection_id}. Streaming events...'
        )
        
        # Create event generator for SSE streaming
        async def event_generator():
            try:
                # First, send a connection established event
                connection_event = {
                    "type": "connection_established",
                    "connection_id": connection_id
                }
                yield f"event: connection\ndata: {json.dumps(connection_event)}\n\n"
                
                # Stream events from the event store
                agent_state_changed = None
                async_store = AsyncEventStoreWrapper(
                    agent_loop_info.event_store, latest_event_id + 1
                )
                
                # Main event streaming loop - reuse logic from listen_socket.py
                async for event in async_store:
                    logger.debug(f'SSE event: {event.__class__.__name__}')
                    if isinstance(
                        event,
                        (NullAction, NullObservation, RecallAction),
                    ):
                        continue
                    elif isinstance(event, AgentStateChangedObservation):
                        agent_state_changed = event
                    else:
                        event_data = event_to_dict(event)
                        yield f"data: {json.dumps(event_data)}\n\n"
                
                # Send agent state changed event last if it exists
                if agent_state_changed:
                    event_data = event_to_dict(agent_state_changed)
                    yield f"data: {json.dumps(event_data)}\n\n"
                
                logger.info(
                    f'Finished streaming events for conversation {conversation_id}'
                )
                
                # Keep connection open to stream future events
                while True:
                    await asyncio.sleep(30)  # Send heartbeat every 30 seconds
                    yield f": heartbeat\n\n"  # SSE comment format for heartbeat
                    
            except asyncio.CancelledError:
                logger.info(f"SSE connection {connection_id} cancelled")
                await conversation_manager.disconnect_from_session(connection_id)
                yield f"event: close\ndata: Connection closed\n\n"
                
            except Exception as e:
                logger.exception(f"Error in SSE stream: {str(e)}")
                yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
                await conversation_manager.disconnect_from_session(connection_id)
        
        # Return streaming response
        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable buffering in Nginx
            }
        )
        
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
        
    except Exception as e:
        logger.exception(f"Error establishing SSE connection: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
