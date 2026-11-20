import os
from pathlib import Path

import pytest

from copilot.context import load_context


def populate(root: Path) -> None:
    (root / "app").mkdir()
    (root / "app" / "views.py").write_text("def create_alias(alias):\n    pass\n", encoding="utf-8")
    (root / "README.md").write_text("# legacy\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (root / "server.key").write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    (root / "credentials.json").write_text("{}", encoding="utf-8")
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    (root / "blob.txt").write_bytes(b"abc\x00def")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("module.exports=1", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config.txt").write_text("[core]", encoding="utf-8")


def test_loads_text_and_skips_secrets_binaries_and_tooling_dirs(tmp_path: Path) -> None:
    populate(tmp_path)
    bundle = load_context(tmp_path)
    assert [f.path for f in bundle.files] == ["README.md", "app/views.py"]
    skipped = dict(bundle.skipped)
    assert skipped[".env"] == "secret-like file name"
    assert skipped["server.key"] == "secret-like file name"
    assert skipped["credentials.json"] == "secret-like file name"
    assert skipped["blob.txt"] == "binary"
    assert skipped["image.png"] == "unsupported type"
    assert not any(
        p.startswith(("node_modules", ".git")) for p, _ in bundle.skipped
    ) and "node_modules/x.js" not in [f.path for f in bundle.files]


def test_order_is_deterministic(tmp_path: Path) -> None:
    for name in ("b.py", "a.py", "c.py"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    assert [f.path for f in load_context(tmp_path).files] == ["a.py", "b.py", "c.py"]


def test_limits_are_enforced(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("x" * 100, encoding="utf-8")
    bundle = load_context(tmp_path, max_files=2, max_file_bytes=40)
    assert len(bundle.files) == 2 and all(f.truncated and len(f.text) == 40 for f in bundle.files)
    assert [r for _, r in bundle.skipped] == ["limit reached"] * 3
    total = load_context(tmp_path, max_total_bytes=150)
    assert len(total.files) == 2  # the byte budget stops further files


def test_a_single_file_can_be_given(tmp_path: Path) -> None:
    target = tmp_path / "one.py"
    target.write_text("print(1)\n", encoding="utf-8")
    assert [f.path for f in load_context(target).files] == ["one.py"]


def test_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_context(tmp_path / "nope")


def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.py").write_text("SECRET = 1\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
    try:
        os.symlink(outside / "leak.py", root / "link.py")
        os.symlink(outside, root / "linkdir", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs privileges on this machine")
    bundle = load_context(root)
    assert [f.path for f in bundle.files] == ["ok.py"]
    assert "leak.py" not in " ".join(f.text for f in bundle.files)
