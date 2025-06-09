export interface Message {
  id: string;
  sender: 'user' | 'agent';
  content: string;
  timestamp: Date;
}

export interface ChatState {
  conversationId: string | null;
  connected: boolean;
  initialized: boolean;
  agentState: string;
  messages: Message[];
  error: string | null;
}

export interface SSEEvent {
  event: string;
  data: any;
  id?: string;
  retry?: number;
}

export interface ApiResponse {
  success: boolean;
  data?: any;
  error?: string;
}
