-- Migration: Create tables for OpenHands session/file store structure

-- Table: openhands_sessions
CREATE TABLE IF NOT EXISTS openhands_sessions (
    id TEXT PRIMARY KEY,
    metadata JSONB,
    agent_state BYTEA,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Table: openhands_events
CREATE TABLE IF NOT EXISTS openhands_events (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES openhands_sessions(id) ON DELETE CASCADE,
    event_index INT NOT NULL,
    event_data JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id, event_index)
);

-- Indexes for efficient JSONB querying (example for user_id in metadata)
CREATE INDEX IF NOT EXISTS idx_openhands_sessions_metadata_user_id ON openhands_sessions ((metadata->>'user_id'));
-- Add more indexes as needed for common metadata fields
