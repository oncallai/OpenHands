import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

import pytest

from openhands.core.config import OpenHandsConfig
from openhands.events.event import Event
from openhands.events.observation import NullObservation
from openhands.server.config.server_config import ServerConfig
from openhands.server.conversation_manager.sse_standalone_conversation_manager import (
    SSEStandaloneConversationManager,
)
from openhands.server.monitoring import MonitoringListener
from openhands.server.session.sse_session import SSEMessage, SSESession
from openhands.storage.files import FileStore


class TestSSESession(unittest.TestCase):
    """Test SSE session functionality."""

    def setUp(self):
        """Set up test environment."""
        self.config = MagicMock(spec=OpenHandsConfig)
        self.file_store = MagicMock(spec=FileStore)
        self.sid = 'test_session_123'
        self.user_id = 'test_user_123'

        # Create SSE session
        self.sse_session = SSESession(
            sid=self.sid,
            config=self.config,
            file_store=self.file_store,
            user_id=self.user_id,
        )

    def tearDown(self):
        """Clean up resources."""
        # Cancel any pending tasks
        loop = asyncio.get_event_loop()
        tasks = asyncio.all_tasks(loop)
        for task in tasks:
            task.cancel()

    async def test_queue_event(self):
        """Test queuing events for SSE transmission."""
        # Create a test message
        test_message = SSEMessage(event='test_event', data={'key': 'value'}, id='123')

        # Queue the event
        result = await self.sse_session._queue_event(test_message)

        # Verify result
        self.assertTrue(result)

        # Verify message was added to the queue
        self.assertEqual(self.sse_session.event_queue.qsize(), 1)

        # Get the message from queue and verify content
        queued_message = await self.sse_session.event_queue.get()
        self.assertEqual(queued_message.event, 'test_event')
        self.assertEqual(queued_message.data, {'key': 'value'})
        self.assertEqual(queued_message.id, '123')

    def test_format_sse_message(self):
        """Test formatting of SSE messages."""
        # Test with simple data
        message = SSEMessage(
            event='test_event', data={'key': 'value'}, id='123', retry=3000
        )

        formatted = self.sse_session._format_sse_message(message)
        expected_lines = [
            'id: 123',
            'event: test_event',
            'retry: 3000',
            'data: {"key": "value"}',
            '',
        ]
        expected = '\n'.join(expected_lines) + '\n'
        self.assertEqual(formatted, expected)

        # Test with string data
        message = SSEMessage(event='test_event', data='Test string data', id='123')

        formatted = self.sse_session._format_sse_message(message)
        expected_lines = ['id: 123', 'event: test_event', 'data: Test string data', '']
        expected = '\n'.join(expected_lines) + '\n'
        self.assertEqual(formatted, expected)

        # Test with multiline data
        message = SSEMessage(
            event='test_event', data='Line 1\nLine 2\nLine 3', id='123'
        )

        formatted = self.sse_session._format_sse_message(message)
        expected_lines = [
            'id: 123',
            'event: test_event',
            'data: Line 1',
            'data: Line 2',
            'data: Line 3',
            '',
        ]
        expected = '\n'.join(expected_lines) + '\n'
        self.assertEqual(formatted, expected)

        # Test with None data
        message = SSEMessage(event='test_event', data=None, id='123')

        formatted = self.sse_session._format_sse_message(message)
        expected_lines = ['id: 123', 'event: test_event', 'data: ', '']
        expected = '\n'.join(expected_lines) + '\n'
        self.assertEqual(formatted, expected)

    @pytest.mark.asyncio
    async def test_on_event(self):
        """Test event handling in SSE session."""
        # Mock the _queue_event method
        self.sse_session._queue_event = AsyncMock(return_value=True)

        # Create a test event
        test_event = Event(NullObservation(), source='test')

        # Call on_event
        self.sse_session.on_event(test_event)

        # Check that _queue_event was called with the right event
        await asyncio.sleep(0.1)  # Allow async call to complete
        self.sse_session._queue_event.assert_called_once()
        call_args = self.sse_session._queue_event.call_args[0][0]
        self.assertEqual(call_args.event, 'oh_event')
        self.assertIn('data', call_args.data)


class TestSSEStandaloneConversationManager(unittest.TestCase):
    """Test SSE standalone conversation manager functionality."""

    def setUp(self):
        """Set up test environment."""
        self.config = MagicMock(spec=OpenHandsConfig)
        self.file_store = MagicMock(spec=FileStore)
        self.server_config = MagicMock(spec=ServerConfig)
        self.monitoring_listener = MagicMock(spec=MonitoringListener)

        # Create conversation manager
        self.manager = SSEStandaloneConversationManager(
            config=self.config,
            file_store=self.file_store,
            server_config=self.server_config,
            sio=None,
            monitoring_listener=self.monitoring_listener,
        )

        # Test session ID
        self.sid = 'test_conversation_123'
        self.user_id = 'test_user_123'
        self.connection_id = 'test_connection_123'

    @pytest.mark.asyncio
    async def test_get_sse_session(self):
        """Test getting an SSE session."""
        # Mock session creation
        test_session = MagicMock(spec=SSESession)
        self.manager._local_agent_loops_by_sid[self.sid] = test_session

        # Get the session
        session = await self.manager.get_sse_session(self.sid)

        # Verify
        self.assertEqual(session, test_session)

        # Test non-existent session
        session = await self.manager.get_sse_session('non_existent_id')
        self.assertIsNone(session)

    @pytest.mark.asyncio
    async def test_dispatch_to_sse_session(self):
        """Test dispatching data to an SSE session."""
        # Create mock session with dispatch method
        mock_session = AsyncMock(spec=SSESession)
        self.manager._local_agent_loops_by_sid[self.sid] = mock_session

        # Test data
        test_data = {'action': 'test_action', 'args': {'key': 'value'}}

        # Dispatch data
        await self.manager.dispatch_to_sse_session(self.sid, test_data)

        # Verify dispatch was called
        mock_session.dispatch.assert_called_once_with(test_data)

        # Test with non-existent session
        with self.assertRaises(RuntimeError):
            await self.manager.dispatch_to_sse_session('non_existent_id', test_data)

    def test_get_sse_session_status(self):
        """Test getting SSE session status."""
        # Create mock session
        mock_session = MagicMock(spec=SSESession)
        mock_session.is_alive = True
        mock_session.active_connections = 2
        mock_session.last_active_ts = int(time.time())
        mock_session.event_queue = MagicMock()
        mock_session.event_queue.qsize.return_value = 5

        self.manager._local_agent_loops_by_sid[self.sid] = mock_session

        # Get status
        status = self.manager.get_sse_session_status(self.sid)

        # Verify status
        self.assertTrue(status['active'])
        self.assertTrue(status['session_exists'])
        self.assertEqual(status['connections'], 2)
        self.assertEqual(status['queue_size'], 5)

        # Test with non-existent session
        status = self.manager.get_sse_session_status('non_existent_id')
        self.assertFalse(status['active'])
        self.assertFalse(status['session_exists'])


class TestSSERoutes:
    """Test SSE route handlers."""

    @pytest.mark.asyncio
    async def test_stream_conversation_events(self):
        """Test streaming conversation events."""
        # This test requires more complex setup with FastAPI
        # Implement more detailed testing here for the FastAPI routes
        pass


# Additional integration test that can be run against a live server
class TestSSEIntegration:
    """Integration tests for SSE functionality."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_sse_flow(self):
        """Test full SSE communication flow."""
        # Skip if not running integration tests
        pytest.skip('Integration test - requires running server')

        import aiohttp
        import sseclient

        # Base URL for server
        BASE_URL = 'http://localhost:8000'

        # Create a new conversation
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'{BASE_URL}/api/conversations',
                json={'initial_user_msg': 'Hello, this is a test conversation'},
            ) as response:
                response_data = await response.json()
                conversation_id = response_data['conversation_id']
                print(f'Created conversation: {conversation_id}')

            # Initialize SSE session
            async with session.post(
                f'{BASE_URL}/api/sse/conversations/{conversation_id}/initialize'
            ) as response:
                init_data = await response.json()
                print(f'Initialized SSE session: {init_data}')

            # Function to listen for events
            async def listen_for_events():
                headers = {'Accept': 'text/event-stream'}
                async with session.get(
                    f'{BASE_URL}/api/sse/conversations/{conversation_id}/events',
                    headers=headers,
                ) as response:
                    # Use a synchronous client for SSE parsing
                    client = sseclient.SSEClient(response.content)
                    for event in client.events():
                        event_type = event.event or 'message'
                        try:
                            data = json.loads(event.data)
                            print(f'Event [{event_type}]: {json.dumps(data, indent=2)}')
                        except json.JSONDecodeError:
                            print(f'Event [{event_type}]: {event.data}')

                        # Only process a few events for testing
                        if event_type == 'oh_event':
                            break

            # Start listening for events
            event_task = asyncio.create_task(listen_for_events())

            # Wait a bit before sending an action
            await asyncio.sleep(2)

            # Send a user message action
            async with session.post(
                f'{BASE_URL}/api/sse/conversations/{conversation_id}/actions',
                json={
                    'action': 'user_message',
                    'args': {'message': 'Hello, testing SSE events'},
                },
            ) as response:
                action_response = await response.json()
                print(f'Sent user message: {action_response}')

            # Wait for some events to be processed
            try:
                await asyncio.wait_for(event_task, timeout=10)
            except asyncio.TimeoutError:
                print('Event listening timed out')

            # Check conversation status
            async with session.get(
                f'{BASE_URL}/api/sse/conversations/{conversation_id}/status'
            ) as response:
                status_data = await response.json()
                print(f'Conversation status: {status_data}')

            # Close the SSE session
            async with session.delete(
                f'{BASE_URL}/api/sse/conversations/{conversation_id}'
            ) as response:
                close_data = await response.json()
                print(f'Closed SSE session: {close_data}')


if __name__ == '__main__':
    unittest.main()
