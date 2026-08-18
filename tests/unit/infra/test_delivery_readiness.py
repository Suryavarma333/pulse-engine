from __future__ import annotations

from pathlib import Path

from scripts.secret_scan import scan

ROOT = Path(__file__).parents[3]


def test_ci_covers_every_required_stack_without_live_aws() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    for expected in (
        "python -m compileall",
        "ruff check",
        "--cov-fail-under=85",
        "postgres:16-alpine",
        "alembic -c db/alembic.ini downgrade base",
        "pnpm --dir dashboard install --frozen-lockfile",
        "docker compose config --quiet",
        "scripts/smoke_stack.sh",
        "terraform -chdir=infra init -backend=false",
        "scripts/secret_scan.py",
    ):
        assert expected in workflow
    assert "terraform apply" not in workflow
    assert "AWS_ACCESS_KEY_ID" not in workflow
    assert "id-token: write" not in workflow


def test_secret_scanner_rejects_credentials_and_generated_artifacts(tmp_path: Path) -> None:
    safe = tmp_path / "safe.txt"
    safe.write_text("PULSE_EXECUTION_MODE=dry_run\n")
    leaked = tmp_path / "leaked.txt"
    leaked.write_text("AK" + "IA" + "ABCDEFGHIJKLMNOP")
    state = tmp_path / "terraform.tfstate"
    state.write_text("{}")

    findings = scan((safe, leaked, state), root=tmp_path)

    assert not any("safe.txt" in finding for finding in findings)
    assert any("AWS access key" in finding for finding in findings)
    assert any("forbidden tracked artifact" in finding for finding in findings)


def test_complete_docs_exist_and_no_slice_language_remains() -> None:
    required = (
        "README.md",
        "docs/architecture.md",
        "docs/state-machine.md",
        "docs/demo-runbook.md",
        "docs/results-template.md",
    )
    for relative in required:
        content = (ROOT / relative).read_text()
        assert content.strip(), relative
        assert "first implementation slice" not in content.lower()
    assert "```mermaid" in (ROOT / "docs/architecture.md").read_text()
    assert "```mermaid" in (ROOT / "docs/state-machine.md").read_text()


def test_there_is_one_response_pipeline_and_one_capacity_mutation_path() -> None:
    sources = "\n".join(
        path.read_text()
        for path in (ROOT / "agent/app").rglob("*.py")
    )
    assert sources.count("class ResponsePipeline:") == 1
    assert sources.count("self._client.set_desired_capacity") == 1
