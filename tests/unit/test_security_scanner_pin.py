"""The baseline scan installs the version used with its recorded baseline."""

from pathlib import Path


def test_baseline_scan_installs_the_exact_version():
    """Guard: replacing the exact scanner version with the former range fails this test."""
    workflow = (Path(__file__).resolve().parents[2] / '.github/workflows/security-scans.yml').read_text()
    assert '"bandit==1.9.4"' in workflow
    assert '"bandit>=1.8,<2.0"' not in workflow
