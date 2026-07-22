def get_alert(d):
    with open(f"{d}/alert.txt", encoding="utf-8") as f:
        return f.read()

def get_metrics(d):
    with open(f"{d}/metrics.txt", encoding="utf-8") as f:
        return f.read()

def get_logs(d):
    with open(f"{d}/logs.txt", encoding="utf-8") as f:
        return f.read()

def get_deploy_history(d):
    with open(f"{d}/deploy_history.json", encoding="utf-8") as f:
        return f.read()
    
def get_git_log(d):
    with open(f"{d}/git_log.txt", encoding="utf-8") as f:
        return f.read()

TOOLS = [
    {"name": "get_alert",
    "description": "Get the production alert that just fired.",
    "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_metrics",
    "description": "Get recent metrics (memory, restarts, latency) for the affected service.",
    "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_logs",
    "description": "Get recent pod logs for the affected service — shows errors, restarts, and failure signatures like OOMKilled.",
    "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_deploy_history",
    "description": "Get the recent deployment history for the affected service — versions, timestamps, and who deployed.",
    "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_git_log",
    "description": "Get the recent git log for the affected service — shows commits, authors, and timestamps.",
    "input_schema": {"type": "object", "properties": {}}},
]