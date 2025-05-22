from litellm import ChatCompletionToolParam, ChatCompletionToolParamFunctionChunk

_INCIDENT_ANALYSIS_DESCRIPTION = """Analyze an incident and create a troubleshooting plan.

This tool is used to analyze an incident description and generate:
1. A comprehensive analysis of the incident
2. A structured troubleshooting plan with concrete steps
3. Agent type recommendations for each step

You should use this tool when you first need to understand an incident and create a plan to resolve it.
"""

AnalyzeIncidentTool = ChatCompletionToolParam(
    type='function',
    function=ChatCompletionToolParamFunctionChunk(
        name='analyze_incident',
        description=_INCIDENT_ANALYSIS_DESCRIPTION,
        parameters={
            'type': 'object',
            'properties': {
                'incident_description': {
                    'type': 'string',
                    'description': 'Description of the incident to analyze',
                }
            },
            'required': ['incident_description']
        },
    ),
)
