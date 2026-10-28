"""Run each stub-based suite in its own interpreter."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
suites = (
    "test_status_confirmation.py",
    "test_effect_power_finalizer.py",
    "test_optimization.py",
    "test_reload_availability.py",
    "test_availability_transitions.py",
    "test_reachability_model.py",
    "test_small_diagnostic_fixes.py",
    "test_effect_appearance.py",
    "test_confirmation_only_display.py",
    "test_pending_display.py",
    "test_availability_state.py",
    "test_gatt_write_errors.py",
    "test_config_selection_forms.py",
    "test_device_info.py",
    "test_listener_registration.py",
    "test_cloud_import_preparation.py",
    "test_structure_cleanup.py",
    "test_pending_brightness_cleanup.py",
    "test_discovery_auth_helpers.py",
    "test_paket_a.py",
    "test_vereinfachung.py",
    "test_small_followup.py",
    "test_four_cleanups.py",
    "test_final_cleanup.py",
    "test_tenminute_cleanup.py",
    "test_cloud_data_reuse.py",
    "test_twenty_minute_cleanup.py",
    "test_additional_cleanup.py",
)
for name in suites:
    result = subprocess.run([sys.executable, str(root / name)], check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
