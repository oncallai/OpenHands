"""
This file contains the function calling implementation for ArchitectAgent actions.

This is similar to the functionality in CodeActAgent's function_calling.py.
"""

import json
from typing import Dict, List, Any, TYPE_CHECKING, Optional, cast

from litellm import (
    ModelResponse, 
    ChatCompletionToolParam
)

from openhands.agenthub.architect_agent.tools import (
    DelegateTaskToAgentTool,
    ThinkTool
)
from openhands.core.exceptions import (
    FunctionCallNotExistsError,
    FunctionCallValidationError,
)
from openhands.controller.agent import Agent
from openhands.core.logger import openhands_logger as logger
from openhands.events.action import (
    Action,
    AgentDelegateAction,
    AgentFinishAction,
    AgentThinkAction,
    MessageAction,
)
from openhands.events.action.agent import AgentFinishTaskCompleted
from openhands.events.action.mcp import MCPAction
from openhands.events.tool import ToolCallMetadata


def combine_thought(action: Action, thought: str) -> Action:
    if not hasattr(action, 'thought'):
        return action
    if thought and action.thought:
        action.thought = f'{thought}\n{action.thought}'
    elif thought:
        action.thought = thought
    return action


def response_to_actions(
    response: ModelResponse, mcp_tool_names: list[str] | None = None
) -> list[Action]:
    actions: list[Action] = []
    assert len(response.choices) == 1, 'Only one choice is supported for now'
    choice = response.choices[0]
    assistant_msg = choice.message
    if hasattr(assistant_msg, 'tool_calls') and assistant_msg.tool_calls:
        # Check if there's assistant_msg.content. If so, add it to the thought
        thought = ''
        if isinstance(assistant_msg.content, str):
            thought = assistant_msg.content
        elif isinstance(assistant_msg.content, list):
            for msg in assistant_msg.content:
                if msg['type'] == 'text':
                    thought += msg['text']

        # Process each tool call to OpenHands action
        for i, tool_call in enumerate(assistant_msg.tool_calls):
            action: Action
            logger.debug(f'Tool call in function_calling.py: {tool_call}')
            try:
                arguments = json.loads(tool_call.function.arguments)
            except json.decoder.JSONDecodeError as e:
                raise FunctionCallValidationError(
                    f'Failed to parse tool call arguments: {tool_call.function.arguments}'
                ) from e

            # ================================================
            # DelegateTaskToAgentTool
            # ================================================
            if tool_call.function.name == DelegateTaskToAgentTool['function']['name']:
                if 'task_id' not in arguments:
                    raise FunctionCallValidationError(
                        f'Missing required argument "task_id" in tool call {tool_call.function.name}'
                    )
                if 'agent_type' not in arguments:
                    raise FunctionCallValidationError(
                        f'Missing required argument "agent_type" in tool call {tool_call.function.name}'
                    )
                
                # Validate agent_type against registered agents
                agent_type = arguments.get('agent_type', '')
                available_agents = Agent.list_agents()  # This returns a list of strings, not a dictionary
                
                if agent_type not in available_agents:
                    valid_agents = ', '.join(available_agents)  # available_agents is already a list of strings
                    raise FunctionCallValidationError(
                        f'Agent type "{agent_type}" is not registered. Valid agents are: {valid_agents}'
                    )
                
                # Create proper delegate action to the agent type
                action = AgentDelegateAction(
                    agent=agent_type,
                    inputs=arguments,
                )
                
            # ================================================
            # ThinkTool
            # ================================================
            elif tool_call.function.name == ThinkTool['function']['name']:
                action = AgentThinkAction(thought=arguments.get('thought', ''))
                
            # ================================================
            # MCPAction (MCP)
            # ================================================
            elif mcp_tool_names and tool_call.function.name in mcp_tool_names:
                action = MCPAction(
                    name=tool_call.function.name,
                    arguments=arguments,
                )
            else:
                raise FunctionCallNotExistsError(
                    f'Tool {tool_call.function.name} is not registered. (arguments: {arguments}). Please check the tool name and retry with an existing tool.'
                )

            # We only add thought to the first action
            if i == 0:
                action = combine_thought(action, thought)
            # Add metadata for tool calling
            action.tool_call_metadata = ToolCallMetadata(
                tool_call_id=tool_call.id,
                function_name=tool_call.function.name,
                model_response=response,
                total_calls_in_response=len(assistant_msg.tool_calls),
            )
            actions.append(action)
    else:
        # Check if the response indicates task completion
        if _is_finish_message(assistant_msg.content):
            actions.append(
                AgentFinishAction(
                    final_thought=assistant_msg.content,
                    task_completed=AgentFinishTaskCompleted.TRUE
                )
            )
        else:
            # Otherwise, it's a regular message
            actions.append(
                MessageAction(
                    content=str(assistant_msg.content) if assistant_msg.content else '',
                    wait_for_response=True,
                )
            )

    # Add response id to actions
    for action in actions:
        action.response_id = response.id

    assert len(actions) >= 1
    return actions


def _is_finish_message(content: str) -> bool:
    """
    Check if a message indicates the task is complete.
    
    Args:
        content: The message content to check
        
    Returns:
        True if the message indicates the task is complete, False otherwise
    """
    # This is a simplified implementation; you might want to use a more sophisticated method
    if not content:
        return False
    
    # Define keywords/phrases that indicate task completion
    completion_phrases = [
        "task complete", 
        "I'm done", 
        "finished", 
        "completed the task",
        "task is finished",
        "task has been completed",
        "all steps are completed",
        "all done",
        "root cause identified",
        "root cause determined",
        "solution found",
        "issue resolved",
        "problem solved",
        "incident analysis complete",
        "troubleshooting complete",
        "analysis complete"
    ]
    
    # Check if any of the completion phrases are in the message
    content_lower = content.lower()
    for phrase in completion_phrases:
        if phrase in content_lower:
            return True
    
    return False
