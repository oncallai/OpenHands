#!/usr/bin/env python
"""
Simple test script to verify that the ArchitectAgent can be initialized correctly.
"""

import os
import sys
from openhands.core.config import AgentConfig
from openhands.llm.llm import LLM
from openhands.agenthub.architect_agent.architect_agent import ArchitectAgent

def main():
    """Test initializing the ArchitectAgent."""
    print("Testing ArchitectAgent initialization...")
    
    # Create a minimal config
    config = AgentConfig()
    
    # Create a dummy LLM (we won't actually use it)
    llm = LLM(config=config.llm)
    
    try:
        # Try to initialize the ArchitectAgent
        agent = ArchitectAgent(llm, config)
        print("ArchitectAgent initialized successfully!")
        print(f"Agent version: {agent.VERSION}")
        print(f"Prompt manager initialized: {agent._prompt_manager is not None}")
        return True
    except Exception as e:
        print(f"Error initializing ArchitectAgent: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
