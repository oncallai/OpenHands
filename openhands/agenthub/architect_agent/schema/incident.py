from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class TroubleshootingStep:
    """
    Represents a single troubleshooting step in an incident investigation.
    """
    id: str
    description: str
    agent_name: str
    delegated: bool = False
    completed: bool = False
    result: Optional[str] = None
    delegated_at: Optional[float] = None
    completed_at: Optional[float] = None

    def mark_delegated(self) -> None:
        """Mark this step as delegated and record the timestamp."""
        self.delegated = True
        self.delegated_at = time.time()

    def mark_completed(self, result: str) -> None:
        """Mark this step as completed with the given result."""
        self.completed = True
        self.completed_at = time.time()
        self.result = result
    
    def get_status(self) -> str:
        """Get a human-readable status for this step."""
        if self.completed:
            return "COMPLETED"
        elif self.delegated:
            return "DELEGATED"
        return "PENDING"
        
    def to_dict(self) -> Dict[str, Any]:
        """Convert this step to a dictionary representation."""
        return {
            "id": self.id,
            "description": self.description,
            "agent_name": self.agent_name,
            "delegated": self.delegated,
            "completed": self.completed,
            "result": self.result,
            "delegated_at": self.delegated_at,
            "completed_at": self.completed_at,
            "status": self.get_status()
        }


@dataclass
class Incident:
    """
    Represents an incident being investigated by the Architect Agent.
    
    This class encapsulates all the data related to an incident, including:
    - The incident analysis (summary, description, etc.)
    - The troubleshooting plan with its steps
    - Tracking of delegated tasks
    - The root cause analysis
    """
    # The incident analysis contains information about the incident
    analysis: Dict[str, Any] = field(default_factory=dict)
    
    # The troubleshooting plan is a list of steps to investigate the incident
    troubleshooting_plan: List[TroubleshootingStep] = field(default_factory=list)
    
    # The delegated tasks tracks steps that have been delegated to other agents
    delegated_tasks: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    # The root cause once determined
    root_cause: Optional[str] = None
    
    def set_analysis(self, analysis: Dict[str, Any]) -> None:
        """Set the incident analysis data."""
        self.analysis = analysis
    
    def get_summary(self) -> str:
        """Get the incident summary."""
        return self.analysis.get("summary", "No summary available")
    
    def add_troubleshooting_step(self, step: TroubleshootingStep) -> None:
        """Add a troubleshooting step to the plan."""
        self.troubleshooting_plan.append(step)
    
    def set_troubleshooting_plan(self, steps: List[TroubleshootingStep]) -> None:
        """Set the entire troubleshooting plan."""
        self.troubleshooting_plan = steps
    
    def get_next_pending_step(self) -> Optional[TroubleshootingStep]:
        """Get the next pending (not delegated or completed) step."""
        for step in self.troubleshooting_plan:
            if not step.delegated and not step.completed:
                return step
        return None
    
    def delegate_step(self, step_id: str, agent_name: str) -> None:
        """Mark a step as delegated and record delegation information."""
        # Find the step by ID
        step = self.get_step_by_id(step_id)
        if step:
            step.mark_delegated()
            step.agent_name = agent_name
            
            # Record delegation info
            self.delegated_tasks[step_id] = {
                'step': step.to_dict(),
                'delegated_at': time.time(),
                'completed': False,
                'result': None
            }
    
    def complete_step(self, step_id: str, result: str) -> None:
        """Mark a step as completed with the given result."""
        # Find the step by ID
        step = self.get_step_by_id(step_id)
        if step:
            step.mark_completed(result)
            
            # Update delegation tracking if applicable
            if step_id in self.delegated_tasks:
                self.delegated_tasks[step_id]['completed'] = True
                self.delegated_tasks[step_id]['result'] = result
    
    def get_step_by_id(self, step_id: str) -> Optional[TroubleshootingStep]:
        """Get a troubleshooting step by ID."""
        for step in self.troubleshooting_plan:
            if step.id == step_id:
                return step
        return None
    
    def set_root_cause(self, root_cause: str) -> None:
        """Set the determined root cause of the incident."""
        self.root_cause = root_cause
    
    def all_steps_completed(self) -> bool:
        """Check if all steps in the troubleshooting plan are completed."""
        return all(step.completed for step in self.troubleshooting_plan)
    
    def get_completed_steps(self) -> List[TroubleshootingStep]:
        """Get all completed steps."""
        return [step for step in self.troubleshooting_plan if step.completed]
    
    def format_troubleshooting_plan(self) -> str:
        """Format the troubleshooting plan for display."""
        result = ""
        for step in self.troubleshooting_plan:
            status = f" [{step.get_status()}]"
            agent_info = f" (Agent: {step.agent_name})"
            result += f"{step.id}. {step.description}{agent_info}{status}\n"
        return result
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert this incident to a dictionary representation."""
        return {
            "analysis": self.analysis,
            "troubleshooting_plan": [step.to_dict() for step in self.troubleshooting_plan],
            "delegated_tasks": self.delegated_tasks,
            "root_cause": self.root_cause
        }
