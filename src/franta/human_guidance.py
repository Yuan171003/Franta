"""Immutable human suggestions submitted independently of the running scheduler."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterator
import uuid


_INBOX_PATH = Path("private/human-guidance/inbox")
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
_FILENAME = re.compile(r"(HG-(\d{8}T\d{12}Z)-[0-9a-f]{32})\.md\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _text_bytes(text: str) -> bytes:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("human guidance must contain nonempty text")
    if "\x00" in text:
        raise ValueError("human guidance must not contain NUL characters")
    return text.encode("utf-8")


@contextmanager
def _regular_file(directory: int, name: str) -> Iterator[int]:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"human-guidance path must be a regular file: {name}")
        yield descriptor
    finally:
        os.close(descriptor)


def _read_file(directory: int, name: str) -> bytes:
    with _regular_file(directory, name) as descriptor:
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            return stream.read()


@contextmanager
def _inbox(project: str | Path, *, create: bool) -> Iterator[tuple[Path, int | None]]:
    """Traverse only real directories; validate initialization when submitting.

    Descriptor-relative operations prevent an inbox or private-directory symlink
    from redirecting a submission. This deliberately never opens a runtime or a
    SQLite connection, even when the scheduler is currently running.
    """

    root = Path(project).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("human guidance requires a project directory")
    if not create:
        # Inbox polling is optional for old projects. It must not start requiring
        # source copies or other initialization artifacts that normal recovery
        # does not read, or reject a legacy layout with no inbox at all.
        try:
            inbox_stat = (root / _INBOX_PATH).lstat()
        except (FileNotFoundError, NotADirectoryError):
            yield root, None
            return
        if stat.S_ISLNK(inbox_stat.st_mode):
            raise OSError("human-guidance inbox must not be a symlink")
    with ExitStack() as stack:
        root_fd = os.open(root, _DIRECTORY_FLAGS)
        stack.callback(os.close, root_fd)
        private_fd = os.open("private", _DIRECTORY_FLAGS, dir_fd=root_fd)
        stack.callback(os.close, private_fd)
        if create:
            config = json.loads(_read_file(private_fd, "runtime-config.json").decode("utf-8"))
            if (
                not isinstance(config, dict)
                or config.get("version") != 1
                or not isinstance(config.get("root_problem"), str)
                or not config["root_problem"].strip()
                or not isinstance(config.get("foundation_policy"), str)
                or not config["foundation_policy"].strip()
            ):
                raise ValueError("human guidance requires an initialized Franta project")
            for marker in ("root-problem.md", "foundation-v1.md", "bootstrap-manifest.toml"):
                with _regular_file(root_fd, marker):
                    pass
            with _regular_file(root_fd, "scheduler.sqlite3") as database_fd:
                if os.read(database_fd, 16) != b"SQLite format 3\x00":
                    raise ValueError("human guidance requires an initialized scheduler database")

        directory = private_fd
        for name in ("human-guidance", "inbox"):
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=directory)
                    os.fsync(directory)
                except FileExistsError:
                    pass
            try:
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory)
            except FileNotFoundError:
                if create:
                    raise
                yield root, None
                return
            stack.callback(os.close, child_fd)
            directory = child_fd
        yield root, directory


def _snapshot(filename: str, text: str, data: bytes) -> dict[str, Any]:
    match = _FILENAME.fullmatch(filename)
    if match is None:
        raise ValueError(f"invalid human-guidance filename: {filename}")
    timestamp = datetime.strptime(match[2], _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    return {
        "guidance_id": match[1],
        "text": text,
        "sha256": hashlib.sha256(data).hexdigest(),
        "relative_path": (_INBOX_PATH / filename).as_posix(),
        "received_at": timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }


def submit_human_guidance(project: str | Path, text: str) -> dict[str, Any]:
    """Publish exact UTF-8 Markdown once, without changing scheduler state."""

    data = _text_bytes(text)
    with _inbox(project, create=True) as (_root, directory):
        assert directory is not None
        timestamp = datetime.now(timezone.utc).strftime(_TIMESTAMP_FORMAT)
        guidance_id = f"HG-{timestamp}-{uuid.uuid4().hex}"
        filename = f"{guidance_id}.md"
        temporary_name = f".{guidance_id}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode=0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), 0o444)
                os.fsync(stream.fileno())
            # Unlike replace(), link() cannot overwrite an earlier submission.
            # The public filename appears only after the complete file is durable.
            os.link(
                temporary_name,
                filename,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
            os.fsync(directory)
        finally:
            os.unlink(temporary_name, dir_fd=directory)
        os.fsync(directory)
    return _snapshot(filename, text, data)


def read_human_guidance_inbox(project: str | Path) -> list[dict[str, Any]]:
    """Read completed submissions in stable order without creating any files."""

    records: list[dict[str, Any]] = []
    with _inbox(project, create=False) as (_root, directory):
        if directory is None:
            return records
        for filename in os.listdir(directory):
            if _FILENAME.fullmatch(filename) is None:
                continue
            data = _read_file(directory, filename)
            text = data.decode("utf-8")
            _text_bytes(text)
            records.append(_snapshot(filename, text, data))
    return sorted(records, key=lambda record: (record["received_at"], record["guidance_id"]))


__all__ = ["read_human_guidance_inbox", "submit_human_guidance"]
