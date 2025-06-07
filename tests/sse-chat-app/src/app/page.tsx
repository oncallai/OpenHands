'use client';

import dynamic from 'next/dynamic';

// Import the Chat component with dynamic import to avoid SSR issues with EventSource
const Chat = dynamic(() => import('../components/Chat'), {
  loading: () => <div className="flex items-center justify-center h-screen">Loading chat interface...</div>
});

export default function Home() {
  return (
    <main className="flex min-h-screen flex-col">
      <div className="bg-blue-600 text-white p-4">
        <h1 className="text-xl font-bold">OpenHands SSE Chat Demo</h1>
        <p className="text-sm opacity-80">Test the Server-Sent Events (SSE) functionality</p>
      </div>
      
      <div className="flex-1 overflow-hidden">
        <Chat />
      </div>
    </main>
  );
}
