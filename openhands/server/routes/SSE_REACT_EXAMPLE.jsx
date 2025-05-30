import React, { useEffect, useState, useCallback, useRef } from 'react';

/**
 * OpenHands SSE Chat Component
 * 
 * This component demonstrates how to use Server-Sent Events (SSE)
 * for receiving real-time updates and REST API for sending actions.
 */
const OpenHandsSSEChat = ({ apiKey }) => {
  const [messages, setMessages] = useState([]);
  const [inputValue, setInputValue] = useState('');
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState(null);
  const [conversationId, setConversationId] = useState(null);
  const [repository, setRepository] = useState('');
  const eventSourceRef = useRef(null);
  const connectionIdRef = useRef(null);

  // Function to create a new conversation
  const createConversation = async () => {
    try {
      setError(null);
      
      if (!repository.trim()) {
        setError('Please enter a repository name');
        return;
      }
      
      const response = await fetch('/api/conversations', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${apiKey}`
        },
        body: JSON.stringify({
          initial_user_msg: 'Check whether there is any incorrect information in the README.md file and send me updates',
          repository: repository.trim()
        }),
        credentials: 'include'
      });
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      
      const data = await response.json();
      console.log('Conversation created:', data);
      
      if (data.conversation_id) {
        setConversationId(data.conversation_id);
        // Now connect to SSE
        connectSSE(data.conversation_id);
      } else {
        throw new Error('No conversation_id received');
      }
    } catch (error) {
      console.error('Error creating conversation:', error);
      setError(`Failed to create conversation: ${error.message}`);
    }
  };
  
  // Function to establish SSE connection
  const connectSSE = useCallback((convId) => {    
    // Connect to SSE
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }
    
    try {
      // Create new SSE connection
      const url = `/api/sse/conversations/${convId}/events?latest_event_id=-1`;
      const eventSource = new EventSource(url, { withCredentials: true });
      eventSourceRef.current = eventSource;
      
      // Handle connection event
      eventSource.addEventListener('connection', (event) => {
        try {
          const data = JSON.parse(event.data);
          connectionIdRef.current = data.connection_id;
          setIsConnected(true);
          setError(null);
          console.log('SSE connected:', data);
        } catch (e) {
          console.error('Error parsing connection event:', e);
        }
      });
      
      // Handle regular message events
      eventSource.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          
          // Process different event types
          if (data.type === 'agent_message' || data.type === 'user_message') {
            setMessages(prevMessages => [...prevMessages, {
              id: Date.now(),
              type: data.type,
              content: data.content,
              timestamp: new Date().toISOString()
            }]);
          } else {
            // Handle other event types as needed
            console.log('Received event:', data);
          }
        } catch (e) {
          console.error('Error handling SSE message:', e);
        }
      };
      
      // Handle errors
      eventSource.onerror = (error) => {
        console.error('SSE connection error:', error);
        setError('Connection error. Reconnecting...');
        setIsConnected(false);
        // Browser will automatically try to reconnect
      };
    } catch (error) {
      console.error('Error establishing SSE connection:', error);
      setError(`Failed to connect: ${error.message}`);
      setIsConnected(false);
    }
  }, []);
  
  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
      }
    };
  }, []);

  // Function to send a message using REST API
  const sendMessage = async (e) => {
    e.preventDefault();
    if (!inputValue.trim() || !conversationId) return;

    try {
      // Add user message to UI immediately
      const userMessage = {
        id: Date.now(),
        type: 'user_message',
        content: inputValue,
        timestamp: new Date().toISOString()
      };
      setMessages(prevMessages => [...prevMessages, userMessage]);
      
      // Prepare message data
      const messageData = {
        type: 'user_message',
        content: inputValue
      };
      
      // Send via REST API
      const response = await fetch(`/api/conversations/${conversationId}/events`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${apiKey}`
        },
        body: JSON.stringify(messageData),
        credentials: 'include'
      });
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      
      setInputValue('');
    } catch (error) {
      console.error('Error sending message:', error);
      setError(`Failed to send message: ${error.message}`);
    }
  };

  // Reconnect function
  const handleReconnect = () => {
    setError(null);
    if (conversationId) {
      connectSSE(conversationId);
    }
  };

  return (
    <div className="openhands-chat">
      <div className="chat-header">
        <h2>OpenHands Chat</h2>
        <div className="connection-status">
          {conversationId ? (
            isConnected ? (
              <span className="status-connected">Connected</span>
            ) : (
              <span className="status-disconnected">Disconnected</span>
            )
          ) : (
            <span className="status-disconnected">No Conversation</span>
          )}
        </div>
      </div>

      {error && (
        <div className="error-banner">
          <p>{error}</p>
          {conversationId && <button onClick={handleReconnect}>Reconnect</button>}
        </div>
      )}

      {!conversationId ? (
        <div className="setup-container">
          <h3>Create a New Conversation</h3>
          <div className="form-group">
            <label htmlFor="repository">Repository (username/repo):</label>
            <input
              id="repository"
              type="text"
              value={repository}
              onChange={(e) => setRepository(e.target.value)}
              placeholder="e.g., yourusername/your-repo"
            />
          </div>
          <button 
            onClick={createConversation}
            disabled={!repository.trim()}
          >
            Start Conversation
          </button>
        </div>
      ) : (
        <>
          <div className="messages-container">
            {messages.length === 0 ? (
              <div className="empty-state">No messages yet. Start the conversation!</div>
            ) : (
              messages.map(message => (
                <div key={message.id} className={`message ${message.type}`}>
                  <div className="message-header">
                    <span className="sender">
                      {message.type === 'user_message' ? 'You' : 'OpenHands'}
                    </span>
                    <span className="time">
                      {new Date(message.timestamp).toLocaleTimeString()}
                    </span>
                  </div>
                  <div className="message-content">{message.content}</div>
                </div>
              ))
            )}
          </div>

          <form className="message-input" onSubmit={sendMessage}>
            <input
              type="text"
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              placeholder="Type your message..."
              disabled={!isConnected}
            />
            <button type="submit" disabled={!isConnected || !inputValue.trim()}>
              Send
            </button>
          </form>
        </>
      )}
    </div>
  );
};

export default OpenHandsSSEChat;
