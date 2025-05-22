from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field, ValidationError

import os
import json
from openhands.core.config.condenser_config import CondenserConfig, NoOpCondenserConfig
from openhands.core.config.extended_config import ExtendedConfig
from openhands.core.logger import openhands_logger as logger


class AgentConfig(BaseModel):
    llm_config: str | None = Field(default=None)
    """The name of the llm config to use. If specified, this will override global llm config."""
    classpath: str | None = Field(default=None)
    """The classpath of the agent to use. To be used for custom agents that are not defined in the openhands.agenthub package."""
    enable_browsing: bool = Field(default=True)
    """Whether to enable browsing tool"""
    enable_llm_editor: bool = Field(default=False)
    """Whether to enable LLM editor tool"""
    enable_editor: bool = Field(default=True)
    """Whether to enable the standard editor tool (str_replace_editor), only has an effect if enable_llm_editor is False."""
    enable_jupyter: bool = Field(default=True)
    """Whether to enable Jupyter tool"""
    enable_cmd: bool = Field(default=True)
    """Whether to enable bash tool"""
    enable_think: bool = Field(default=True)
    """Whether to enable think tool"""
    enable_finish: bool = Field(default=True)
    """Whether to enable finish tool"""
    enable_prompt_extensions: bool = Field(default=True)
    """Whether to enable prompt extensions"""
    enable_mcp: bool = Field(default=True)
    """Whether to enable MCP tools"""
    disabled_microagents: list[str] = Field(default_factory=list)
    """A list of microagents to disable (by name, without .py extension, e.g. ["github", "lint"]). Default is None."""
    enable_history_truncation: bool = Field(default=True)
    """Whether history should be truncated to continue the session when hitting LLM context length limit."""
    enable_som_visual_browsing: bool = Field(default=True)
    """Whether to enable SoM (Set of Marks) visual browsing."""
    condenser: CondenserConfig = Field(
        default_factory=lambda: NoOpCondenserConfig(type='noop')
    )
    extended: ExtendedConfig = Field(default_factory=lambda: ExtendedConfig({}))
    """Extended configuration for the agent."""

    model_config = {'extra': 'forbid'}
    
    # TODO: move this to load along with configs from toml
    @staticmethod
    def load_agent_list(agent_list_path: Optional[str] = None) -> dict:
        """
        Load agent list from a JSON file.
        
        Args:
            agent_list_path: Optional path to the agent list JSON file. If not provided,
                          uses the default path in openhands/core/config/agent_list.json
            
        Returns:
            Dictionary containing agent list information
        """
        if agent_list_path is None:
            # Use the default path in core/config
            agent_list_path = os.path.join(os.path.dirname(__file__), 'agent_list.json')
        try:
            with open(agent_list_path, 'r') as f:
                agent_list = json.load(f)
                if not agent_list.get('local'):
                    logger.warning("Agent list is empty or has no local agents")
                    agent_list = {
                        "local": {
                            "CodeActAgent": {
                                "agent_name": "CodeActAgent",
                                "description": "Expert in analyzing code, fixing bugs, and diagnosing software issues."
                            }
                        }
                    }
                return agent_list
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"Error loading agent list: {e}. Using default agent (CodeActAgent)")
            return {
                "local": {
                    "CodeActAgent": {
                        "agent_name": "CodeActAgent",
                        "description": "Expert in analyzing code, fixing bugs, and diagnosing software issues."
                    }
                }
            }

    @classmethod
    def from_toml_section(cls, data: dict) -> dict[str, AgentConfig]:
        """
        Create a mapping of AgentConfig instances from a toml dictionary representing the [agent] section.

        The default configuration is built from all non-dict keys in data.
        Then, each key with a dict value is treated as a custom agent configuration, and its values override
        the default configuration.

        Example:
        Apply generic agent config with custom agent overrides, e.g.
            [agent]
            enable_prompt_extensions = false
            [agent.BrowsingAgent]
            enable_prompt_extensions = true
        results in prompt_extensions being true for BrowsingAgent but false for others.

        Returns:
            dict[str, AgentConfig]: A mapping where the key "agent" corresponds to the default configuration
            and additional keys represent custom configurations.
        """

        # Initialize the result mapping
        agent_mapping: dict[str, AgentConfig] = {}

        # Extract base config data (non-dict values)
        base_data = {}
        custom_sections: dict[str, dict] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                custom_sections[key] = value
            else:
                base_data[key] = value

        # Try to create the base config
        try:
            base_config = cls.model_validate(base_data)
            agent_mapping['agent'] = base_config
        except ValidationError as e:
            logger.warning(f'Invalid base agent configuration: {e}. Using defaults.')
            # If base config fails, create a default one
            base_config = cls()
            # Still add it to the mapping
            agent_mapping['agent'] = base_config

        # Process each custom section independently
        for name, overrides in custom_sections.items():
            try:
                # Merge base config with overrides
                merged = {**base_config.model_dump(), **overrides}
                custom_config = cls.model_validate(merged)
                agent_mapping[name] = custom_config
            except ValidationError as e:
                logger.warning(
                    f'Invalid agent configuration for [{name}]: {e}. This section will be skipped.'
                )
                # Skip this custom section but continue with others
                continue

        return agent_mapping
