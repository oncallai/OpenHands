import axios from 'axios';
import { SSEEvent } from '../types';

// Constants
const BASE_URL = 'http://localhost:3000';
const API_BASE = `${BASE_URL}/api`;
const SSE_BASE = `${API_BASE}/sse`;

/**
 * SSEClient for managing EventSource connections to the SSE backend
 */
export class SSEClient {
  private eventSource: EventSource | null = null;
  private conversationId: string;
  private listeners: Map<string, ((data: any) => void)[]> = new Map();
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 3;
  private reconnectTimeout = 1000;
  
  constructor(conversationId: string) {
    this.conversationId = conversationId;
  }
  
  /**
   * Connect to the SSE stream
   */
  connect(): void {
    if (this.eventSource) {
      this.disconnect();
    }
    
    const url = `${SSE_BASE}/conversations/${this.conversationId}/events`;
    console.log(`Connecting to SSE stream at: ${url}`);
    this.eventSource = new EventSource(url);
    
    // Set up event handlers
    this.eventSource.onopen = this.handleOpen.bind(this);
    this.eventSource.onerror = this.handleError.bind(this);
    this.eventSource.onmessage = this.handleMessage.bind(this);
    
    // Add standard SSE event listeners
    this.eventSource.addEventListener('connection', this.handleConnection.bind(this));
    this.eventSource.addEventListener('close', this.handleClose.bind(this));
    this.eventSource.addEventListener('error', this.handleServerError.bind(this));
  }
  
  /**
   * Disconnect from the SSE stream
   */
  disconnect(): void {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }
  
  /**
   * Register event listener
   */
  on(event: string, callback: (data: any) => void): void {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, []);
    }
    this.listeners.get(event)?.push(callback);
  }
  
  /**
   * Remove event listener
   */
  off(event: string, callback: (data: any) => void): void {
    const callbacks = this.listeners.get(event);
    if (callbacks) {
      const index = callbacks.indexOf(callback);
      if (index !== -1) {
        callbacks.splice(index, 1);
      }
    }
  }
  
  /**
   * Emit event to registered listeners
   */
  private emit(event: string, data: any): void {
    const callbacks = this.listeners.get(event);
    if (callbacks) {
      callbacks.forEach(callback => callback(data));
    }
  }
  
  /**
   * Handle EventSource open event
   */
  private handleOpen(event: Event): void {
    console.log('SSE connection opened:', event);
    this.reconnectAttempts = 0;
    this.emit('open', event);
  }
  
  /**
   * Handle EventSource error event
   */
  private handleError(event: Event): void {
    console.error('SSE connection error:', event);
    this.emit('error', event);
    
    if (this.eventSource?.readyState === EventSource.CLOSED) {
      this.tryReconnect();
    }
  }
  
  /**
   * Handle general EventSource message event
   */
  private handleMessage(event: MessageEvent): void {
    console.log('Raw SSE message received:', event.type, event.data);
    try {
      const data = JSON.parse(event.data);
      console.log('Parsed SSE message:', data);
      this.emit('message', data);
      
      // Process agent messages
      if (data.type === 'agent_message' && data.content) {
        console.log('Detected agent message:', data.content);
        this.emit('agent_message', { message: data.content });
      }
      // Process agent state changes
      else if (data.type === 'agent_state_changed' && data.state) {
        console.log('Detected agent state change:', data.state);
        this.emit('agent_state_changed', { state: data.state });
      }
      // Process generic event based on type field
      else if (data.type) {
        console.log(`Detected event type: ${data.type}`);
        this.emit(data.type, data);
      }
    } catch (error) {
      console.error('Error parsing SSE message:', error, 'Data:', event.data);
    }
  }
  
  /**
   * Handle agent message events
   */
  // Handle connection established event
  private handleConnection(event: MessageEvent): void {
    try {
      console.log('Connection event received:', event.data);
      const data = JSON.parse(event.data);
      this.emit('connected', data);
    } catch (error) {
      console.error('Error parsing connection event:', error);
    }
  }
  
  /**
   * Handle agent state changed events
   */
  // Handle close event
  private handleClose(event: MessageEvent): void {
    try {
      console.log('Close event received:', event.data);
      this.disconnect();
      this.emit('close', event.data);
    } catch (error) {
      console.error('Error handling close event:', error);
    }
  }
  
  // Handle server-side error event
  private handleServerError(event: MessageEvent): void {
    try {
      console.error('Server error event received:', event.data);
      const data = JSON.parse(event.data);
      this.emit('server_error', data);
    } catch (error) {
      console.error('Error parsing server error event:', error);
    }
  }
  
  /**
   * Try to reconnect to the SSE stream
   */
  private tryReconnect(): void {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      this.emit('max_reconnect_attempts', null);
      return;
    }
    
    this.reconnectAttempts++;
    setTimeout(() => {
      console.log(`Reconnect attempt ${this.reconnectAttempts}...`);
      this.connect();
    }, this.reconnectTimeout);
  }
}

/**
 * API client for the OpenHands SSE backend
 */
const api = {
  /**
   * Create a new conversation
   * @param initialUserMessage Optional initial user message
   * @returns The conversation ID
   */
  async createConversation(initialUserMessage?: string): Promise<string> {
    try {
      const payload = initialUserMessage ? { initial_user_msg: initialUserMessage } : {};
      const response = await axios.post(`${API_BASE}/conversations`, payload);
      
      // The API returns { conversation_id: "some-id" }
      if (response.data && response.data.conversation_id) {
        return response.data.conversation_id;
      } else {
        throw new Error('Invalid response format from create conversation endpoint');
      }
    } catch (error) {
      console.error('Error creating conversation:', error);
      throw error;
    }
  },
  
  /**
   * Initialize SSE for a conversation
   * @param conversationId The conversation ID
   * @returns The initialization response
   */
  async initializeSSE(conversationId: string): Promise<any> {
    try {
      const response = await axios.post(`${SSE_BASE}/conversations/${conversationId}/initialize`);
      return response.data;
    } catch (error) {
      console.error('Error initializing SSE:', error);
      throw error;
    }
  },
  
  /**
   * Get the status of a SSE session
   * @param conversationId The conversation ID
   * @returns The session status
   */
  async getSessionStatus(conversationId: string): Promise<any> {
    try {
      const response = await axios.get(`${SSE_BASE}/conversations/${conversationId}/status`);
      return response.data;
    } catch (error) {
      console.error('Error getting session status:', error);
      throw error;
    }
  },
  
  /**
   * Send a message to a conversation
   * @param conversationId The conversation ID
   * @param message The message to send
   * @returns The response from the backend
   */
  async sendMessage(conversationId: string, message: string): Promise<any> {
    try {
      const payload = {
        action: "user_message",
        args: { message }
      };
      
      const response = await axios.post(
        `${SSE_BASE}/conversations/${conversationId}/actions`,
        payload
      );
      
      return response.data;
    } catch (error) {
      console.error('Error sending message:', error);
      throw error;
    }
  },
  
  /**
   * Close a SSE session
   * @param conversationId The conversation ID
   * @returns The response from the backend
   */
  async closeSession(conversationId: string): Promise<any> {
    try {
      const response = await axios.delete(`${SSE_BASE}/conversations/${conversationId}`);
      return response.data;
    } catch (error) {
      console.error('Error closing session:', error);
      throw error;
    }
  }
};

export { api };
