#!/usr/bin/env python
"""
This script fixes the ArchitectAgent class to properly format messages for the LLM.
"""

import re

# Path to the file
architect_agent_path = "/Users/jwalit/Documents/curser/OpenHands/openhands/agenthub/architect_agent/architect_agent.py"

# Read the current content
with open(architect_agent_path, 'r') as file:
    content = file.read()

# Fix the _analyze_incident method
analyze_pattern = r'params = \{\s+"messages": \[\s+Message\(role="system", content=\[TextContent\(text=system_message\)\]\),\s+Message\(role="user", content=\[TextContent\(text=user_message\)\]\)\s+\]\s+\}'
analyze_replacement = '''messages = [
            Message(role="system", content=[TextContent(text=system_message)]),
            Message(role="user", content=[TextContent(text=user_message)])
        ]
        
        params = {
            "messages": self.llm.format_messages_for_llm(messages)
        }'''

content = re.sub(analyze_pattern, analyze_replacement, content)

# Fix the _create_root_cause_report method
root_cause_pattern = r'params = \{\s+"messages": \[\s+Message\(role="system", content=\[TextContent\(text=system_message\)\]\),\s+Message\(role="user", content=\[TextContent\(text=user_message\)\]\)\s+\]\s+\}'
root_cause_replacement = '''messages = [
            Message(role="system", content=[TextContent(text=system_message)]),
            Message(role="user", content=[TextContent(text=user_message)])
        ]
        
        params = {
            "messages": self.llm.format_messages_for_llm(messages)
        }'''

content = re.sub(root_cause_pattern, root_cause_replacement, content)

# Write the fixed content back to the file
with open(architect_agent_path, 'w') as file:
    file.write(content)

print("ArchitectAgent has been fixed to properly format messages for the LLM.")
