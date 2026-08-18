from pathlib import Path


def test_wrappers_are_headless_pairable_and_generated_output_is_ignored() -> None:
    runner = Path("load_tests/scripts/run_scenario.py").read_text()
    pair = Path("load_tests/scripts/run_pair.py").read_text()
    scheduled = Path("load_tests/scripts/run_scheduled.sh").read_text()
    ignored = Path(".gitignore").read_text().splitlines()

    assert '"--headless"' in runner
    assert 'for baseline in ("reactive_only", "pulse")' in pair
    assert "scripts/reset_demo.py" in pair
    assert "scripts/seed_scheduled_events.py" in scheduled
    assert "load_tests/results/" in ignored
    assert "dashboard/.next/" in ignored


def test_browser_code_is_not_present_before_dashboard_contract_scan() -> None:
    dashboard = Path("dashboard")
    if not dashboard.exists():
        return
    source_roots = [dashboard / name for name in ("app", "components", "lib", "tests")]
    sources = "\n".join(
        path.read_text()
        for root in source_roots
        for path in root.rglob("*")
        if path.suffix in {".ts", ".tsx", ".js", ".jsx"}
    )
    assert "/internal/" not in sources
    assert "X-Pulse-Control-Token" not in sources
