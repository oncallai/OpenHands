#!/usr/bin/env python
"""
Manual test script for OpenHands SSE functionality.
This script tests the SSE (Server-Sent Events) functionality by creating a conversation,
connecting to the SSE stream, and sending some test messages.

Usage:
    python test_sse_manual.py [--server-url http://localhost:8000]

Dependencies:
    pip install aiohttp sseclient-py
"""

import argparse
import asyncio
import json
import sys
import time
import aiohttp
from typing import Dict, Any


class SSEClient:
    """Simple client for testing SSE endpoints."""

    def __init__(self, base_url: str = "http://localhost:8000"):
        """Initialize the SSE client with the server base URL."""
        self.base_url = base_url
        self.conversation_id = None
        self.session = None
        self.event_task = None
        self.running = False

    async def __aenter__(self):
        """Set up the client session."""
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Clean up resources."""
        if self.event_task and not self.event_task.done():
            self.running = False
            self.event_task.cancel()
            try:
                await self.event_task
            except asyncio.CancelledError:
                pass

        if self.session:
            await self.session.close()

    async def create_conversation(self, initial_message: str = "Hello, testing SSE") -> str:
        """Create a new conversation and return the conversation ID."""
        print(f"Creating conversation with message: {initial_message}")

        async with self.session.post(
            f"{self.base_url}/api/conversations",
            json={"initial_user_msg": initial_message}
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Failed to create conversation: {await response.text()}")

            data = await response.json()
            self.conversation_id = data.get("conversation_id")
            print(f"Created conversation with ID: {self.conversation_id}")
            return self.conversation_id

    async def initialize_sse_session(self) -> Dict[str, Any]:
        """Initialize the SSE session for the current conversation."""
        if not self.conversation_id:
            raise ValueError("No conversation ID set. Create a conversation first.")

        print(f"Initializing SSE session for conversation: {self.conversation_id}")

        async with self.session.post(
            f"{self.base_url}/api/sse/conversations/{self.conversation_id}/initialize"
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Failed to initialize SSE session: {await response.text()}")

            data = await response.json()
            print(f"SSE session initialized: {data}")
            return data

    async def listen_for_events(self):
        """Listen for SSE events from the current conversation."""
        if not self.conversation_id:
            raise ValueError("No conversation ID set. Create a conversation first.")

        self.running = True
        print(f"Listening for events on conversation: {self.conversation_id}")

        async with self.session.get(
            f"{self.base_url}/api/sse/conversations/{self.conversation_id}/events",
            headers={"Accept": "text/event-stream"}
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Failed to connect to SSE stream: {await response.text()}")

            print("Connected to SSE stream. Waiting for events...")

            # Process the SSE stream manually
            buffer = ""
            event_type = "message"
            event_data = ""
            event_id = None

            while self.running:
                chunk = await response.content.read(1024)
                if not chunk:
                    if self.running:
                        print("SSE stream closed unexpectedly")
                    break

                buffer += chunk.decode('utf-8')

                # Process complete messages
                while '\n\n' in buffer:
                    message, buffer = buffer.split('\n\n', 1)
                    lines = message.split('\n')

                    # Parse the message
                    event_type = "message"
                    event_data = ""
                    event_id = None

                    for line in lines:
                        if line.startswith('event:'):
                            event_type = line[6:].strip()
                        elif line.startswith('data:'):
                            event_data += line[5:].strip() + '\n'
                        elif line.startswith('id:'):
                            event_id = line[3:].strip()

                    # Remove trailing newline from data
                    if event_data.endswith('\n'):
                        event_data = event_data[:-1]

                    # Try to parse JSON data
                    try:
                        data_obj = json.loads(event_data)
                        data_str = json.dumps(data_obj, indent=2)
                    except json.JSONDecodeError:
                        data_str = event_data

                    # Print the event
                    print(f"\n=== EVENT [{event_type}] ===")
                    if event_id:
                        print(f"ID: {event_id}")
                    print(f"Data: {data_str}")
                    print("=" * 40)

    async def start_event_listener(self):
        """Start listening for events in a background task."""
        if self.event_task and not self.event_task.done():
            print("Already listening for events")
            return

        self.event_task = asyncio.create_task(self.listen_for_events())

    async def send_message(self, message: str, retry_on_error=True) -> Dict[str, Any]:
        """Send a user message to the current conversation.

        Args:
            message: The message text to send
            retry_on_error: If True, will wait for container readiness and retry once on error

        Returns:
            Dict with server response
        """
        if not self.conversation_id:
            raise ValueError("No conversation ID set. Create a conversation first.")

        print(f"Sending message to conversation {self.conversation_id}: {message}")

        try:
            async with self.session.post(
                f"{self.base_url}/api/sse/conversations/{self.conversation_id}/actions",
                json={
                    "action": "oh_user_action",
                    "args": {
                        "action": "message",
                        "args": {"text": message}
                    }
                }
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    if retry_on_error and ("No active session" in error_text or "not fully initialized" in error_text):
                        print("Container not ready yet. Waiting for readiness before retrying...")
                        ready = await self.wait_for_container_ready(timeout=20)
                        if ready:
                            # Retry once after waiting for container
                            return await self.send_message(message, retry_on_error=False)
                        else:
                            raise RuntimeError(f"Container not ready after waiting: {error_text}")
                    else:
                        raise RuntimeError(f"Failed to send message: {error_text}")

                data = await response.json()
                print(f"Message sent: {data}")
                return data
        except Exception as e:
            if retry_on_error and ("No active session" in str(e) or "not fully initialized" in str(e)):
                print("Connection error. Waiting for container readiness before retrying...")
                ready = await self.wait_for_container_ready(timeout=20)
                if ready:
                    # Retry once after waiting for container
                    return await self.send_message(message, retry_on_error=False)
            raise

    async def check_status(self) -> Dict[str, Any]:
        """Check the status of the current SSE session."""
        if not self.conversation_id:
            raise ValueError("No conversation ID set. Create a conversation first.")

        print(f"Checking status for conversation: {self.conversation_id}")

        async with self.session.get(
            f"{self.base_url}/api/sse/conversations/{self.conversation_id}/status"
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Failed to check status: {await response.text()}")

            data = await response.json()
            print(f"Status: {data}")
            return data

    async def wait_for_container_ready(self, timeout=30, check_interval=1.0) -> bool:
        """Wait for the Docker container to be fully ready.

        This polls the status endpoint until the container is reported as ready
        or until the timeout is reached.

        Args:
            timeout: Maximum time to wait in seconds
            check_interval: Time between status checks in seconds

        Returns:
            bool: True if container is ready, False if timeout occurred
        """
        if not self.conversation_id:
            raise ValueError("No conversation ID set. Create a conversation first.")

        print(f"Waiting for container to be ready for conversation: {self.conversation_id}")

        start_time = time.time()
        ready = False

        while time.time() - start_time < timeout:
            try:
                status = await self.check_status()

                # Check for explicit readiness signal in the enhanced status response
                if status.get("ready_for_messages") is True:
                    print("Container is ready for messages!")
                    ready = True
                    break

                # Check for docker readiness
                if status.get("docker_ready") is True:
                    print("Docker container is running. Waiting for agent initialization...")

                # Check agent state for readiness
                agent_state = status.get("agent_state")
                if agent_state in ["awaiting_user_input", "ready", "READY"]:
                    print(f"Agent is in ready state: {agent_state}")
                    ready = True
                    break

                # Log the current status
                print(f"Container status: {status}")

                # If session exists but not ready, try a probe message as a last resort
                if status.get("active") and status.get("session_exists") and not ready:
                    try:
                        # Try sending a heartbeat ping to see if the session accepts messages
                        async with self.session.post(
                            f"{self.base_url}/api/sse/conversations/{self.conversation_id}/actions",
                            json={
                                "action": "oh_heartbeat",
                                "args": {}
                            },
                            timeout=2.0  # Short timeout to avoid hanging
                        ) as response:
                            if response.status == 200:
                                print("Container is ready! Heartbeat accepted.")
                                ready = True
                                break
                    except asyncio.TimeoutError:
                        print("Heartbeat probe timed out - container likely not ready")
                    except Exception as e:
                        pass  # Ignore other exceptions and continue polling

            except Exception as e:
                print(f"Error checking container status: {e}")

            # Wait before checking again
            await asyncio.sleep(check_interval)

        if not ready:
            print(f"Timeout waiting for container readiness after {timeout} seconds")

        return ready

    async def close_session(self) -> Dict[str, Any]:
        """Close the current SSE session."""
        if not self.conversation_id:
            raise ValueError("No conversation ID set. Create a conversation first.")

        print(f"Closing conversation: {self.conversation_id}")

        async with self.session.delete(
            f"{self.base_url}/api/sse/conversations/{self.conversation_id}"
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Failed to close session: {await response.text()}")

            data = await response.json()
            print(f"Session closed: {data}")
            return data


async def interactive_test(base_url: str):
    """Run an interactive test session."""
    print("=" * 60)
    print("OpenHands SSE Interactive Test")
    print("=" * 60)
    print(f"Server URL: {base_url}")
    print()
    print("Starting test session...")

    async with SSEClient(base_url) as client:
        # Create a conversation
        await client.create_conversation("Testing SSE functionality")

        # Initialize SSE session
        await client.initialize_sse_session()

        # Start listening for events
        await client.start_event_listener()

        # Wait for container to be ready
        print("Waiting for container to be ready...")
        container_ready = await client.wait_for_container_ready(timeout=30)
        if not container_ready:
            print("WARNING: Container not ready within timeout. Sending messages may fail.")
        else:
            print("Container is ready! You can now send messages.")

        # Interactive loop
        try:
            while True:
                print("\nCommands:")
                print("  m <message> - Send a message")
                print("  s - Check status")
                print("  w - Wait for container ready")
                print("  q - Quit")

                cmd = input("> ").strip()

                if not cmd:
                    continue

                if cmd.lower() == 'q':
                    break
                elif cmd.lower() == 's':
                    await client.check_status()
                elif cmd.lower() == 'w':
                    await client.wait_for_container_ready()
                elif cmd.lower().startswith('m '):
                    message = cmd[2:].strip()
                    if message:
                        await client.send_message(message)
                else:
                    print(f"Unknown command: {cmd}")

        except KeyboardInterrupt:
            print("\nTest interrupted by user")

        finally:
            # Close the session
            try:
                await client.close_session()
            except Exception as e:
                print(f"Error closing session: {e}")


async def automated_test(base_url: str):
    """Run an automated test sequence."""
    print("=" * 60)
    print("OpenHands SSE Automated Test")
    print("=" * 60)
    print(f"Server URL: {base_url}")
    print()
    print("Starting automated test...")

    async with SSEClient(base_url) as client:
        # 1. Create a conversation
        await client.create_conversation("Automated SSE test")

        # 2. Initialize SSE session
        await client.initialize_sse_session()

        # 3. Start listening for events
        await client.start_event_listener()

        # # 4. Wait for container to be fully ready before interacting with it
        # print("Waiting for container to be ready...")
        # container_ready = await client.wait_for_container_ready(timeout=30)
        # if not container_ready:
        #     print("WARNING: Container not ready within timeout, but continuing anyway")

        # 5. Send a test message only after container is ready
        await client.send_message("This is a test message from the automated SSE test")

        # 6. Wait for response events
        print("Waiting for response events...")
        await asyncio.sleep(5)

        # 7. Check status
        await client.check_status()

        # 8. Send another message
        await client.send_message("How does the SSE implementation work?")

        # 9. Wait for more events
        print("Waiting for more events...")
        await asyncio.sleep(5)

        # 10. Close the session (done in context manager)
        print("Test sequence completed.")


async def main():
    parser = argparse.ArgumentParser(description="Test OpenHands SSE functionality")
    parser.add_argument("--server-url", default="http://localhost:3000", help="OpenHands server URL")
    parser.add_argument("--automated", action="store_true", help="Run automated test sequence")

    args = parser.parse_args()

    if args.automated:
        await automated_test(args.server_url)
    else:
        await interactive_test(args.server_url)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nTest interrupted by user")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
