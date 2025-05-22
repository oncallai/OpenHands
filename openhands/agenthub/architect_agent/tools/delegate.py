from litellm import ChatCompletionToolParam, ChatCompletionToolParamFunctionChunk

_DELEGATE_STEP_DESCRIPTION = """Delegate a troubleshooting step to the appropriate agent type.

This tool is used to determine which specialized agent would be best suited to handle a particular 
troubleshooting step based on the step's requirements and the agent's capabilities.

You should use this tool when you have identified a step in your troubleshooting plan that needs to
be assigned to a specific agent type for execution.
"""

DelegateStepTool = ChatCompletionToolParam(
    type='function',
    function=ChatCompletionToolParamFunctionChunk(
        name='delegate_step',
        description=_DELEGATE_STEP_DESCRIPTION,
        parameters={
            'type': 'object',
            'properties': {
                'step_id': {
                    'type': 'string', 
                    'description': 'ID of the step to delegate',
                },
                'agent_type': {
                    'type': 'string', 
                    'description': 'Type of agent to delegate the step to',
                }
            },
            'required': ['step_id', 'agent_type'],
        },
    ),
)
