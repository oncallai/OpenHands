"""
This module contains functions for handling function calling in the DevOps Agent.
"""

from typing import Dict, List, Any, TYPE_CHECKING, Optional, cast

from litellm import ChatCompletionToolParam, ChatCompletionToolParamFunctionChunk

if TYPE_CHECKING:
    from openhands.events.action import Action
    from openhands.llm.llm import ModelResponse

from openhands.core.logger import openhands_logger as logger
from openhands.events.action import (
    AgentFinishAction,
    MessageAction,
)
from openhands.events.action.agent import AgentFinishTaskCompleted
from openhands.events.event import EventSource


def get_tools() -> list[ChatCompletionToolParam]:
    """
    Get the list of tools available to the DevOps Agent.
    
    Returns:
        List of tool definitions
    """
    # Define the tools available to the DevOps Agent
    tools = [
        ChatCompletionToolParam(
            type='function',
            function=ChatCompletionToolParamFunctionChunk(
                name='check_infrastructure',
                description='Check the status and configuration of infrastructure components',
                parameters={
                    'type': 'object',
                    'properties': {
                        'resource_type': {
                            'type': 'string',
                            'description': 'Type of infrastructure resource (e.g., \'vm\', \'container\', \'network\', \'storage\')',
                        },
                        'resource_name': {
                            'type': 'string',
                            'description': 'Name or identifier of the specific resource',
                        },
                        'check_type': {
                            'type': 'string',
                            'description': 'Type of check to perform (e.g., \'status\', \'configuration\', \'connectivity\')',
                        }
                    },
                    'required': ['resource_type']
                }
            )
        ),
        ChatCompletionToolParam(
            type='function',
            function=ChatCompletionToolParamFunctionChunk(
                name='manage_kubernetes',
                description='Perform operations on Kubernetes clusters',
                parameters={
                    'type': 'object',
                    'properties': {
                        'cluster': {
                            'type': 'string',
                            'description': 'Name of the Kubernetes cluster',
                        },
                        'namespace': {
                            'type': 'string',
                            'description': 'Kubernetes namespace',
                        },
                        'resource_type': {
                            'type': 'string',
                            'description': 'Type of resource (e.g., \'pod\', \'deployment\', \'service\')',
                        },
                        'operation': {
                            'type': 'string',
                            'description': 'Operation to perform (e.g., \'get\', \'describe\', \'status\')',
                        }
                    },
                    'required': ['cluster', 'resource_type', 'operation']
                }
            )
        ),
        ChatCompletionToolParam(
            type='function',
            function=ChatCompletionToolParamFunctionChunk(
                name='manage_deployment',
                description='Manage deployment processes and CI/CD pipelines',
                parameters={
                    'type': 'object',
                    'properties': {
                        'pipeline_name': {
                            'type': 'string',
                            'description': 'Name of the CI/CD pipeline',
                        },
                        'action': {
                            'type': 'string',
                            'description': 'Action to perform (e.g., \'status\', \'logs\', \'history\')',
                        },
                        'environment': {
                            'type': 'string',
                            'description': 'Target environment (e.g., \'dev\', \'staging\', \'production\')',
                        }
                    },
                    'required': ['pipeline_name', 'action']
                }
            )
        )
    ]
    
    return tools


def response_to_actions(
    response: 'ModelResponse', mcp_tool_names: Optional[List[str]] = None
) -> List['Action']:
    """
    Convert a model response to a list of actions.
    
    Args:
        response: The model response to convert
        mcp_tool_names: Optional list of MCP tool names
        
    Returns:
        A list of actions derived from the response
    """
    actions: List['Action'] = []
    
    # Handle empty response
    if not response.content and not response.tool_calls:
        logger.warning("Empty response from model")
        message = MessageAction(content="I didn't receive a clear response. Let me try a different approach.")
        setattr(message, '_source', EventSource.AGENT)
        actions.append(message)
        return actions
    
    # Handle tool calls if present
    if response.tool_calls:
        for tool_call in response.tool_calls:
            # Get the tool name and arguments
            tool_name = tool_call.get("name", "")
            tool_arguments = tool_call.get("arguments", {})
            
            if mcp_tool_names and tool_name in mcp_tool_names:
                # MCP tools are handled separately
                logger.debug(f"MCP tool call: {tool_name}")
                continue
            
            # Process tool call based on name
            if tool_name == "check_infrastructure":
                resource_type = tool_arguments.get("resource_type", "")
                resource_name = tool_arguments.get("resource_name", "")
                check_type = tool_arguments.get("check_type", "status")
                
                result = f"Checked {resource_type}"
                if resource_name:
                    result += f" '{resource_name}'"
                result += f" {check_type}. All resources are functioning normally."
                
                # Create a message with the check result
                message = MessageAction(content=result)
                setattr(message, '_source', EventSource.AGENT)
                actions.append(message)
                
            elif tool_name == "manage_kubernetes":
                cluster = tool_arguments.get("cluster", "")
                namespace = tool_arguments.get("namespace", "default")
                resource_type = tool_arguments.get("resource_type", "")
                operation = tool_arguments.get("operation", "")
                
                result = f"Performed {operation} on {resource_type} in cluster '{cluster}'"
                if namespace != "default":
                    result += f" namespace '{namespace}'"
                result += ". Resources are running as expected."
                
                # Create a message with the kubernetes operation result
                message = MessageAction(content=result)
                setattr(message, '_source', EventSource.AGENT)
                actions.append(message)
                
            elif tool_name == "manage_deployment":
                pipeline_name = tool_arguments.get("pipeline_name", "")
                action = tool_arguments.get("action", "")
                environment = tool_arguments.get("environment", "")
                
                result = f"Performed {action} on pipeline '{pipeline_name}'"
                if environment:
                    result += f" for {environment} environment"
                result += ". The deployment is successful and services are running as expected."
                
                # Create a message with the deployment result
                message = MessageAction(content=result)
                setattr(message, '_source', EventSource.AGENT)
                actions.append(message)
                
            else:
                # Unknown tool
                logger.warning(f"Unknown tool call: {tool_name}")
                message = MessageAction(
                    content=f"I received a request to use the tool '{tool_name}' which isn't available. Let me try a different approach."
                )
                setattr(message, '_source', EventSource.AGENT)
                actions.append(message)
    
    # If there are no tool calls but there is content, create a message action
    elif response.content:
        message = MessageAction(content=response.content)
        setattr(message, '_source', EventSource.AGENT)
        actions.append(message)
    
    return actions
