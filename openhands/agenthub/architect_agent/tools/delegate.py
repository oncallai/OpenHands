from litellm import ChatCompletionToolParam, ChatCompletionToolParamFunctionChunk

_DELEGATE_TASK_TO_AGENT_DESCRIPTION = """Delegate a troubleshooting task to the appropriate agent type.

This tool is used to determine which specialized agent would be best suited to handle a particular 
troubleshooting task based on the task's requirements and the agent's capabilities.

You should use this tool when you have identified a task in your troubleshooting plan that needs to
be assigned to a specific agent type for execution.

Make sure to provide a clear task description that the delegated agent can understand and execute.
"""

DelegateTaskToAgentTool = ChatCompletionToolParam(
    type='function',
    function=ChatCompletionToolParamFunctionChunk(
        name='delegate_task_to_agent',
        description=_DELEGATE_TASK_TO_AGENT_DESCRIPTION,
        parameters={
            'type': 'object',
            'properties': {
                'task_id': {
                    'type': 'string', 
                    'description': 'ID of the task to delegate',
                },
                'agent_type': {
                    'type': 'string', 
                    'description': 'Type of agent to delegate the task to',
                },
                'task': {
                    'type': 'string',
                    'description': 'Detailed description of the task to be performed by the delegated agent',
                }
            },
            'required': ['task_id', 'agent_type', 'task'],
        },
    ),
)
