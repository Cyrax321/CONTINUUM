import re

with open("src/continuum/mcp/server.py", "r") as f:
    content = f.read()

old_code = """        gate_config = load_gate_config(_Path(DEFAULT_GATE_CONFIG_PATH))
        similarity_config = None
        if gate_config:
            spec = gate_config.get(action_type)
            if spec and spec.get("similarity"):
                from continuum.replay_similarity import similarity_backend"""

new_code = """        gate_config = load_gate_config(_Path(DEFAULT_GATE_CONFIG_PATH))
        similarity_config = None
        if gate_config:
            spec = None
            for tool_name, s in gate_config.items():
                if s.get("action_type", tool_name) == action_type:
                    spec = s
                    break
            if spec and spec.get("similarity"):
                from continuum.replay_similarity import similarity_backend"""

content = content.replace(old_code, new_code)

with open("src/continuum/mcp/server.py", "w") as f:
    f.write(content)
