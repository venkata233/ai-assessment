"""Session persistence: one directory per session, atomic writes, validated names."""

import json
import os
import re
import tempfile
from pathlib import Path

from copilot.models import Session, from_dict, to_dict

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class StoreError(Exception):
    pass


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def session_dir(self, session_id: str) -> Path:
        if not SESSION_ID_RE.fullmatch(session_id):
            raise StoreError("invalid session id")
        return self.root / session_id

    def exists(self, session_id: str) -> bool:
        return (self.session_dir(session_id) / "session.json").is_file()

    def save(self, session: Session) -> None:
        directory = self.session_dir(session.id)
        directory.mkdir(parents=True, exist_ok=True)
        self._atomic_write(directory / "session.json", json.dumps(to_dict(session), indent=2, sort_keys=True))

    def load(self, session_id: str) -> Session:
        path = self.session_dir(session_id) / "session.json"
        if not path.is_file():
            raise StoreError("unknown session")
        return from_dict(json.loads(path.read_text(encoding="utf-8")))

    def write_artifact(self, session_id: str, name: str, text: str, subdir: str | None = None) -> Path:
        if not ARTIFACT_NAME_RE.fullmatch(name) or (
            subdir is not None and not ARTIFACT_NAME_RE.fullmatch(subdir)
        ):
            raise StoreError("invalid artifact name")
        directory = self.session_dir(session_id) / subdir if subdir else self.session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        self._atomic_write(path, text)
        return path

    def read_artifact(self, session_id: str, name: str) -> str:
        if not ARTIFACT_NAME_RE.fullmatch(name):
            raise StoreError("invalid artifact name")
        path = self.session_dir(session_id) / name
        if not path.is_file():
            raise StoreError("unknown artifact")
        return path.read_text(encoding="utf-8")

    def list_sessions(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.name
            for p in self.root.iterdir()
            if (p / "session.json").is_file() and SESSION_ID_RE.fullmatch(p.name)
        )

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            os.replace(temp, path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
