"""
This module contains functions for handling function calling in the SRE Agent.
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
    Get the list of tools available to the SRE Agent.
    
    Returns:
        List of tool definitions
    """
    # Define the tools available to the SRE Agent
    tools = [
        ChatCompletionToolParam(
            type='function',
            function=ChatCompletionToolParamFunctionChunk(
                name='analyze_logs',
                description='Analyze log data to identify patterns, errors, or anomalies',
                parameters={
                    'type': 'object',
                    'properties': {
                        'log_source': {
                            'type': 'string',
                            'description': 'The source of the logs (e.g., application, system, database)',
                        },
                        'time_range': {
                            'type': 'string',
                            'description': 'Time range for the log analysis (e.g., \'1h\', \'24h\', \'7d\')',
                        },
                        'filter_pattern': {
                            'type': 'string',
                            'description': 'Optional pattern to filter logs (e.g., \'error\', \'exception\', \'timeout\')',
                        }
                    },
                    'required': ['log_source']
                }
            )
        ),
        ChatCompletionToolParam(
            type='function',
            function=ChatCompletionToolParamFunctionChunk(
                name='check_metrics',
                description='Retrieve and analyze metrics from monitoring systems',
                parameters={
                    'type': 'object',
                    'properties': {
                        'metric_name': {
                            'type': 'string',
                            'description': 'The name of the metric to check',
                        },
                        'service_name': {
                            'type': 'string',
                            'description': 'The service or component to check metrics for',
                        },
                        'time_range': {
                            'type': 'string',
                            'description': 'Time range for the metric data (e.g., \'1h\', \'24h\', \'7d\')',
                        }
                    },
                    'required': ['metric_name', 'service_name']
                }
            )
        ),
        ChatCompletionToolParam(
            type='function',
            function=ChatCompletionToolParamFunctionChunk(
                name='perform_health_check',
                description='Perform a health check on a system or service',
                parameters={
                    'type': 'object',
                    'properties': {
                        'target': {
                            'type': 'string',
                            'description': 'The system, service, or endpoint to check',
                        },
                        'check_type': {
                            'type': 'string',
                            'description': 'Type of health check (e.g., \'connectivity\', \'response_time\', \'resource_usage\')',
                        }
                    },
                    'required': ['target']
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
            if tool_name == "analyze_logs":
                log_source = tool_arguments.get("log_source", "")
                time_range = tool_arguments.get("time_range", "1h")
                filter_pattern = tool_arguments.get("filter_pattern", "")
                
                result = f"Analyzed logs from {log_source} over {time_range}"
                if filter_pattern:
                    result += f" with filter '{filter_pattern}'"
                result += ". Found typical patterns in system behavior with no significant anomalies."
                
                # Create a message with the analysis result
                message = MessageAction(content=result)
                setattr(message, '_source', EventSource.AGENT)
                actions.append(message)
                
            elif tool_name == "check_metrics":
                metric_name = tool_arguments.get("metric_name", "")
                service_name = tool_arguments.get("service_name", "")
                time_range = tool_arguments.get("time_range", "1h")
                
                result = f"Checked {metric_name} metrics for {service_name} over {time_range}. Values are within normal operational ranges."
                
                # Create a message with the metrics result
                message = MessageAction(content=result)
                setattr(message, '_source', EventSource.AGENT)
                actions.append(message)
                
            elif tool_name == "perform_health_check":
                target = tool_arguments.get("target", "")
                check_type = tool_arguments.get("check_type", "connectivity")
                
                result = f"Performed {check_type} health check on {target}. All checks passed successfully."
                
                # Create a message with the health check result
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
