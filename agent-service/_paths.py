"""Make the sibling services importable without copying their code."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for service in ("rule-service", "rag-service", "data-pipeline", "ml-service"):
    path = str(ROOT / service)
    if path not in sys.path:
        # Appended, so agent-service modules keep precedence over sibling names.
        sys.path.append(path)
