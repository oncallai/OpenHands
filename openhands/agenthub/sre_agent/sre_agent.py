"""
SRE (Site Reliability Engineering) Agent for monitoring, observability, and reliability tasks.
"""

import os
from typing import TYPE_CHECKING, List, Any, Dict, cast

if TYPE_CHECKING:
    from openhands.events.action import Action
    from openhands.llm.llm import ModelResponse

import openhands.agenthub.sre_agent.function_calling as sre_function_calling
from openhands.controller.agent import Agent
from openhands.controller.state.state import State
from openhands.core.config import AgentConfig
from openhands.core.logger import openhands_logger as logger
from openhands.events.action import AgentFinishAction, MessageAction
from openhands.events.event import Event
from openhands.llm.llm import LLM
from openhands.memory.conversation_memory import ConversationMemory
from openhands.runtime.plugins import PluginRequirement
from openhands.utils.prompt import PromptManager


class SREAgent(Agent):
    """
    SRE (Site Reliability Engineering) Agent for monitoring, observability, and reliability tasks.
    
    This agent specializes in:
    - Log analysis and correlation
    - Metrics collection and interpretation
    - Distributed tracing and request flows
    - Performance monitoring and benchmarking
    - Alerting systems and incident response
    - System health checks and diagnostics
    - Root cause analysis for reliability issues
    """
    
    VERSION = '1.0'
    
    # No sandbox plugins needed for basic operation
    sandbox_plugins: List[PluginRequirement] = []
    
    def __init__(self, llm: LLM, config: AgentConfig):
        super().__init__(llm, config)
        self.reset()
        
        # Initialize prompt manager
        self._prompt_manager = PromptManager(
            prompt_dir=os.path.join(os.path.dirname(__file__), 'prompts'),
        )
        
        # Create conversation memory
        self.conversation_memory = ConversationMemory(self.config, self.prompt_manager)
        
        # Set up function calling
        self.tools = sre_function_calling.get_tools()
        
        logger.info(f"SREAgent {self.VERSION} initialized")
    
    def reset(self) -> None:
        """Reset the agent state."""
        super().reset()
    
    def step(self, state: State) -> 'Action':
        """
        Perform one step using the SRE Agent.
        
        This includes gathering info on previous steps and prompting the model to take action.
        
        Args:
            state: Used to get updated info
            
        Returns:
            An action to perform
        """
        # Prepare the message to send to the LLM
        messages = self._get_messages(state.history)
        
        # Create parameters for LLM completion
        params = {
            'messages': self.llm.format_messages_for_llm(messages),
            'tools': self.tools,
            'extra_body': {'metadata': state.to_llm_metadata(agent_name=self.name)}
        }
        
        # Get response from LLM
        response = self.llm.completion(**params)
        logger.debug(f'Response from LLM: {response}')
        
        # Convert response to actions
        actions = self.response_to_actions(response)
        logger.debug(f'Actions after response_to_actions: {actions}')
        
        # Return the first action
        if actions:
            return actions[0]
        else:
            # If no actions were returned, create a default message
            return MessageAction(content="I'm analyzing the observability data to identify patterns and anomalies.")
    
    def _get_messages(self, events: List[Event]) -> List:
        """
        Construct the message history for the LLM conversation.
        
        Args:
            events: The list of events to convert to messages
            
        Returns:
            A list of formatted messages ready for LLM consumption
        """
        if not self.prompt_manager:
            raise Exception('Prompt Manager not instantiated.')
        
        # Find the initial user message action
        initial_user_message = None
        for event in events:
            if isinstance(event, MessageAction) and event.source == 'user':
                initial_user_message = event
                break
        
        # If no initial user message is found, create a default one
        if initial_user_message is None:
            initial_user_message = MessageAction(content="Please help with monitoring and observability tasks.")
            logger.warning("No initial user message found, using default message")
        
        # Use ConversationMemory to process events
        messages = self.conversation_memory.process_events(
            condensed_history=events,
            initial_user_action=initial_user_message,
            max_message_chars=self.llm.config.max_message_chars,
            vision_is_active=self.llm.vision_is_active(),
        )
        
        if self.llm.is_caching_prompt_active():
            self.conversation_memory.apply_prompt_caching(messages)
        
        return messages
    
    def response_to_actions(self, response: 'ModelResponse') -> List['Action']:
        """
        Convert LLM response to a list of actions.
        
        Args:
            response: The response from the LLM
            
        Returns:
            A list of actions to perform
        """
        return sre_function_calling.response_to_actions(
            response, mcp_tool_names=list(self.mcp_tools.keys())
        )
