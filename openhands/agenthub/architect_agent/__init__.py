"""
ArchitectAgent module for OpenHands.
"""

from openhands.agenthub.architect_agent.architect_agent import ArchitectAgent
from openhands.controller.agent import Agent

Agent.register('ArchitectAgent', ArchitectAgent)
