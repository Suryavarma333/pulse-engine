from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_TRACKED_NAMES = {
    ".env",
    "terraform.tfstate",
    "terraform.tfstate.backup",
}
FORBIDDEN_TRACKED_SUFFIXES = {".tfplan", ".pem", ".p12", ".pfx"}
GENERATED_PARTS = {
    ("dashboard", ".next"),
    ("dashboard", "node_modules"),
    ("load_tests", "results"),
}
SECRET_PATTERNS = {
    "AWS access key": re.compile(r"(?:AK" + r"IA|AS" + r"IA)[0-9A-Z]{16}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE" + r" KEY-----"),
    "GitHub token": re.compile(r"(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}"),
    "Slack token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
}


def tracked_files(root: Path = ROOT) -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return tuple(root / item.decode() for item in result.stdout.split(b"\0") if item)


def scan(paths: tuple[Path, ...], root: Path = ROOT) -> list[str]:
    findings: list[str] = []
    for path in paths:
        relative = path.relative_to(root)
        if (
            relative.name in FORBIDDEN_TRACKED_NAMES
            or relative.suffix in FORBIDDEN_TRACKED_SUFFIXES
        ):
            findings.append(f"forbidden tracked artifact: {relative}")
        parts = relative.parts
        if any(parts[: len(prefix)] == prefix for prefix in GENERATED_PARTS):
            findings.append(f"generated artifact is tracked: {relative}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                findings.append(f"{label} signature: {relative}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan tracked Pulse files for secret signatures")
    parser.parse_args()
    findings = scan(tracked_files())
    if findings:
        for finding in findings:
            print(f"ERROR: {finding}")
        return 1
    print("Tracked-file secret and generated-artifact scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
