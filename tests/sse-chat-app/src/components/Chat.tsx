'use client';

import React, { useState, useEffect, useRef } from 'react';
import { api, SSEClient } from '../services/api';
import ChatMessage from './ChatMessage';

// Define Message interface
interface Message {
  id: string;
  sender: 'user' | 'agent' | 'system';
  content: string;
  timestamp: Date;
}

// Define our chat state interface
interface ChatState {
  conversationId: string | null;
  initialized: boolean;
  agentState: string;
  messages: Message[];
  error: string | null;
  connected: boolean;
  serverStatus: 'connecting' | 'connected' | 'error';
}

const Chat: React.FC = () => {
  const [state, setState] = useState<ChatState>({
    conversationId: null,
    connected: false,
    initialized: false,
    agentState: 'idle',
    messages: [],
    error: null,
    serverStatus: 'connecting'
  });

  const [inputMessage, setInputMessage] = useState<string>('');
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const sseClientRef = useRef<SSEClient | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  
  // Scroll to bottom whenever messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [state.messages]);
  
  // Clean up SSE connection on unmount
  useEffect(() => {
    return () => {
      if (sseClientRef.current) {
        sseClientRef.current.disconnect();
      }
    };
  }, []);
  
  // Create a new conversation
  const createConversation = async () => {
    try {
      setIsLoading(true);
      setState(prev => ({ ...prev, error: null }));
      
      const conversationId = await api.createConversation("Hello, I'd like to chat with OpenHands assistant");
      console.log(`Conversation created: ${conversationId}`);
      
      setState(prev => ({ 
        ...prev, 
        conversationId,
        messages: [
          ...prev.messages,
          {
            id: `user-init-${Date.now()}`,
            sender: 'user',
            content: "Hello, I'd like to chat with OpenHands assistant",
            timestamp: new Date()
          }
        ]
      }));
      
      // Initialize SSE after creating conversation
      await initializeSSE(conversationId);
    } catch (error: any) {
      console.error('Error creating conversation:', error);
      setState(prev => ({ ...prev, error: `Failed to create conversation: ${error.message || 'Unknown error'}` }));
      setIsLoading(false);
    }
  };
  
  // Initialize SSE for the conversation
  const initializeSSE = async (conversationId: string) => {
    try {
      const initResponse = await api.initializeSSE(conversationId);
      console.log('SSE initialized:', initResponse);
      
      setState(prev => ({ ...prev, initialized: true }));
      
      // Connect to SSE stream
      connectToSSE(conversationId);
    } catch (error: any) {
      console.error('Error initializing SSE:', error);
      setState(prev => ({ ...prev, error: `Failed to initialize SSE: ${error.message || 'Unknown error'}` }));
      setIsLoading(false);
    }
  };
  
  // Connect to the SSE event stream
  const connectToSSE = (conversationId: string) => {
    try {
      // Create new SSE client
      const sseClient = new SSEClient(conversationId);
      sseClientRef.current = sseClient;
      
      // Set up event listeners
      sseClient.on('open', () => {
        console.log('SSE connection opened');
        setState(prev => ({ 
          ...prev, 
          connected: true, 
          serverStatus: 'connected',
          error: null
        }));
        setIsLoading(false);
      });
      
      sseClient.on('connected', (data) => {
        console.log('SSE connected event received:', data);
        setState(prev => ({ 
          ...prev, 
          connected: true, 
          serverStatus: 'connected',
          error: null
        }));
        setIsLoading(false);
      });
      
      sseClient.on('message', (data: any) => {
        console.log('SSE message received:', data);
        processSSEEvent(data);
      });
      
      sseClient.on('agent_message', (data: any) => {
        console.log('Agent message received:', data);
        if (data?.message) {
          const message: Message = {
            id: `agent-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`,
            sender: 'agent',
            content: data.message,
            timestamp: new Date()
          };
          
          setState(prev => ({
            ...prev,
            messages: [...prev.messages, message]
          }));
        }
      });
      
      // Listen for all messages to debug
      sseClient.on('message', (data: any) => {
        console.log('Raw message received in chat component:', data);
        // Process any unhandled message types
        if (data?.type === 'agent_message' && data?.content) {
          const message: Message = {
            id: `agent-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`,
            sender: 'agent',
            content: data.content,
            timestamp: new Date()
          };
          
          setState(prev => ({
            ...prev,
            messages: [...prev.messages, message]
          }));
        }
      });
      
      sseClient.on('agent_state_changed', (data: any) => {
        console.log('Agent state changed:', data);
        if (data?.state) {
          setState(prev => ({ ...prev, agentState: data.state }));
        }
      });
      
      sseClient.on('error', (error: any) => {
        console.error('SSE connection error:', error);
        setState(prev => ({ 
          ...prev, 
          connected: false, 
          serverStatus: 'error',
          error: error?.message || 'Connection error'
        }));
        setIsLoading(false);
      });
      
      sseClient.on('server_error', (error: any) => {
        console.error('Server error received:', error);
        setState(prev => ({ 
          ...prev,
          error: error?.error || 'Server error',
          serverStatus: 'error'
        }));
      });
      
      sseClient.on('close', () => {
        console.log('SSE connection closed');
        setState(prev => ({ ...prev, connected: false }));
      });
      
      // Connect to the SSE stream
      sseClient.connect();
      
    } catch (error: any) {
      console.error('Error connecting to SSE:', error);
      setState(prev => ({ ...prev, error: `Failed to connect to SSE: ${error.message || 'Unknown error'}` }));
      setIsLoading(false);
    }
  };
  
  // Process incoming SSE events
  const processSSEEvent = (event: any) => {
    console.log('Processing event:', event);
    
    // Handle agent state changes
    if (event.type === 'agent_state_changed') {
      setState(prev => ({ ...prev, agentState: event.state }));
    }
    // Handle agent messages if they come through this channel
    else if (event.type === 'agent_message') {
      console.log('Processing agent message from event:', event);
    }
    // Debug any other event types
    else {
      console.log('Unhandled event type:', event.type);
    }
  };
  
  // Send a message to the conversation
  const sendMessage = async () => {
    if (!inputMessage.trim() || !state.conversationId || isLoading) return;
    
    try {
      setIsLoading(true);
      
      // Add user message to state immediately
      const userMessage: Message = {
        id: `user-${Date.now()}`,
        sender: 'user',
        content: inputMessage,
        timestamp: new Date()
      };
      
      setState(prev => ({
        ...prev,
        messages: [...prev.messages, userMessage]
      }));
      
      // Clear input field
      setInputMessage('');
      
      // Send message to API
      await api.sendMessage(state.conversationId, inputMessage);
      
    } catch (error: any) {
      console.error('Error sending message:', error);
      setState(prev => ({ ...prev, error: `Failed to send message: ${error.message || 'Unknown error'}` }));
    } finally {
      setIsLoading(false);
    }
  };
  
  // Close the SSE session
  const closeSession = async () => {
    if (!state.conversationId) return;
    
    try {
      setIsLoading(true);
      
      // Disconnect SSE client
      if (sseClientRef.current) {
        sseClientRef.current.disconnect();
        sseClientRef.current = null;
      }
      
      // Close session on server
      await api.closeSession(state.conversationId);
      
      setState(prev => ({
        ...prev,
        connected: false,
        initialized: false
      }));
      
    } catch (error: any) {
      console.error('Error closing session:', error);
      setState(prev => ({ ...prev, error: `Failed to close session: ${error.message || 'Unknown error'}` }));
    } finally {
      setIsLoading(false);
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    sendMessage();
  };
  
  return (
    <div className="flex flex-col h-full bg-gray-100">
      {/* Header */}
      <div className="bg-white p-4 shadow flex justify-between items-center">
        <h1 className="text-xl font-semibold">OpenHands Chat</h1>
        <div className="flex space-x-2">
          <div className={`px-3 py-1 text-sm rounded-full ${
            state.connected ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'
          }`}>
            {state.connected ? 'Connected' : 'Disconnected'}
          </div>
          <div className={`px-3 py-1 text-sm rounded-full ${
            state.agentState === 'ready' ? 'bg-green-100 text-green-800' : 
            state.agentState === 'thinking' ? 'bg-yellow-100 text-yellow-800' :
            'bg-gray-100 text-gray-800'
          }`}>
            Agent: {state.agentState || 'Unknown'}
          </div>
        </div>
      </div>
      
      {/* Status and error messages */}
      {state.error && (
        <div className="bg-red-100 border border-red-200 text-red-700 px-4 py-2">
          Error: {state.error}
        </div>
      )}
      
      {/* Actions bar */}
      <div className="bg-white p-3 border-b flex space-x-2 overflow-x-auto">
        <button
          onClick={createConversation}
          disabled={isLoading || state.conversationId !== null}
          className={`px-3 py-1 rounded-full text-sm ${
            isLoading || state.conversationId !== null
              ? 'bg-gray-200 text-gray-500 cursor-not-allowed'
              : 'bg-blue-500 text-white hover:bg-blue-600'
          }`}
        >
          {isLoading ? 'Loading...' : 'Start New Chat'}
        </button>
        
        <button
          onClick={closeSession}
          disabled={isLoading || !state.connected}
          className={`px-3 py-1 rounded-full text-sm ${
            isLoading || !state.connected
              ? 'bg-gray-200 text-gray-500 cursor-not-allowed'
              : 'bg-red-500 text-white hover:bg-red-600'
          }`}
        >
          End Session
        </button>
        
        {state.conversationId && (
          <div className="text-sm text-gray-500 py-1 ml-auto">
            ID: {state.conversationId}
          </div>
        )}
      </div>
      
      {/* Messages area */}
      <div className="flex-1 flex flex-col p-4 overflow-auto" ref={messagesEndRef}>
        {state.serverStatus === 'error' && (
          <div className="bg-red-100 border-l-4 border-red-500 text-red-700 p-4 mb-4">
            <p className="font-bold">Backend Error</p>
            <p>{state.error || 'There was an error connecting to the OpenHands backend'}</p>
            <p className="text-sm mt-2">Check if the OpenHands server is running correctly and all services are available.</p>
          </div>
        )}
        
        {state.messages.length === 0 ? (
          <div className="text-center text-gray-500 my-auto">
            {state.serverStatus === 'connecting' 
              ? 'Connecting to OpenHands backend...' 
              : 'Start a conversation to see messages here'}
          </div>
        ) : (
          state.messages.map((message) => (
            <ChatMessage key={message.id} message={message} />
          ))
        )}
      </div>
      
      {/* Input area */}
      <div className="bg-white border-t p-4">
        <form onSubmit={handleSubmit} className="flex space-x-2">
          <input
            type="text"
            value={inputMessage}
            onChange={(e) => setInputMessage(e.target.value)}
            disabled={!state.connected || isLoading}
            placeholder={!state.connected ? "Connect to start chatting..." : "Type your message..."}
            className="flex-1 border rounded-full px-4 py-2 focus:outline-none focus:ring-2 focus:border-blue-300"
          />
          <button
            type="submit"
            disabled={!state.connected || !inputMessage.trim() || isLoading}
            className={`px-4 py-2 rounded-full ${
              !state.connected || !inputMessage.trim() || isLoading
                ? 'bg-gray-200 text-gray-500 cursor-not-allowed'
                : 'bg-blue-500 text-white hover:bg-blue-600'
            }`}
          >
            {isLoading ? '...' : 'Send'}
          </button>
        </form>
      </div>
    </div>
  );
};

export default Chat;
