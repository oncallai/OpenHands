"""
This module contains the ArchitectAgent class, which is responsible for analyzing incidents,
delegating troubleshooting steps to available agents, and finding root causes of issues.
"""

import os
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from openhands.events.action import Action
    from openhands.llm.llm import ModelResponse

from litellm import ChatCompletionToolParam
from openhands.agenthub.architect_agent.function_calling import response_to_actions
from openhands.agenthub.architect_agent.tools import (
    AnalyzeIncidentTool,
    RootCauseReportTool,
    DelegateStepTool,
    ThinkTool
)

from openhands.controller.agent import Agent
from openhands.controller.state.state import State
from openhands.core.config import AgentConfig
from openhands.core.logger import openhands_logger as logger
from openhands.core.message import Message, TextContent
from openhands.memory.conversation_memory import ConversationMemory
from openhands.events.action import (
    AgentDelegateAction,
    AgentFinishAction,
    MessageAction,
)
from openhands.events.action.agent import AgentFinishTaskCompleted
from openhands.events.event import Event
from openhands.events.observation import AgentDelegateObservation, Observation
from openhands.llm.llm import LLM
from openhands.llm.llm_utils import check_tools
from openhands.runtime.plugins import PluginRequirement
from openhands.utils.prompt import PromptManager
from openhands.memory.condenser import Condenser
from openhands.memory.condenser.condenser import Condensation, View


@dataclass
class TroubleshootingStep:
    """Represents a single troubleshooting step in the incident analysis process."""
    
    id: str
    description: str
    agent_type: str
    delegated: bool = False
    completed: bool = False
    result: Optional[Dict[str, Any]] = None


class ArchitectAgent(Agent):
    """
    The Architect Agent is responsible for analyzing incidents, delegating troubleshooting 
    steps to available agents, and finding root causes of issues. It does not execute steps 
    itself but coordinates the work of other agents.
    
    The agent works by:
    1. Analyzing the incident to understand its nature
    2. Creating a troubleshooting plan with specific steps
    3. Delegating each step to an appropriate agent
    4. Collecting and analyzing the results
    5. Determining the root cause
    6. (Future) Creating and delegating resolution steps
    
    This agent follows the same pattern as other OpenHands agents but specializes in
    coordination rather than direct execution.
    """
    
    VERSION = '1.0'
    
    # No sandbox plugins needed as this agent doesn't execute code directly
    sandbox_plugins: List[PluginRequirement] = []
    
    def __init__(
        self, 
        llm: LLM, 
        config: AgentConfig,
    ) -> None:
        """
        Initialize the ArchitectAgent.
        
        Args:
            llm: The language model to use for analysis
            config: The agent configuration
        """
        super().__init__(llm, config)
        
        # Initialize pending actions queue
        self.pending_actions: deque['Action'] = deque()
        
        # Initialize state variables
        self.incident_analysis: Dict[str, Any] = {}
        self.troubleshooting_plan: List[TroubleshootingStep] = []
        self.delegated_tasks: Dict[str, Dict[str, Any]] = {}
        self.root_cause: Optional[str] = None
        
        # Reset the agent to ensure all variables are properly initialized
        self.reset()
        
        # Set up tools
        self.tools = self._get_tools()
        
        # Create conversation memory
        self.conversation_memory = ConversationMemory(self.config, self.prompt_manager)
        
        # Initialize condenser for history management
        self.condenser = Condenser.from_config(self.config.condenser)
        logger.debug(f'Using condenser: {type(self.condenser)}')
        
        # Load the agent list from the centralized location
        self.agent_list = AgentConfig.load_agent_list()
        
        logger.info(f"ArchitectAgent {self.VERSION} initialized")
    
    @property
    def prompt_manager(self) -> PromptManager:
        if self._prompt_manager is None:
            self._prompt_manager = PromptManager(
                prompt_dir=os.path.join(os.path.dirname(__file__), 'prompts'),
            )
        return self._prompt_manager
    
    # TODO: flags needs to be added to config for tools enable
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
        """Reset the agent's state."""
        super().reset()
        self.incident_analysis = {}
        self.troubleshooting_plan = []
        self.delegated_tasks = {}
        self.root_cause = None
        self.pending_actions.clear()
        logger.debug("ArchitectAgent state reset")
    
    def step(self, state: State) -> 'Action':
        """
        Perform one step of the architect agent's workflow.
        
        This method is called by the controller to get the next action from the agent.
        The agent's behavior depends on its current state in the workflow:
        1. If no incident analysis exists, analyze the incident
        2. If there are undelegated steps, delegate the next step
        3. If all steps are delegated and completed, create a root cause report
        4. Otherwise, wait for delegated tasks to complete
        
        Args:
            state: The current state containing events and other information
            
        Returns:
            An Action for the controller to execute
        """
        # Determine the current state for better logging and traceability
        current_state = "initializing"
        
        # Continue with pending actions if any
        if self.pending_actions:
            current_state = "processing_pending_actions"
            logger.info(f"ArchitectAgent state: {current_state} - Processing pending actions")
            return self.pending_actions.popleft()
            
        # Check for exit command
        latest_user_message = state.get_last_user_message()
        if latest_user_message and latest_user_message.content.strip() == '/exit':
            current_state = "exiting"
            logger.info(f"ArchitectAgent state: {current_state} - User requested exit")
            return AgentFinishAction()
            
        # Use condenser to manage event history
        current_state = "condensing_history"
        logger.info(f"ArchitectAgent state: {current_state} - Processing event history")
        condensed_history: list[Event] = []
        match self.condenser.condensed_history(state):
            case View(events=events):
                condensed_history = events
                
            case Condensation(action=condensation_action):
                logger.info(f"ArchitectAgent state: condensation_action - Returning condensation action")
                return condensation_action
                
        logger.debug(
            f'Processing {len(condensed_history)} events from a total of {len(state.history)} events'
        )
            
        # Process any delegate observations
        self._process_delegate_observations(condensed_history)
        
        # Get the initial user message
        initial_user_message = self._get_initial_user_message(state.history)
        
        # If we haven't analyzed the incident yet, do that first
        if not self.incident_analysis:
            current_state = "analyzing_incident"
            logger.info(f"ArchitectAgent state: {current_state} - Analyzing incident")
            action = self._analyze_incident(condensed_history)
            if action:
                self.pending_actions.append(action)
                return self.pending_actions.popleft()
        
        # If there are undelegated steps, delegate the next one
        next_step = self._get_next_undelegated_step()
        if next_step:
            current_state = "delegating_step"
            logger.info(f"ArchitectAgent state: {current_state} - Delegating step {next_step.id}: {next_step.description}")
            action = self._delegate_step(next_step)
            if action:
                self.pending_actions.append(action)
                return self.pending_actions.popleft()
        
        # If we've received results from all delegated tasks, analyze them and create a root cause report
        if self._all_steps_completed() and not self.root_cause:
            current_state = "creating_report"
            logger.info(f"ArchitectAgent state: {current_state} - Creating root cause report")
            action = self._create_root_cause_report()
            if action:
                self.pending_actions.append(action)
                return self.pending_actions.popleft()
        
        # Otherwise, wait for more information
        current_state = "waiting_for_results"
        completed_count = sum(1 for step in self.troubleshooting_plan if step.completed)
        total_count = len(self.troubleshooting_plan)
        logger.info(f"ArchitectAgent state: {current_state} - Waiting for delegated tasks to complete ({completed_count}/{total_count})")
        
        
        message = MessageAction(
            content=f"I'm waiting for the results of the delegated troubleshooting steps... ({completed_count}/{total_count} completed)"
        )
        # Set the source attribute after creation
        setattr(message, '_source', 'agent')
        return message
    
    def _process_delegate_observations(self, events: List[Event]) -> None:
        """
        Process observations from delegated agents to update our internal state.
        
        Args:
            events: The list of events to process
        """
        for event in events:
            if isinstance(event, AgentDelegateObservation):
                # Check if this observation is for one of our delegated tasks
                # The task_id should be in the outputs dictionary
                task_id = event.outputs.get('task_id')
                if task_id and task_id in self.delegated_tasks:
                    logger.info(f"Received observation for delegated task {task_id}")
                    
                    # Update the task status
                    self.delegated_tasks[task_id]['completed'] = True
                    self.delegated_tasks[task_id]['result'] = event.outputs
                    
                    # Update the corresponding step in the troubleshooting plan
                    for step in self.troubleshooting_plan:
                        if step.id == task_id:
                            step.completed = True
                            step.result = event.outputs
                            logger.debug(f"Updated step {step.id} as completed with result: {event.outputs}")
                            break
    
    def _get_messages(self, events: list[Event], initial_user_message: MessageAction) -> list[Message]:
        """
        Constructs the message history for the LLM conversation.

        This method builds a structured conversation history by processing events from the state
        and formatting them into messages that the LLM can understand.

        Args:
            events: The list of events to convert to messages
            initial_user_message: The initial user message that started the conversation

        Returns:
            list[Message]: A list of formatted messages ready for LLM consumption
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
        
    def _analyze_incident(self, events: List[Event]) -> 'Action':
        """
        Analyze the incident based on user messages and create a troubleshooting plan.
        
        Args:
            events: The list of events to analyze
            
        Returns:
            A MessageAction with the analysis and plan
        """
        # Extract the incident description from user messages
        incident_description = self._extract_incident_description(events)
        logger.debug(f"Extracted incident description: {incident_description[:100]}...")
        
        # Get the initial user message
        initial_user_message = self._get_initial_user_message(events)
        
        # Get messages using our standard message construction
        messages = self._get_messages(events, initial_user_message)
        
        logger.debug("Sending incident to LLM for analysis")
        
        try:
            # Use the centralized LLM interaction method
            response = self._call_llm(messages)
            logger.debug(f"Received response from LLM: {response}")
            
            # The response is a ModelResponse object, extract the message content
            response_content = response.choices[0].message.content if hasattr(response, 'choices') else str(response)
        except Exception as e:
            logger.error(f"Error during incident analysis: {e}")
            # Create an error message for the user
            message = MessageAction(
                content=f"I encountered an issue while analyzing the incident: {str(e)}\n\n"
                        f"Please try again or provide more details about the incident."
            )
            setattr(message, '_source', 'agent')
            return message
        
        # Parse the response to extract the analysis and troubleshooting steps
        self.incident_analysis = self._parse_incident_analysis(response_content)
        self.troubleshooting_plan = self._parse_troubleshooting_plan(response_content)
        
        # Log detailed information about the troubleshooting plan
        logger.info(f"Created incident analysis with {len(self.troubleshooting_plan)} troubleshooting steps")
        for step in self.troubleshooting_plan:
            logger.info(f"Step {step.id}: {step.description[:100]}... (Agent: {step.agent_type})")
        
        # Log the incident analysis summary
        if 'summary' in self.incident_analysis:
            logger.info(f"Incident analysis summary: {self.incident_analysis['summary'][:150]}...")
        
        # Return a message explaining the analysis and plan
        plan_text = self._format_troubleshooting_plan()
        # Create the message action without the source parameter
        message = MessageAction(
            content=f"I've analyzed the incident. Here's my understanding:\n\n{self.incident_analysis.get('summary', '')}\n\n"
                    f"I'll now coordinate the following troubleshooting steps:\n\n{plan_text}"
        )
        # Set the source attribute after creation
        setattr(message, '_source', 'agent')
        return message
    
    def _delegate_step(self, step: TroubleshootingStep) -> 'Action':
        """
        Delegate a troubleshooting step to an appropriate agent.
        
        Args:
            step: The step to delegate
            
        Returns:
            An AgentDelegateAction to delegate the step
        """
        # Mark the step as delegated
        step.delegated = True
        
        # Create inputs for the delegate agent
        inputs = {
            "task": step.description,
            "context": self.incident_analysis.get('summary', ''),
            "task_id": step.id  # Include the task ID so we can track it
        }
        
        # Track this delegation in our internal state
        self.delegated_tasks[step.id] = {
            'step': {
                'id': step.id,
                'description': step.description,
                'agent_type': step.agent_type
            },
            'delegated_at': time.time(),
            'completed': False,
            'result': None
        }
        
        logger.info(f"Delegating step {step.id} to {step.agent_type}: {step.description}")
        
        # Return a delegate action
        return AgentDelegateAction(
            agent=step.agent_type,
            inputs=inputs,
            thought=f"Delegating step {step.id}: {step.description} to {step.agent_type} agent."
        )
    
    def _create_root_cause_report(self) -> 'Action':
        """
        Analyze the results of all troubleshooting steps and create a root cause report.
        
        Returns:
            An AgentFinishAction with the root cause report
        """
        # Collect all the results from the delegated tasks
        results = []
        for step in self.troubleshooting_plan:
            if step.completed and step.result:
                results.append({
                    'step_id': step.id,
                    'description': step.description,
                    'result': step.result
                })
        
        # Format the results for the LLM
        results_text = "\n\n".join([
            f"Step {r['step_id']}: {r['description']}\nResult: {r['result']}"
            for r in results
        ])
        
        logger.debug(f"Sending troubleshooting results to LLM for root cause analysis: {len(results)} results")
        
        # In this context, we don't need the initial user message since we're not using _get_messages
        # We're directly creating the messages using prompt_manager.get_message
        
        # Create messages directly
        system_message = "You are an AI architect analyzing incidents and finding root causes."
        if self._prompt_manager is not None:
            system_message = self._prompt_manager.get_system_message()
            
        messages = [
            Message(
                role="system",
                content=[TextContent(text=system_message)]
            ),
            Message(
                role="user",
                content=[TextContent(text=f"Create a root cause analysis based on the following information:\n\nIncident Summary: {self.incident_analysis.get('summary', '')}\n\nTroubleshooting Results:\n{results_text}")]
            )
        ]
        
        try:
            # Use the centralized LLM interaction method
            response = self._call_llm(messages)
            logger.debug(f"Received response from LLM: {response}")
            
            # Extract the root cause from the response
            if hasattr(response, 'choices') and response.choices and hasattr(response.choices[0], 'message'):
                self.root_cause = self._parse_root_cause(response.choices[0].message.content)
            else:
                self.root_cause = self._parse_root_cause(str(response))
        except Exception as e:
            logger.error(f"Error during root cause analysis: {e}")
            # Provide a fallback root cause report
            self.root_cause = f"Unable to generate a complete root cause analysis due to an error: {str(e)}\n\n" \
                        f"Based on the completed steps, here's what we know: \n" \
                        f"{results_text}"
        
        # Log detailed information about the root cause and contributing steps
        logger.info(f"Determined root cause: {self.root_cause[:100]}...")
        
        # Log the completed troubleshooting steps that contributed to the root cause
        completed_steps = [step for step in self.troubleshooting_plan if step.completed]
        logger.info(f"Root cause determination based on {len(completed_steps)}/{len(self.troubleshooting_plan)} completed steps")
        
        for step in completed_steps:
            logger.info(f"Contributing step {step.id}: {step.description[:80]}... Result: {str(step.result)[:100]}...")
        
        # Return a finish action with the root cause report
        return AgentFinishAction(
            final_thought=f"I've analyzed all the troubleshooting results and determined the root cause of the incident:\n\n{self.root_cause}",
            task_completed=AgentFinishTaskCompleted.TRUE,
            outputs={"root_cause": self.root_cause, "troubleshooting_results": results}
        )
    
    def _extract_incident_description(self, events: List[Event]) -> str:
        """
        Extract the incident description from user messages.
        
        Args:
            events: The list of events to extract from
            
        Returns:
            The incident description as a string
        """
        # Find the most recent user message
        for event in reversed(events):
            if isinstance(event, MessageAction) and event.source == "user":
                return event.content
        return "No incident description found."
    
    def _parse_incident_analysis(self, content: str) -> Dict[str, Any]:
        """
        Parse the LLM response to extract incident analysis.
        
        In a real implementation, this would use structured output from the LLM.
        For now, we'll use a simple approach.
        
        Args:
            content: The LLM response content
            
        Returns:
            A dictionary with the incident analysis
        """
        # Simple parsing - in production, use structured output
        sections = content.split("\n\n")
        summary = sections[0] if sections else content
        
        return {
            "summary": summary,
            "raw_analysis": content
        }
    
    def _parse_troubleshooting_plan(self, content: str) -> List[TroubleshootingStep]:
        """
        Parse the LLM response to extract troubleshooting steps.
        
        In a real implementation, this would use structured output from the LLM.
        For now, we'll extract steps based on simple patterns.
        
        Args:
            content: The LLM response content
            
        Returns:
            A list of TroubleshootingStep objects
        """
        # Simple parsing - in production, use structured output
        steps = []
        
        # Look for numbered steps in the content
        lines = content.split("\n")
        step_count = 0
        
        for line in lines:
            # Look for lines that start with a number and a period or parenthesis
            if (line.strip().startswith(str(step_count + 1) + ".") or 
                line.strip().startswith(str(step_count + 1) + ")") or
                line.strip().startswith("Step " + str(step_count + 1))):
                
                step_count += 1
                description = line.split(".", 1)[1].strip() if "." in line else line.split(")", 1)[1].strip()
                
                # Select the appropriate agent using LLM
                incident_context = self.incident_analysis.get('summary', '')
                agent_type = self._select_agent_for_task(description, incident_context)
                
                steps.append(TroubleshootingStep(
                    id=str(step_count),
                    description=description,
                    agent_type=agent_type
                ))
        
        # If we couldn't find steps with the above method, try a different approach
        if not steps:
            # Split by double newlines to find paragraphs
            paragraphs = content.split("\n\n")
            for i, paragraph in enumerate(paragraphs):
                if "step" in paragraph.lower() or "check" in paragraph.lower():
                    # Select agent type using LLM
                    incident_context = "System incident requiring troubleshooting."
                    agent_type = self._select_agent_for_task(paragraph, incident_context)
                    steps.append(TroubleshootingStep(
                        id=str(i + 1),
                        description=paragraph,
                        agent_type=agent_type
                    ))
        
        # If we still don't have steps, create some generic ones based on the content
        if not steps:
            default_steps = [
                "Check system logs for errors related to the incident",
                "Verify service status and connectivity",
                "Examine recent changes that might have caused the issue"
            ]
            
            # Get a brief context for better agent selection
            incident_context = "System incident requiring troubleshooting. No specific details available."
            
            steps = []
            for i, description in enumerate(default_steps):
                agent_type = self._select_agent_for_task(description, incident_context)
                steps.append(TroubleshootingStep(
                    id=str(i+1),
                    description=description,
                    agent_type=agent_type
                ))
        
        return steps
    
    def _select_agent_for_task(self, step_description: str, incident_context: str = "") -> str:
        """
        Use the LLM to select the most appropriate agent for a troubleshooting step.
        
        Args:
            step_description: The description of the troubleshooting step
            incident_context: Optional context about the incident for better agent selection
            
        Returns:
            The name of the selected agent
        """
        try:
            # Only use local agents for now
            local_agents = self.agent_list.get("local", {})
            
            if not local_agents:
                logger.warning("No local agents available for selection, defaulting to CodeActAgent")
                return "CodeActAgent"
            
            # Create messages directly
            messages = [
                Message(
                    role="system",
                    content=[TextContent(text="You are a task router that selects the most appropriate agent for a given task based on the agent's expertise.")]
                ),
                Message(
                    role="user",
                    content=[TextContent(text=f"Select the most appropriate agent for the following task:\n\nStep Description: {step_description}\n\nIncident Context: {incident_context}\n\nAvailable Agents: {json.dumps(local_agents, indent=2)}\n\nRespond with ONLY the agent name, nothing else.")]
                )
            ]
            
            try:
                # Use the centralized LLM interaction method
                response = self._call_llm(messages)
                
                # Extract agent name from response
                agent_name = response.content.strip() if response.content else ""
            except Exception as e:
                logger.error(f"Error selecting agent for task: {e}")
                # Default to CodeActAgent on error
                logger.warning(f"Error during agent selection, defaulting to CodeActAgent: {e}")
                return "CodeActAgent"
            
            # Validate agent name
            if agent_name in local_agents:
                logger.info(f"LLM selected agent {agent_name} for task: {step_description[:50]}...")
                return agent_name
            else:
                logger.warning(f"LLM selected invalid agent '{agent_name}', defaulting to CodeActAgent")
                return "CodeActAgent"
                
        except Exception as e:
            logger.error(f"Error selecting agent: {e}. Defaulting to CodeActAgent")
            return "CodeActAgent"
    
    def _parse_root_cause(self, content: str) -> str:
        """
        Parse the LLM response to extract the root cause.
        
        Args:
            content: The LLM response content
            
        Returns:
            The root cause as a string
        """
        # For now, just return the content as the root cause
        return content
    
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
    
    def _get_next_undelegated_step(self) -> Optional[TroubleshootingStep]:
        """
        Get the next step that hasn't been delegated yet.
        
        Returns:
            The next undelegated step, or None if all steps are delegated
        """
        for step in self.troubleshooting_plan:
            if not step.delegated:
                return step
        return None
    
    def _all_steps_completed(self) -> bool:
        """
        Check if all steps in the troubleshooting plan have been completed.
        
        Returns:
            True if all delegated steps are completed, False otherwise
        """
        # Check if there are any steps and if all delegated steps are completed
        return (
            len(self.troubleshooting_plan) > 0 and
            all(step.completed for step in self.troubleshooting_plan if step.delegated)
        )
    
    def _format_troubleshooting_plan(self) -> str:
        """
        Format the troubleshooting plan for display.
        
        Returns:
            A formatted string representation of the plan
        """
        result = ""
        for step in self.troubleshooting_plan:
            status = ""
            if step.completed:
                status = " [COMPLETED]"
            elif step.delegated:
                status = " [DELEGATED]"
            
            result += f"{step.id}. {step.description}{status}\n"
        return result
        
    def _call_llm(self, messages: List[Message], metadata: Optional[Dict[str, Any]] = None) -> 'ModelResponse':
        """
        Centralized method for all LLM interactions with consistent parameter handling.
        
        Args:
            messages: The messages to send to the LLM
            metadata: Optional metadata to include in the request
            
        Returns:
            The LLM response
            
        Raises:
            Exception: If there's an error during the LLM call
        """
        # Create standard parameters structure
        params = {
            'messages': self.llm.format_messages_for_llm(messages),
            'tools': check_tools(self.tools, self.llm.config),
            'extra_body': {'metadata': metadata or {'agent_name': self.name}}
        }
        
        try:
            response = self.llm.completion(**params)
            return response
        except Exception as e:
            logger.error(f"Error during LLM call: {str(e)}")
            raise
    
    def response_to_actions(self, response: 'ModelResponse') -> List['Action']:
        """
        Convert a model response to a list of actions.
        
        Args:
            response: The model response to convert
            
        Returns:
            A list of actions derived from the response
        """
        return response_to_actions(
            response, mcp_tool_names=list(self.mcp_tools.keys())
        )
