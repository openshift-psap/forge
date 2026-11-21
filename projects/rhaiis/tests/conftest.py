"""Fixtures for the RHAIIS project tests."""

import sys
from pathlib import Path

# The orchestration entrypoint imports its sibling modules by bare name, the
# way it is imported when the pipeline runs it as a script.
_ORCHESTRATION_DIR = Path(__file__).resolve().parents[1] / "orchestration"
if str(_ORCHESTRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATION_DIR))
