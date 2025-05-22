from litellm import ChatCompletionToolParam, ChatCompletionToolParamFunctionChunk

_ROOT_CAUSE_DESCRIPTION = """Create a root cause report based on completed troubleshooting steps.

This tool is used to analyze the results from all completed troubleshooting steps and generate:
1. A comprehensive root cause analysis
2. Identification of the underlying issue(s)
3. Clear explanation of the incident chain of events
4. Supporting evidence from the troubleshooting steps

You should use this tool when all troubleshooting steps have been completed and you need to provide
a final analysis of what caused the incident.
"""

RootCauseReportTool = ChatCompletionToolParam(
    type='function',
    function=ChatCompletionToolParamFunctionChunk(
        name='create_root_cause_report',
        description=_ROOT_CAUSE_DESCRIPTION,
        parameters={
            'type': 'object',
            'properties': {}
        },
    ),
)
