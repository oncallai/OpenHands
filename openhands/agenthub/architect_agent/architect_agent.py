"""
This module contains the ArchitectAgent class, which is responsible for analyzing incidents,
delegating troubleshooting steps to available agents, and finding root causes of issues.
"""

from __future__ import annotations

# Standard library imports
import os
from collections import deque
from typing import List, Optional

# Third-party library imports
from litellm import ChatCompletionToolParam

# OpenHands framework imports
from openhands.controller.agent import Agent
from openhands.controller.state.incident import Incident, TroubleshootingStep
from openhands.controller.state.state import State
from openhands.core.config import AgentConfig
from openhands.core.logger import openhands_logger as logger
from openhands.llm.llm_utils import check_tools
from openhands.events.action import (
    Action,
    AgentDelegateAction,
    AgentFinishAction,
    AgentFinishTaskCompleted,
    MessageAction,
)
from openhands.events.event import Event
from openhands.events.observation import AgentDelegateObservation
from openhands.llm.llm import LLM, ModelResponse
from openhands.memory.condenser import Condenser
from openhands.memory.condenser.condenser import Condensation, View
from openhands.memory.conversation_memory import ConversationMemory
from openhands.runtime.plugins import PluginRequirement
from openhands.utils.prompt import Message, PromptManager

# Local imports
from openhands.agenthub.architect_agent.function_calling import response_to_actions
from openhands.agenthub.architect_agent.tools import (
    AnalyzeIncidentTool,
    DelegateStepTool,
    RootCauseReportTool,
    ThinkTool,
)


class ArchitectAgent(Agent):
    VERSION = '1.0'
    """
    The Architect Agent is responsible for analyzing incidents, delegating troubleshooting 
    steps to available agents, and finding root causes of issues. It does not execute steps 
    itself but coordinates the work of other agents.
    
    This agent uses a tool-driven approach where the LLM selects appropriate tools based on context:
    1. analyze_incident: Analyzes the incident and creates a troubleshooting plan
    2. delegate_step: Assigns specific steps to specialized agents based on their expertise
    3. create_root_cause_report: Synthesizes findings to determine the root cause
    4. think: Reasons through complex problems before taking action
    
    The ArchitectAgent follows the standard OpenHands agent pattern with tool-driven function
    calling, enabling more flexible and intelligent incident analysis. The agent maintains
    conversation memory across iterations to build on previous findings until the root cause
    is identified.
    """
       
    # No sandbox plugins needed as this agent doesn't execute code directly
    sandbox_plugins: List[PluginRequirement] = []
    
    def __init__(
        self, 
        llm: LLM, 
        config: AgentConfig
    ) -> None:
        """
        Initialize the ArchitectAgent.
        
        Args:
            llm: The language model to use for analysis
            config: The agent configuration
        """
        super().__init__(llm, config)
        self.pending_actions: deque['Action'] = deque()
        self.reset()
        self.tools = self._get_tools()

        # Create a ConversationMemory instance
        self.conversation_memory = ConversationMemory(self.config, self.prompt_manager)

        self.condenser = Condenser.from_config(self.config.condenser)
        logger.debug(f'Using condenser: {type(self.condenser)}')
        
        # We use the standard Agent.list_agents() method to get available agents
        self.agent_list = Agent.list_agents()
        
        logger.info(f"ArchitectAgent {self.VERSION} initialized")
    
    @property
    def prompt_manager(self) -> PromptManager:
        if self._prompt_manager is None:
            self._prompt_manager = PromptManager(
                prompt_dir=os.path.join(os.path.dirname(__file__), 'prompts'),
            )
        return self._prompt_manager
    
    def _get_tools(self) -> list['ChatCompletionToolParam']:
        # For these models, we use short tool descriptions ( < 1024 tokens)
        # to avoid hitting the OpenAI token limit for tool descriptions.
        SHORT_TOOL_DESCRIPTION_LLM_SUBSTRS = ['gpt-', 'o3', 'o1', 'o4']

        use_short_tool_desc = False
        if self.llm is not None:
            use_short_tool_desc = any(
                model_substr in self.llm.config.model
                for model_substr in SHORT_TOOL_DESCRIPTION_LLM_SUBSTRS
            )
        
        tools = []
        
        # Add our specific tools
        tools.append(AnalyzeIncidentTool)
        tools.append(RootCauseReportTool)
        tools.append(DelegateStepTool)
        tools.append(ThinkTool)
        
        return tools
        
    def reset(self) -> None:
        """Resets the Architect Agent."""
        super().reset()
        self.pending_actions.clear()
    
    def step(self, state: State) -> 'Action':
        """Performs one step using the Architect Agent.
        
        This includes gathering info on previous steps and prompting the model to analyze incidents and delegate tasks.
        
        Parameters:
        - state (State): used to get updated info
        
        Returns:
        - AgentDelegateAction(agent, inputs) - delegate tasks to specialized agents
        - MessageAction(content) - Message action to communicate analysis or findings
        - AgentThinkAction(thought) - reasoning through complex problems
        - AgentFinishAction() - end the interaction
        """
        # Continue with pending actions if any
        if self.pending_actions:
            return self.pending_actions.popleft()
        
        # If we're done, go back
        latest_user_message = state.get_last_user_message()
        if latest_user_message and latest_user_message.content.strip() == '/exit':
            return AgentFinishAction()
        
        # Initialize the incident in the state if it doesn't exist
        if state.incident is None:
            state.incident = Incident()
            logger.info("Initialized new incident in state")
        
        # Condense the events from the state using the condenser
        # If we get a view we'll pass those to be processed, but if we get a condensation
        # event we'll just return that instead of an action. The controller will
        # immediately ask the agent to step again with the new view.
        condensed_history: list[Event] = []
        match self.condenser.condensed_history(state):
            case View(events=events):
                condensed_history = events

            case Condensation(action=condensation_action):
                return condensation_action
        
        logger.debug(
            f'Processing {len(condensed_history)} events from a total of {len(state.history)} events'
        )
        
        # Process delegate observations to update state - important to do this before LLM call
        # We only need to process observations if we're in the middle of a troubleshooting plan
        if state.incident is not None and state.incident.troubleshooting_plan and not state.incident.all_steps_completed():
            logger.info("Processing delegate observations before LLM call")
            self._process_delegate_observations(state, condensed_history)
        
        # Prepare messages for LLM
        initial_user_message = self._get_initial_user_message(state.history)
        messages = self._get_messages(condensed_history, initial_user_message)
        
        # Call LLM with tools
        params = {
            'messages': self.llm.format_messages_for_llm(messages),
            'tools': check_tools(self.tools, self.llm.config),
            'extra_body': {'metadata': state.to_llm_metadata(agent_name=self.name)}
        }
        response = self.llm.completion(**params)
        logger.debug(f'Response from LLM: {response}')
        
        # Convert response to actions and queue them
        actions = response_to_actions(response)
        logger.debug(f'Actions after response_to_actions: {actions}')
        
        for action in actions:
            self.pending_actions.append(action)
        
        return self.pending_actions.popleft()
    
    def _process_delegate_observations(self, state: State, events: List[Event]) -> None:
        """
        Process observations from delegated agent tasks.
        
        This method looks for AgentDelegateObservation events and updates the
        corresponding steps in the troubleshooting plan with the results.
        
        Args:
            state: The current state of the conversation
            events: List of events to process
        """
        if state.incident is None:
            logger.warning("No incident object found in state")
            return
            
        for event in events:
            if isinstance(event, AgentDelegateObservation):
                # The task ID would be in the outputs, not inputs (which doesn't exist)
                # Try to find the task_id in the outputs
                task_id = None
                if hasattr(event, 'outputs') and isinstance(event.outputs, dict):
                    task_id = event.outputs.get('task_id')
                
                if task_id:
                    logger.info(f"Received observation for delegated task {task_id}")
                    
                    # Get the step result from the observation outputs and ensure it's a string
                    result_value = event.outputs if event.outputs else "No results provided"
                    result_str = str(result_value) if not isinstance(result_value, str) else result_value
                    
                    # Update the step in the incident object
                    if state.incident is not None:
                        state.incident.complete_step(task_id, result_str)
                        logger.debug(f"Updated step {task_id} as completed with result: {result_str}")
    
    def _get_messages(self, events: list[Event], initial_user_message: MessageAction) -> list[Message]:
        """
        Constructs the message history for the LLM conversation by processing events from the state
        and formatting them into messages that the LLM can understand. It handles both regular
        message flow and function-calling scenarios.
        
        This method performs the following steps:
        1. Check for SystemMessageAction in events, adds one if missing (legacy support)
        2. Processes events into a proper conversation flow, including SystemMessageAction
        3. Handles tool calls and their responses in function-calling mode
        4. Manages multi-turn chat interaction (user/assistant/tool responses)
        5. Applies character limits and truncation when necessary to fit context limits
        6. Adds environment reminders for non-function-calling mode
        
        Args:
            events: The list of events to convert to messages
            initial_user_message: The initial user message to include
        
        Returns:
            list[Message]: A list of formatted messages ready for LLM consumption, including:
                - System message with prompt (from SystemMessageAction)
                - Action messages (from both user and assistant)
                - Observation messages (including tool responses)
                - Environment reminders (in non-function-calling mode)
        
        Note:
            - In function-calling mode, tool calls and their responses are carefully tracked
              to maintain proper conversation flow
            - Messages from the same role are combined to prevent consecutive same-role messages
            - When possible, specific messages are cached according to their interpretation
        """
        if not self.prompt_manager:
            raise Exception('Prompt Manager not instantiated.')

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
            
    # All the following parsing methods have been removed as they're no longer needed with the tool-driven approach:
    # - _extract_incident_description
    # - _parse_incident_analysis
    # - _parse_json_plan
    # - _parse_troubleshooting_plan
    # - _parse_root_cause
    #
    # In the tool-driven approach, the LLM returns structured data through tool calls
    # which are processed by the function_calling.py module. No manual parsing of
    # LLM text output is required.
    
    def _get_initial_user_message(self, history: list[Event]) -> MessageAction:
        """Finds the initial user message action from the full history."""
        initial_user_message: MessageAction | None = None
        for event in history:
            if isinstance(event, MessageAction) and event.source == 'user':
                initial_user_message = event
                break

        if initial_user_message is None:
            # This should not happen in a valid conversation
            logger.error(
                f'CRITICAL: Could not find the initial user MessageAction in the full {len(history)} events history.'
            )
            # Depending on desired robustness, could raise error or create a dummy action
            # and log the error
            raise ValueError(
                'Initial user message not found in history. Please report this issue.'
            )
        return initial_user_message
    
