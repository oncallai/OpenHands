# Server-Sent Events (SSE) in OpenHands

This document explains how to use the new SSE endpoint for real-time event streaming in OpenHands.

## Overview

OpenHands now supports Server-Sent Events (SSE) as an alternative to WebSockets for receiving real-time updates from conversations. SSE is a simpler, unidirectional communication protocol that works over standard HTTP, making it easier to work with in some environments.

The OpenHands architecture uses the following communication protocols:

- **For creating conversations**: Use the REST API endpoint (`POST /api/conversations`)
- **For sending messages/actions to the server**: Use the REST API endpoint (`POST /api/conversations/{conversation_id}/events`)
- **For receiving real-time events**: Use the new SSE endpoint (`GET /api/sse/conversations/{conversation_id}/events`)

## Communication Flow

1. **Create a conversation** using the REST API
2. **Establish SSE connection** for receiving events
3. **Exchange messages**:
   - Send user actions via REST API
   - Receive updates via SSE

## Endpoint Details

### 1. Create a Conversation (REST API)

```
POST /api/conversations
```

**Request Body:**
```json
{
  "initial_user_msg": "Check whether there is any incorrect information in the README.md file",
  "repository": "yourusername/your-repo"
}
```

**Response:**
```json
{
  "status": "ok",
  "conversation_id": "abc1234"
}
```

### 2. SSE Connection Endpoint

```
GET /api/sse/conversations/{conversation_id}/events
```

**Query Parameters:**
- `latest_event_id` (optional): The ID of the last event you've received. Use `-1` to start from the beginning.
- `providers_set` (optional): Comma-separated list of provider types (e.g., `github,gitlab`)

**Headers:**
- Standard authentication headers (API key, cookies, etc.)

### 3. Send Messages (REST API)

Use the REST API to send messages/actions:

```javascript
async function sendMessage(conversationId, message) {
  const response = await fetch(`/api/conversations/${conversationId}/events`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${apiKey}`
    },
    body: JSON.stringify({
      type: 'user_message',
      content: message
    }),
    credentials: 'include'
  });
  
  if (!response.ok) {
    throw new Error(`HTTP error! status: ${response.status}`);
  }
  
  return await response.json();
}
```

## Complete Client Implementation Example

Here's a comprehensive example showing how to integrate both Socket.IO and SSE with OpenHands:

```javascript
class OpenHandsClient {
  constructor(apiKey, baseUrl = '') {
    this.apiKey = apiKey;
    this.baseUrl = baseUrl;
    this.eventSource = null;
    this.conversationId = null;
    this.connected = false;
    this.eventHandlers = {
      message: [],
      error: [],
      connection: [],
      close: []
    };
  }

  // Create a new conversation
  async createConversation(repository, initialMessage = '') {
    try {
      const response = await fetch(`${this.baseUrl}/api/conversations`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${this.apiKey}`
        },
        body: JSON.stringify({
          initial_user_msg: initialMessage || `Check whether there is any incorrect information in the README.md file and send me updates`,
          repository: repository
        }),
        credentials: 'include'
      });
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      
      const data = await response.json();
      
      if (data.conversation_id) {
        this.conversationId = data.conversation_id;
        await this.connect();
        return data;
      } else {
        throw new Error('No conversation_id received');
      }
    } catch (error) {
      this._triggerHandlers('error', error);
      throw error;
    }
  }
  
  // Connect to SSE
  async connect(conversationId = null, latestEventId = -1, providersSet = ['github']) {
    if (conversationId) {
      this.conversationId = conversationId;
    }
    
    if (!this.conversationId) {
      throw new Error('No conversation ID provided');
    }
    
    // Connect to SSE for receiving events
    this._connectSSE(latestEventId, providersSet);
    
    this.connected = true;
    return this;
  }
  
  // Setup SSE connection
  _connectSSE(latestEventId = -1, providersSet = ['github']) {
    // Close existing connection if any
    if (this.eventSource) {
      this.eventSource.close();
    }
    
    // Format providers as a string
    const providersStr = Array.isArray(providersSet) ? providersSet.join(',') : providersSet;
    
    // Build the SSE URL with query parameters
    const url = `${this.baseUrl}/api/sse/conversations/${this.conversationId}/events?latest_event_id=${latestEventId}&providers_set=${providersStr}`;
    
    // Create new EventSource with credentials
    this.eventSource = new EventSource(url, { withCredentials: true });
    
    // Set up SSE event handlers
    this.eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        this._triggerHandlers('message', data);
      } catch (e) {
        console.error('Error parsing SSE message:', e);
      }
    };
    
    // Handle connection event
    this.eventSource.addEventListener('connection', (event) => {
      try {
        const data = JSON.parse(event.data);
        this._triggerHandlers('connection', { type: 'sse', ...data });
      } catch (e) {
        console.error('Error parsing connection event:', e);
      }
    });
    
    // Handle errors
    this.eventSource.onerror = (error) => {
      this._triggerHandlers('error', { type: 'sse', error });
    };
  }
  
  // Send a message/action via REST API
  async sendAction(actionData) {
    if (!this.conversationId) {
      throw new Error('Not connected to a conversation');
    }
    
    try {
      const response = await fetch(`${this.baseUrl}/api/conversations/${this.conversationId}/events`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${this.apiKey}`
        },
        body: JSON.stringify(actionData),
        credentials: 'include'
      });
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      
      return await response.json();
    } catch (error) {
      this._triggerHandlers('error', { type: 'api', error });
      throw error;
    }
  }
  
  // Send a user message (convenience method)
  async sendUserMessage(message) {
    return await this.sendAction({
      type: 'user_message',
      content: message
    });
  }
  
  // Close connection
  disconnect() {
    this.connected = false;
    
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }
  
  // Add event listener
  on(event, handler) {
    if (this.eventHandlers[event]) {
      this.eventHandlers[event].push(handler);
    }
    return this;
  }
  
  // Remove event listener
  off(event, handler) {
    if (this.eventHandlers[event]) {
      this.eventHandlers[event] = this.eventHandlers[event].filter(h => h !== handler);
    }
    return this;
  }
  
  // Internal method to trigger handlers
  _triggerHandlers(event, data) {
    if (this.eventHandlers[event]) {
      this.eventHandlers[event].forEach(handler => {
        try {
          handler(data);
        } catch (e) {
          console.error(`Error in ${event} handler:`, e);
        }
      });
    }
  }
}
````

## Usage Example

```javascript
// Initialize the client
const client = new OpenHandsClient('your-api-key');

// Add event listeners
client.on('connection', (data) => {
  console.log('Connected:', data);
});

client.on('message', (event) => {
  // Handle different event types
  switch (event.type) {
    case 'agent_message':
      console.log('Agent says:', event.content);
      break;
    case 'agent_action':
      console.log('Agent performed action:', event);
      break;
    default:
      console.log('Received event:', event);
  }
});

client.on('error', (error) => {
  console.error('Connection error:', error);
});

// Create a new conversation and connect
async function startConversation() {
  try {
    // Create a new conversation
    const result = await client.createConversation('yourusername/your-repo');
    console.log('Conversation created:', result);
    
    // Send a message
    client.sendUserMessage('Hello, I need help with my repository');
  } catch (error) {
    console.error('Error:', error);
  }
}

// Or connect to an existing conversation
function connectToExisting() {
  client.connect('your-conversation-id');
}

// Disconnect when done
function cleanup() {
  client.disconnect();
}

// Start the conversation
startConversation();
```

## Advantages of the REST+SSE Approach

1. **HTTP Everywhere**: Both sending and receiving use standard HTTP, avoiding WebSocket complexity
2. **Firewall Friendly**: Works better through firewalls and proxies that might block WebSockets
3. **Stateless Actions**: Clear request/response cycle for sending actions
4. **Simpler Infrastructure**: No need to maintain WebSocket servers
5. **Debugging Ease**: All communication visible in browser dev tools

## Why Use SSE For Receiving Events?

SSE offers several advantages over regular polling:

1. **Efficient Streaming**: One persistent connection instead of repeated requests
2. **Lower Latency**: Updates delivered immediately when available
3. **Automatic Reconnection**: Browsers handle connection drops
4. **Standard Browser API**: Works with the EventSource API
5. **Simple Implementation**: Much simpler than WebSockets

## Notes and Limitations

1. SSE is unidirectional (server to client only)
2. Some proxies may buffer SSE responses, affecting real-time delivery
3. The maximum number of concurrent SSE connections per domain is limited in some browsers
4. Request/response pattern for sending actions lacks the real-time push capabilities of WebSockets

## Troubleshooting

1. **Connection Refused**: Check that you're providing valid authentication credentials and API key
2. **Events Not Arriving**: Check that the SSE connection is established
3. **Can't Send Messages**: Verify the conversation ID is correct and the API endpoint is accessible
4. **SSE Reconnecting Too Often**: Some proxies may time out long-lived connections
5. **Delayed Messages**: Look for buffering proxies between client and server
6. **Connection Closing Unexpectedly**: Look for server-side errors in the logs
