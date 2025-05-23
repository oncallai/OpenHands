"""
This module contains the ArchitectAgent class, which is responsible for analyzing incidents,
delegating troubleshooting steps to available agents, and finding root causes of issues.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

from litellm import ChatCompletionToolParam
from litellm import Message as LiteLLMMessage
import litellm

from openhands.controller.agent import Agent
from openhands.controller.state.state import State
from openhands.core.config import AgentConfig
from openhands.core.logger import openhands_logger as logger
from openhands.events.action import (
    Action,
    AgentDelegateAction,
    AgentFinishAction,
    AgentFinishTaskCompleted,
    MessageAction,
)
from openhands.events.event import Event
from openhands.events.observation import AgentDelegateObservation, Observation
from openhands.llm.llm import LLM, ModelResponse
from openhands.llm.llm_utils import check_tools
from openhands.memory.condenser import Condenser
from openhands.memory.condenser.condenser import Condensation, View
from openhands.runtime.plugins import PluginRequirement
from openhands.utils.conversation_memory import ConversationMemory
from openhands.utils.prompt import Message, PromptManager, TextContent

from openhands.agenthub.architect_agent.function_calling import response_to_actions
from openhands.controller.state.incident import Incident, TroubleshootingStep
from openhands.agenthub.architect_agent.tools import (
    AnalyzeIncidentTool,
    DelegateStepTool,
    RootCauseReportTool,
    ThinkTool,
)


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
        analyze_incident_llm=None
    ) -> None:
        """
        Initialize the ArchitectAgent.
        
        Args:
            llm: The language model to use for analysis
            config: The agent configuration
            analyze_incident_llm: Optional custom LLM for incident analysis
        """
        super().__init__(llm, config)
        
        # Initialize pending actions queue
        self.pending_actions: deque['Action'] = deque()
        
        # Create AnalyzeIncidentLLM tool - use litellm as default if none provided
        self.analyze_incident_llm = analyze_incident_llm if analyze_incident_llm is not None else litellm
        
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
        """Reset the agent state."""
        # Clear pending actions
        self.pending_actions.clear()
        logger.debug("ArchitectAgent state reset")
    
    def step(self, state: State) -> 'Action':
        """
        Process the current state and return the next action.
        
        Args:
            state: The current state of the conversation
            
        Returns:
            The next action to take
        """
        # Initialize the incident in the state if it doesn't exist
        if state.incident is None:
            state.incident = Incident()
            logger.info("Initialized new incident in state")
            
        # Determine the current state for better logging and traceability
        current_state = "initializing"
        
        # Continue with pending actions if any
        if self.pending_actions:
            current_state = "processing_pending_actions"
            logger.info(f"ArchitectAgent state: {current_state} - Processing pending actions")
            return self.pending_actions.popleft()
            
        # Check for exit command
        if self._check_for_exit_command(state.history):
            current_state = "exiting"
            logger.info(f"ArchitectAgent state: {current_state} - User requested exit")
            return AgentFinishAction(
                task_completed=AgentFinishTaskCompleted.TRUE,
                final_thought="The user has requested to exit, so I am terminating the session."
            )
        
        # Process the state history to extract relevant events
        condensed_history = state.history
        
        # Check if we have received any observations from delegated tasks
        current_state = "processing_delegate_observations"
        logger.info(f"ArchitectAgent state: {current_state} - Processing event history")
        self._process_delegate_observations(state, condensed_history)
        
        # FIRST CASE: New iteration - analyze incident
        if state.incident is None or not state.incident.analysis or state.local_iteration == 0:
            current_state = "analyzing_incident"
            logger.info(f"ArchitectAgent state: {current_state} - Analyzing incident (iteration {state.local_iteration + 1})")
            return self._analyze_incident(state, condensed_history)
        
        # SECOND CASE: Check if all steps in the current plan are completed
        if state.incident is not None and state.incident.all_steps_completed():
            # If we have a root cause, finish
            if state.incident is not None and state.incident.root_cause:
                current_state = "creating_root_cause_report"
                logger.info(f"ArchitectAgent state: {current_state} - Creating root cause report")
                return self._create_root_cause_report(state)
            
            # Check if we've reached max iterations
            if state.local_iteration >= state.max_iterations - 1:
                logger.info(f"Reached maximum iterations ({state.local_iteration + 1}). Creating final report.")
                return self._create_final_report(state)
            
            # Otherwise, save current iteration data and start new iteration
            logger.info(f"Completed iteration {state.local_iteration + 1} without finding root cause. Starting new iteration.")
            if state.incident is not None:
                state.incident.save_iteration_data()
                state.incident.analysis = {}  # Reset for next iteration
                state.incident.troubleshooting_plan = []  # Reset for next iteration
                state.incident.delegated_tasks = {}  # Reset delegated tasks
            
            # Start new analysis with previous iteration knowledge
            current_state = "analyzing_incident"
            logger.info(f"ArchitectAgent state: {current_state} - Starting new analysis (iteration {state.local_iteration + 1})")
            return self._analyze_incident(state, condensed_history)
        
        # THIRD CASE: Continue with current plan - delegate next step
        if state.incident is not None and state.incident.troubleshooting_plan:
            # Find the next step that hasn't been delegated yet
            next_step = state.incident.get_next_pending_step()
            
            if next_step:
                current_state = "delegating_step"
                logger.info(f"ArchitectAgent state: {current_state} - Delegating step {next_step.id}")
                
                # Get the agent name for delegation - at this point, we're guaranteed to have a valid agent name
                # since we validate in _parse_json_plan
                agent_name = next_step.agent_name
                logger.info(f"Using agent {agent_name} for step {next_step.id} as specified in the plan")
                
                # Mark the step as delegated in the incident object
                state.incident.delegate_step(next_step.id, agent_name)
                
                # Create inputs for the delegate agent
                inputs = {
                    "task": next_step.description,
                    "context": state.incident.get_summary(),
                    "task_id": next_step.id,
                    "incident_summary": state.incident.get_summary(),
                }
                
                # Log the delegation
                logger.info(f"Delegating step {next_step.id} to {agent_name}: {next_step.description}")
                
                # Return a delegate action directly from the step method
                return AgentDelegateAction(
                    agent=agent_name,
                    inputs=inputs,
                    thought=f"Delegating step {next_step.id}: {next_step.description} to {agent_name} agent."
                )
            else:
                # All steps are delegated but not all completed, wait for results
                current_state = "waiting_for_results"
                logger.info(f"ArchitectAgent state: {current_state} - Waiting for delegated tasks to complete")
                
                # Ensure state.incident is not None before calling format_troubleshooting_plan
                plan_text = ""
                if state.incident is not None:
                    plan_text = state.incident.format_troubleshooting_plan()
                
                return MessageAction(
                    content=f"I'm waiting for the results of the following delegated troubleshooting steps:\n\n{plan_text}"
                )
        completed_count = 0
        total_count = 0
        if state.incident is not None:
            completed_count = sum(1 for step in state.incident.troubleshooting_plan if step.completed)
            total_count = len(state.incident.troubleshooting_plan)
        logger.info(f"ArchitectAgent state: {current_state} - Waiting for delegated tasks to complete ({completed_count}/{total_count})")
        
        # Return a message to the user with the current status
        return MessageAction(
            content=f"I'm waiting for the results of the delegated troubleshooting steps... ({completed_count}/{total_count} completed)"
        )
    
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
        
    def _analyze_incident(self, state: State, events: List[Event]) -> 'Action':
        """
        Analyze the incident based on user messages and create a troubleshooting plan.
        
        Args:
            state: The current state of the conversation
            events: The list of events to analyze
            
        Returns:
            A MessageAction with the analysis and plan
        """
        # Extract the incident description from user messages
        incident_description = self._extract_incident_description(events)
        # Safely truncate incident description
        if incident_description:
            logger.debug(f"Extracted incident description: {incident_description[:100]}...")
        else:
            logger.debug("No incident description extracted")
        
        # Get the available agents for delegation
        available_agents = self.agent_list.get("local", {})
        if not available_agents:
            logger.warning("No local agents available, will use default agents")
            available_agents = {"CodeActAgent": "Handles code-related tasks and command execution"}
        
        # Get agent names for the template
        agent_names = list(available_agents.keys())
        
        # Use the prompt manager to get the message from the template
        if self._prompt_manager is None:
            raise ValueError("Prompt manager is not initialized")
        
        # Prepare template variables including previous iterations data
        template_vars = {
            "incident_description": incident_description,
            "available_agents": available_agents,
            "agent_names": agent_names,
            "current_iteration": state.local_iteration,
            "previous_iterations": state.incident.get_previous_iterations_summary() if state.incident is not None else "No previous iterations."
        }
        
        # Get the system message from the prompt manager
        system_prompt = self._prompt_manager.get_system_message()
        
        # Get the user message from the template
        user_prompt = self._prompt_manager.render_prompt("incident_analysis.j2", template_vars)
        messages = [
            Message(
                role="system",
                content=[TextContent(text=system_prompt)]
            ),
            Message(
                role="user",
                content=[TextContent(text=user_prompt)]
            )
        ]
        
        logger.debug("Sending incident to LLM for analysis with JSON structured output")
        
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
        
        # Parse the JSON response to extract structured plan
        analysis, steps = self._parse_json_plan(response_content, available_agents)
        
        # Update the incident object - ensure incident is not None before proceeding
        if state.incident is None:
            state.incident = Incident()
            logger.info("Created new incident in state during analysis")
            
        state.incident.set_analysis(analysis)
        state.incident.set_troubleshooting_plan(steps)
        
        # Log detailed information about the troubleshooting plan
        logger.info(f"Created incident analysis with {len(state.incident.troubleshooting_plan)} troubleshooting steps")
        for step in state.incident.troubleshooting_plan:
            logger.info(f"Step {step.id}: {step.description[:100]}... (Agent: {step.agent_name})")
        
        # Log the incident analysis summary
        summary = state.incident.get_summary()
        # Safely log the summary with proper handling of potential None value
        if summary:
            logger.info(f"Incident analysis summary: {summary[:150]}...")
        else:
            logger.info("No incident analysis summary available")
        
        # Return a message explaining the analysis and plan
        plan_text = state.incident.format_troubleshooting_plan()
        # Create the message action without the source parameter
        message = MessageAction(
            content=f"I've analyzed the incident. Here's my understanding:\n\n{summary}\n\n"
                    f"I'll now coordinate the following troubleshooting steps:\n\n{plan_text}"
        )
        # Set the source attribute after creation
        setattr(message, '_source', 'agent')
        return message
    
    # The _delegate_step method has been removed as delegation is now handled directly in the step method
    
    def _create_final_report(self, state: State) -> 'Action':
        """
        Create a final report after reaching maximum iterations without finding a root cause.
        
        Args:
            state: The current state of the conversation
            
        Returns:
            An AgentFinishAction with the final report
        """
        # Ensure state.incident is not None before proceeding
        if state.incident is None:
            state.incident = Incident()
            logger.info("Created new incident in state during final report generation")
            
        # Get all iteration data
        iterations_summary = state.incident.get_previous_iterations_summary()
        
        # Save the current iteration data before creating the report
        state.incident.save_iteration_data()
        
        # Compile results from all iterations
        all_results = []
        for i, plan in enumerate(state.incident.iteration_plans):
            iteration_results = []
            for step in plan:
                if step.completed:
                    iteration_results.append({
                        'step_id': step.id,
                        'description': step.description,
                        'result': step.result
                    })
            if iteration_results:
                all_results.extend(iteration_results)
        
        # Create a synthesized conclusion based on all findings
        system_message = "You are an AI architect analyzing incidents and finding patterns in troubleshooting results."
        if self._prompt_manager is not None:
            system_message = self._prompt_manager.get_system_message()
            
        # Format results for the LLM
        results_text = "\n\n".join([
            f"Step {r['step_id']}: {r['description']}\nResult: {r['result']}"
            for r in all_results
        ])
        
        messages = [
            Message(
                role="system",
                content=[TextContent(text=system_message)]
            ),
            Message(
                role="user",
                content=[TextContent(text=f"After {state.local_iteration} iterations, we could not determine a definitive root cause. Synthesize a conclusion from all the information gathered:\n\nIncident Summary: {state.incident.get_summary()}\n\nAll Troubleshooting Results:\n{results_text}")]
            )
        ]
        
        try:
            # Use the centralized LLM interaction method
            response = self._call_llm(messages)
            logger.debug(f"Received response from LLM: {response}")
            
            # Extract the conclusion from the response
            conclusion = ""
            if hasattr(response, 'choices') and response.choices and hasattr(response.choices[0], 'message'):
                conclusion = response.choices[0].message.content
            else:
                conclusion = str(response)
                
            # Set the conclusion as the root cause in the incident object
            state.incident.set_root_cause(conclusion)
        except Exception as e:
            logger.error(f"Error during final report generation: {e}")
            # Provide a fallback conclusion
            fallback_conclusion = f"After {state.local_iteration} iterations, we could not determine a definitive root cause due to an error: {str(e)}\n\n" \
                          f"Based on all the troubleshooting steps, here's what we know: \n" \
                          f"{results_text}"
            state.incident.set_root_cause(fallback_conclusion)
        
        # Return a finish action with the final report
        return AgentFinishAction(
            final_thought=f"After {state.local_iteration} iterations, here is my conclusion:\n\n{state.incident.root_cause}",
            task_completed=AgentFinishTaskCompleted.TRUE,
            outputs={
                "conclusion": state.incident.root_cause, 
                "troubleshooting_results": all_results,
                "iterations_summary": iterations_summary
            }
        )
    
    def _create_root_cause_report(self, state: State) -> 'Action':
        """
        Analyze the results of all troubleshooting steps and create a root cause report.
        
        Args:
            state: The current state of the conversation
            
        Returns:
            An AgentFinishAction with the root cause report
        """
        # Ensure state.incident is not None before proceeding
        if state.incident is None:
            state.incident = Incident()
            logger.info("Created new incident in state during root cause report generation")
            
        # Get completed steps from the incident object
        completed_steps = state.incident.get_completed_steps()
        
        # Collect all the results from the completed steps
        results = []
        for step in completed_steps:
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
                content=[TextContent(text=f"Create a root cause analysis based on the following information:\n\nIncident Summary: {state.incident.get_summary()}\n\nTroubleshooting Results:\n{results_text}")]
            )
        ]
        
        try:
            # Use the centralized LLM interaction method
            response = self._call_llm(messages)
            logger.debug(f"Received response from LLM: {response}")
            
            # Extract the root cause from the response
            root_cause = ""
            if hasattr(response, 'choices') and response.choices and hasattr(response.choices[0], 'message'):
                root_cause = self._parse_root_cause(response.choices[0].message.content)
            else:
                root_cause = self._parse_root_cause(str(response))
                
            # Set the root cause in the incident object
            state.incident.set_root_cause(root_cause)
        except Exception as e:
            logger.error(f"Error during root cause analysis: {e}")
            # Provide a fallback root cause report
            fallback_report = f"Unable to generate a complete root cause analysis due to an error: {str(e)}\n\n" \
                          f"Based on the completed steps, here's what we know: \n" \
                          f"{results_text}"
            state.incident.set_root_cause(fallback_report)
        
        # Log detailed information about the root cause and contributing steps
        if state.incident.root_cause is not None:
            logger.info(f"Determined root cause: {state.incident.root_cause[:100]}...")
        else:
            logger.info("No root cause was determined.")
        
        # Log the completed troubleshooting steps that contributed to the root cause
        logger.info(f"Root cause determination based on {len(completed_steps)}/{len(state.incident.troubleshooting_plan)} completed steps")
        
        for step in completed_steps:
            # Safely truncate strings that might be None
            desc = step.description or ""
            result = str(step.result) if step.result is not None else ""
            
            logger.info(f"Contributing step {step.id}: {desc[:80]}... Result: {result[:100]}...")
        
        # Save the current iteration data before creating the report
        state.incident.save_iteration_data()
        
        # Get the iteration summary for the outputs
        iterations_summary = state.incident.get_previous_iterations_summary()
        
        # Return a finish action with the root cause report
        return AgentFinishAction(
            final_thought=f"After {state.local_iteration + 1} iterations, I've analyzed all the troubleshooting results and determined the root cause of the incident:\n\n{state.incident.root_cause}",
            task_completed=AgentFinishTaskCompleted.TRUE,
            outputs={
                "root_cause": state.incident.root_cause, 
                "troubleshooting_results": results,
                "iterations_summary": iterations_summary
            }
        )
    
    def _extract_incident_description(self, events: List[Event]) -> str:
        """
        Extract the incident description from the event history.
        
        Args:
            events: The list of events to extract the description from
            
        Returns:
            The incident description
        """
        # Find the most recent user message
        for event in reversed(events):
            # Check if it's a MessageAction with source='user'
            if isinstance(event, MessageAction) and getattr(event, 'source', None) == 'user':
                return event.content if hasattr(event, 'content') else ""
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
    
    def _parse_json_plan(self, content: str, available_agents: Dict[str, str]) -> Tuple[Dict[str, Any], List[TroubleshootingStep]]:
        """
        Parse the LLM response to extract a JSON-structured incident analysis and plan.
        
        Args:
            content: The LLM response content containing JSON
            available_agents: Dictionary of available agents
            
        Returns:
            A tuple containing (analysis_dict, troubleshooting_steps)
        """
        # Extract JSON content from the LLM response
        json_content = ""
        json_blocks = re.findall(r'```(?:json)?\s*(.+?)\s*```', content, re.DOTALL)
        
        if json_blocks:
            json_content = json_blocks[0]
        else:
            # Try to find JSON without markdown code blocks
            match = re.search(r'(\{\s*"analysis"\s*:.+?\}\s*$)', content, re.DOTALL)
            if match:
                json_content = match.group(1)
            else:
                # Fallback to using the whole content
                json_content = content
        
        try:
            # Parse the JSON content
            data = json.loads(json_content)
            
            # Extract analysis
            analysis = {
                "summary": data.get("analysis", "No analysis provided"),
                "raw_analysis": content
            }
            
            # Extract troubleshooting steps
            steps = []
            plan_data = data.get("troubleshootingPlan", [])
            
            for i, step_data in enumerate(plan_data):
                step_id = step_data.get("id", str(i+1))
                description = step_data.get("description", f"Step {i+1}")
                
                # Ensure agent_name is present and valid
                if "agentName" not in step_data:
                    raise ValueError(f"Missing required 'agentName' for step {step_id}")
                    
                agent_name = step_data["agentName"]
                
                # Validate agent name against available agents
                if not agent_name or agent_name not in available_agents:
                    raise ValueError(f"Invalid agent name '{agent_name}' for step {step_id}. Must be one of: {list(available_agents.keys())}")
                
                # Create the troubleshooting step
                steps.append(TroubleshootingStep(
                    id=step_id,
                    description=description,
                    agent_name=agent_name
                ))
            
            return analysis, steps
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON plan: {e}")
            # Fallback to regular parsing methods
            analysis = self._parse_incident_analysis(content)
            steps = self._parse_troubleshooting_plan(content)
            return analysis, steps
    
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
                
                # Don't select agent here, let the delegate_step method handle it
                agent_name = "CodeActAgent"  # Default agent
                
                steps.append(TroubleshootingStep(
                    id=str(step_count),
                    description=description,
                    agent_name=agent_name
                ))
        
        # If we couldn't find steps with the above method, try a different approach
        if not steps:
            # Split by double newlines to find paragraphs
            paragraphs = content.split("\n\n")
            for i, paragraph in enumerate(paragraphs):
                if "step" in paragraph.lower() or "check" in paragraph.lower():
                    # Don't select agent here, let the delegate_step method handle it
                    agent_name = "CodeActAgent"  # Default agent
                    steps.append(TroubleshootingStep(
                        id=str(i + 1),
                        description=paragraph,
                        agent_name=agent_name
                    ))
        
        # If we still don't have steps, create some generic ones based on the content
        if not steps:
            default_steps = [
                "Check system logs for errors related to the incident",
                "Verify service status and connectivity",
                "Examine recent changes that might have caused the issue"
            ]
            
            steps = []
            for i, description in enumerate(default_steps):
                # Use a default agent name
                steps.append(TroubleshootingStep(
                    id=str(i+1),
                    description=description,
                    agent_name="CodeActAgent"  # Default agent
                ))
        
        return steps
    
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
    
    def _get_next_undelegated_step(self, state: State) -> Optional[TroubleshootingStep]:
        """
        Get the next step that hasn't been delegated yet.
        
        Args:
            state: The current state of the conversation
            
        Returns:
            The next undelegated step, or None if all steps are delegated
        """
        if state.incident is None:
            return None
            
        for step in state.incident.troubleshooting_plan:
            if not step.delegated:
                return step
        return None
    
    def _all_steps_completed(self, state: State) -> bool:
        """
        Check if all steps in the troubleshooting plan have been completed.
        
        Args:
            state: The current state of the conversation
            
        Returns:
            True if all delegated steps are completed, False otherwise
        """
        # Check if incident exists
        if state.incident is None:
            return False
            
        # Check if there are any steps and if all delegated steps are completed
        return (
            len(state.incident.troubleshooting_plan) > 0 and
            all(step.completed for step in state.incident.troubleshooting_plan if step.delegated)
        )
        
    # The _select_agent_for_task method has been removed as we now use agent names directly from the JSON plan
    
    def _check_for_exit_command(self, events: List[Event]) -> bool:
        """
        Check if the user has requested to exit the session.
        
        Args:
            events: List of events to check
            
        Returns:
            True if the user has requested to exit, False otherwise
        """
        for event in events:
            # Check if it's a MessageAction with source='user'
            if isinstance(event, MessageAction) and getattr(event, 'source', None) == 'user':
                message = event.content.strip() if hasattr(event, 'content') else ''
                if message.lower() == '/exit':
                    return True
        return False
    
    # Second _get_initial_user_message method removed - already defined above
        
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
