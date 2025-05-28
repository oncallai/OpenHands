# ArchitectAgent

## Overview

The ArchitectAgent is a specialized agent within the OpenHands framework designed to analyze incidents, coordinate troubleshooting efforts, and determine root causes. Unlike other agents that directly execute actions, the ArchitectAgent acts as a coordinator, delegating specific tasks to other agents and synthesizing their results to provide comprehensive incident analysis.

**Version:** 1.0

## Purpose

The primary purpose of the ArchitectAgent is to provide a higher-level coordination layer for complex troubleshooting scenarios. When faced with an incident or issue, the ArchitectAgent:

1. Analyzes the incident to understand its nature and potential causes
2. Creates a structured troubleshooting plan with specific steps
3. Delegates each step to appropriate specialized agents
4. Collects and analyzes the results from these delegated tasks
5. Determines the root cause of the incident
6. (Future) Creates and delegates resolution steps

This approach allows for more efficient and thorough troubleshooting by:
- Breaking down complex problems into manageable steps
- Leveraging specialized agents for specific tasks
- Providing a centralized analysis of distributed findings
- Maintaining a clear overview of the troubleshooting process

## Architecture

The ArchitectAgent is built on the standard OpenHands Agent interface and integrates with the existing agent controller without requiring modifications to the core controller logic.

### Key Components

#### Classes

- **ArchitectAgent**: The main agent class that extends the base `Agent` class
- **TroubleshootingStep**: A dataclass representing a single step in the troubleshooting plan

#### Modules

- **architect_agent.py**: Contains the main ArchitectAgent class implementation
- **function_calling.py**: Handles function calling for the ArchitectAgent

#### Prompt Templates

- **system_prompt.j2**: Defines the agent's role and responsibilities
- **user_prompt.j2**: Template for user requests
- **incident_analysis.j2**: Template for analyzing incidents
- **troubleshooting_plan.j2**: Template for creating troubleshooting plans
- **root_cause_analysis.j2**: Template for determining root causes

### Workflow

1. **Incident Analysis**:
   - Extracts incident description from user messages
   - Uses LLM to analyze the incident
   - Creates a structured troubleshooting plan

2. **Task Delegation**:
   - Identifies the next undelegated step
   - Determines the appropriate agent for the step
   - Creates an `AgentDelegateAction` to delegate the task
   - Tracks delegation status in internal state

3. **Result Collection**:
   - Processes `AgentDelegateObservation` events from the event stream
   - Updates internal state with completed task results
   - Tracks overall progress of the troubleshooting plan

4. **Root Cause Analysis**:
   - Collects results from all completed tasks
   - Uses LLM to analyze the results and determine root cause
   - Generates a comprehensive root cause report

## Implementation Details

### Integration with OpenHands

The ArchitectAgent is integrated with the OpenHands framework through the following mechanisms:

1. **Module Registration**: The agent module is imported in the main `openhands/agenthub/__init__.py` file:
   ```python
   from openhands.agenthub import (
       architect_agent,
       browsing_agent,
       codeact_agent,
       # other agents...
   )
   
   __all__ = [
       'Agent',
       'architect_agent',
       'codeact_agent',
       # other agents...
   ]
   ```

2. **Agent Registration**: The agent is registered in its own `__init__.py` file:
   ```python
   from openhands.agenthub.architect_agent.architect_agent import ArchitectAgent
   from openhands.controller.agent import Agent
   
   Agent.register('ArchitectAgent', ArchitectAgent)
   ```

3. **Default Agent Configuration**: The agent can be set as the default in two ways:
   - In `config.toml`:
     ```toml
     [core]
     default_agent = "ArchitectAgent"
     ```
   - In `openhands/core/config/config_utils.py`:
     ```python
     OH_DEFAULT_AGENT = 'ArchitectAgent'
     ```

These integrations ensure that the ArchitectAgent is properly loaded and available for use in the OpenHands framework.

### State Management

The ArchitectAgent maintains several key state variables:

- `incident_analysis`: Dictionary containing the analysis of the current incident
- `troubleshooting_plan`: List of `TroubleshootingStep` objects representing the plan
- `delegated_tasks`: Dictionary tracking delegated tasks by their ID
- `root_cause`: The determined root cause of the incident

### Key Methods

#### Core Workflow Methods

- `step(state)`: Main method called by the controller to get the next action
- `_analyze_incident(events)`: Analyzes the incident and creates a troubleshooting plan
- `_delegate_step(step)`: Delegates a troubleshooting step to an appropriate agent
- `_create_root_cause_report()`: Creates a root cause report based on collected results

#### Helper Methods

- `_process_delegate_observations(events)`: Processes observations from delegated agents
- `_extract_incident_description(events)`: Extracts incident description from user messages
- `_parse_incident_analysis(content)`: Parses LLM response to extract incident analysis
- `_parse_troubleshooting_plan(content)`: Parses LLM response to extract troubleshooting steps
- `_parse_root_cause(content)`: Parses LLM response to extract root cause
- `_get_next_undelegated_step()`: Gets the next step that hasn't been delegated
- `_all_steps_completed()`: Checks if all steps in the plan have been completed
- `_format_troubleshooting_plan()`: Formats the troubleshooting plan for display

### Integration with Agent Controller

The ArchitectAgent integrates with the existing agent controller through:

1. **Standard Agent Interface**: Implements the required methods of the base `Agent` class
2. **Event Processing**: Processes events from the event stream to track delegated tasks
3. **Action Generation**: Returns appropriate actions for the controller to execute
4. **Delegation Mechanism**: Uses `AgentDelegateAction` for task delegation

## Usage Guide

### Setting as Default Agent

You can set the ArchitectAgent as the default agent in your OpenHands application by updating the `config.toml` file:

```toml
[core]
workspace_base="./workspace"
runtime = "docker"
default_agent = "ArchitectAgent"
```

This will make the ArchitectAgent the default agent used when starting a new task.

### Basic Usage

To use the ArchitectAgent in your OpenHands application:

1. **Import the Agent**:
   ```python
   from openhands.agenthub.architect_agent import ArchitectAgent
   ```

2. **Create an Instance**:
   ```python
   from openhands.core.config import AgentConfig
   from openhands.llm.llm import LLM
   
   # Create LLM instance
   llm = LLM(...)
   
   # Create agent config
   config = AgentConfig(...)
   
   # Create ArchitectAgent instance
   architect_agent = ArchitectAgent(llm, config)
   ```

3. **Use with Agent Controller**:
   ```python
   from openhands.controller.agent_controller import AgentController
   from openhands.events import EventStream
   
   # Create event stream
   event_stream = EventStream()
   
   # Create agent controller with ArchitectAgent
   controller = AgentController(
       agent=architect_agent,
       event_stream=event_stream,
       max_iterations=50
   )
   
   # Start the controller
   controller.start()
   ```

### Example: Incident Analysis

Here's an example of how to use the ArchitectAgent for incident analysis:

```python
from openhands.events.action import MessageAction

# Create a message action with the incident description
incident_message = MessageAction(
    content="Our web application is experiencing intermittent 500 errors. Users report that the issue started around 2 hours ago. The error rate is approximately 15% of requests. We haven't deployed any changes in the last 24 hours.",
    source="user"
)

# Add the message to the event stream
event_stream.add_event(incident_message)

# The ArchitectAgent will:
# 1. Analyze the incident
# 2. Create a troubleshooting plan
# 3. Delegate tasks to other agents
# 4. Collect and analyze results
# 5. Determine the root cause
```

### Customizing the ArchitectAgent

You can customize the ArchitectAgent's behavior by:

1. **Modifying Prompt Templates**:
   - Edit the templates in the `prompts` directory to change how the agent analyzes incidents, creates plans, etc.

2. **Extending the Agent Class**:
   ```python
   class CustomArchitectAgent(ArchitectAgent):
       def _determine_best_agent_for_step(self, step):
           # Custom logic to determine the best agent for a step
           if "database" in step.description.lower():
               return "DatabaseAgent"
           elif "network" in step.description.lower():
               return "NetworkAgent"
           return "CodeActAgent"
   ```

3. **Adding New Capabilities**:
   ```python
   class EnhancedArchitectAgent(ArchitectAgent):
       def _create_resolution_plan(self):
           # Logic to create a resolution plan after root cause analysis
           pass
           
       def step(self, state):
           # Override step method to include resolution planning
           if self.root_cause and not hasattr(self, 'resolution_plan'):
               return self._create_resolution_plan()
           return super().step(state)
   ```

## Advanced Topics

### Parallel Task Execution

Currently, the ArchitectAgent delegates tasks sequentially. To implement parallel execution:

1. Modify the `step` method to delegate multiple independent tasks
2. Track dependencies between tasks to ensure proper sequencing
3. Update the `_all_steps_completed` method to check for completion of dependent tasks

### Agent Selection Logic

The current implementation uses a simple approach to agent selection. To implement more sophisticated selection:

1. Define agent capabilities and requirements
2. Analyze task requirements to match with appropriate agents
3. Consider agent availability and load balancing

### Structured Output Parsing

To improve parsing of LLM responses:

1. Use structured output formats (JSON, YAML) in prompts
2. Implement robust parsing logic for structured outputs
3. Handle parsing errors gracefully

## Troubleshooting

### Common Issues

1. **Agent Not Receiving Delegated Task Results**:
   - Check that the delegate agent is properly returning observations
   - Verify that the event stream is correctly propagating events
   - Ensure that task IDs are correctly included in inputs and outputs

2. **Poor Incident Analysis**:
   - Review and improve the prompt templates
   - Ensure the LLM has sufficient context about the incident
   - Consider using a more capable LLM for complex analysis

3. **Incorrect Task Delegation**:
   - Enhance the agent selection logic
   - Improve the parsing of troubleshooting steps
   - Provide more detailed step descriptions

### Debugging

1. **Enable Debug Logging**:
   ```python
   import logging
   from openhands.core.logger import openhands_logger
   
   openhands_logger.setLevel(logging.DEBUG)
   ```

2. **Inspect Event Stream**:
   ```python
   # Print all events in the event stream
   for event in event_stream.get_events():
       print(f"Event: {event}")
   ```

3. **Check Agent State**:
   ```python
   # Print the ArchitectAgent's internal state
   print(f"Incident Analysis: {architect_agent.incident_analysis}")
   print(f"Troubleshooting Plan: {architect_agent.troubleshooting_plan}")
   print(f"Delegated Tasks: {architect_agent.delegated_tasks}")
   ```

## Future Enhancements

1. **Resolution Planning**: Extend the agent to create and delegate resolution steps after root cause analysis

2. **Agent Selection Logic**: Implement more sophisticated logic for selecting the best agent for each step based on agent capabilities and step requirements

3. **Parallel Execution**: Allow the agent to delegate multiple steps in parallel when they don't depend on each other

4. **Step Dependencies**: Add support for defining dependencies between troubleshooting steps

5. **Progress Monitoring**: Implement better progress tracking and reporting for delegated tasks

6. **Failure Handling**: Add logic to handle failures in delegated tasks, including retry logic or alternative approaches

7. **Interactive Troubleshooting**: Allow the agent to ask clarifying questions to the user during the troubleshooting process

## Contributing

When contributing to the ArchitectAgent:

1. Follow the existing code style and patterns
2. Add comprehensive docstrings to new methods
3. Update this README with any significant changes
4. Add appropriate logging for debugging
5. Test thoroughly with different incident scenarios

## References

- [OpenHands Agent Framework Documentation](https://github.com/All-Hands-AI/OpenHands)
- [CodeActAgent Implementation](https://github.com/All-Hands-AI/OpenHands/tree/main/openhands/agenthub/codeact_agent)
- [Agent Controller Documentation](https://github.com/All-Hands-AI/OpenHands/blob/main/openhands/controller/agent_controller.py)
