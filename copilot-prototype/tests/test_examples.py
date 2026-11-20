"""The committed examples must be exactly what the current code generates (offline, mock provider, fixed clock)."""

from pathlib import Path

import pytest

from copilot.audit import verify
from copilot.cli import main

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
NAMES = ("greenfield", "ambiguous", "brownfield")


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("name", NAMES)
def test_example_output_is_reproducible_and_its_audit_chain_verifies(name: str, tmp_path: Path) -> None:
    folder = EXAMPLES / name
    args = [
        "run",
        "--brief",
        str(folder / "brief.md"),
        "--decisions",
        str(folder / "decisions.json"),
        "--deterministic",
        "--session-id",
        name,
        "--out",
        str(tmp_path),
    ]
    if (folder / "legacy").is_dir():
        args += ["--context", str(folder / "legacy")]
    assert main(args) == 0
    committed = folder / "output" / name
    assert _files(tmp_path / name) == _files(committed)
    assert verify(committed / "audit.jsonl")[0]


def test_example_outputs_contain_none_of_the_fake_secrets_from_the_legacy_code() -> None:
    legacy = (EXAMPLES / "brownfield" / "legacy" / "app.py").read_text(encoding="utf-8")
    secrets = [line.split('"')[1] for line in legacy.splitlines() if line.startswith(("ADMIN_", "AWS_"))]
    assert len(secrets) == 3
    for path in (EXAMPLES).rglob("output/**/*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not any(secret in text for secret in secrets), path
