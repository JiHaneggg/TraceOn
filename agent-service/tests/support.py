"""Shared helpers for agent-service tests (synthetic fixtures only, no DB or network)."""
from copy import deepcopy
import json
from pathlib import Path
import sys

SERVICE = Path(__file__).resolve().parents[1]
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))

FIXTURES = SERVICE.parent / "rag-service" / "examples" / "agent-workflow"
ALERTS = {"cycle": "request.json", "fan_in": "request-judge-holdout.json"}


def load_alert(name="cycle"):
    return deepcopy(json.loads((FIXTURES / ALERTS[name]).read_text(encoding="utf-8")))


try:
    import langgraph  # noqa: F401
    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False
