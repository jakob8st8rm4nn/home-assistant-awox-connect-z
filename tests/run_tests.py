"""Run each stub-based suite in its own interpreter."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
for name in ("test_status_confirmation.py", "test_effect_power_finalizer.py", "test_optimization.py", "test_reload_availability.py", "test_availability_transitions.py", "test_reachability_model.py", "test_small_diagnostic_fixes.py", "test_effect_appearance.py", "test_confirmation_only_display.py"):
    result = subprocess.run([sys.executable, str(root / name)], check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
