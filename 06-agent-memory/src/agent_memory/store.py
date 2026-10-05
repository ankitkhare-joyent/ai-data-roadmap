"""A production-minded backend for Claude's memory tool (`memory_20250818`).

Claude asks for file operations under `/memories`; this class decides where they really go and what is
allowed. The reference implementations in the docs are deliberately minimal; this one adds the parts a
multi-tenant platform needs:

  isolation    every (tenant, user) gets its own root. The model never sees or chooses the root, so one
               user's memory can't leak into another's, however the conversation goes
  traversal    every path is resolved and must stay under that root (`..`, absolute tricks, URL-encoding,
               symlinks)
  hygiene      secrets and credentials are redacted before anything is written; file and total size caps
  provenance   every write is appended to an audit log with the session id and the source of the
               instruction (user turn or tool output)
  expiry       last access is tracked, so a nightly job can expire memories nobody has read in months

Command names, parameters and return strings follow the memory tool documentation.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

ROOT = "/memories"
MAX_FILE_CHARS = 20_000
MAX_TOTAL_CHARS = 200_000
VIEW_CHARS = 16_000

SECRET_PATTERNS = [
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,})\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(?i)\b(password|passwd|secret|token)\s*[:=]\s*\S+"),
]


class MemoryError_(Exception):
    """Returned to Claude as an is_error tool result."""


@dataclass
class Result:
    content: str
    is_error: bool = False


def _size(n: int) -> str:
    return f"{n / 1024:.1f}K" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f}M"


class MemoryStore:
    def __init__(self, base: str | Path, tenant: str, user: str):
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", tenant) or not re.fullmatch(r"[a-z0-9_.@-]{1,128}", user):
            raise ValueError("tenant/user ids must be simple identifiers")   # they become directory names
        self.root = (Path(base) / tenant / user).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.audit = self.root.parent / f".audit-{user}.jsonl"   # outside the user's tree: not model-visible

    # ------------------------------------------------------------------ path safety
    def _resolve(self, path: str) -> Path:
        decoded = urllib.parse.unquote(path or "")
        if decoded != ROOT and not decoded.startswith(ROOT + "/"):
            raise MemoryError_(f"Error: paths must start with {ROOT}")
        if "\\" in decoded or "\x00" in decoded:
            raise MemoryError_("Error: invalid characters in path")
        real = (self.root / decoded[len(ROOT):].lstrip("/")).resolve()
        if real != self.root and self.root not in real.parents:
            raise MemoryError_("Error: path escapes the memory directory")
        return real

    def _virtual(self, real: Path) -> str:
        rel = real.relative_to(self.root).as_posix()
        return ROOT if rel == "." else f"{ROOT}/{rel}"

    def _total_chars(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def _clean(self, text: str) -> str:
        for p in SECRET_PATTERNS:
            text = p.sub("[REDACTED]", text)
        return text

    def _write(self, real: Path, text: str, session: str, source: str, command: str) -> None:
        text = self._clean(text)
        if len(text) > MAX_FILE_CHARS:
            raise MemoryError_(f"Error: memory files are capped at {MAX_FILE_CHARS} characters")
        current = real.stat().st_size if real.exists() else 0
        if self._total_chars() - current + len(text) > MAX_TOTAL_CHARS:
            raise MemoryError_("Error: memory is full; consolidate or delete old files first")
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text(text)
        self._log(command, self._virtual(real), session, source)

    def _log(self, command: str, path: str, session: str, source: str) -> None:
        with self.audit.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "command": command, "path": path, "session": session,
                                "source": source}) + "\n")

    # ------------------------------------------------------------------ commands
    def execute(self, cmd: dict, session: str = "?", source: str = "model") -> Result:
        try:
            return Result(getattr(self, f"_cmd_{cmd['command']}")(cmd, session, source))
        except MemoryError_ as e:
            return Result(str(e), is_error=True)
        except (KeyError, AttributeError) as e:
            return Result(f"Error: invalid memory command ({e})", is_error=True)

    def _cmd_view(self, cmd, session, source) -> str:
        real = self._resolve(cmd["path"])
        if real.is_dir():
            lines = [f"{_size(sum(p.stat().st_size for p in real.rglob('*') if p.is_file()))}\t{self._virtual(real)}"]
            for p in sorted(real.rglob("*")):
                rel = p.relative_to(real)
                if len(rel.parts) > 2 or any(part.startswith(".") or part == "node_modules" for part in rel.parts):
                    continue
                lines.append(f"{_size(p.stat().st_size if p.is_file() else 0)}\t{self._virtual(p)}")
            return (f"Here're the files and directories up to 2 levels deep in {self._virtual(real)}, "
                    "excluding hidden items and node_modules:\n" + "\n".join(lines))
        if not real.is_file():
            raise MemoryError_(f"The path {cmd['path']} does not exist. Please provide a valid path.")
        real.touch()                                   # last access, for expiry
        text = real.read_text()[:VIEW_CHARS]
        rows = text.split("\n")
        start, end = 1, len(rows)
        if cmd.get("view_range"):
            start, end = cmd["view_range"][0], cmd["view_range"][1]
            end = len(rows) if end == -1 else end
        body = "\n".join(f"{i:6d}\t{rows[i - 1]}" for i in range(max(1, start), min(end, len(rows)) + 1))
        return f"Here's the content of {cmd['path']} with line numbers:\n{body}"

    def _cmd_create(self, cmd, session, source) -> str:
        real = self._resolve(cmd["path"])
        if real == self.root or real.is_dir():
            raise MemoryError_(f"Error: {cmd['path']} is a directory")
        self._write(real, cmd.get("file_text", ""), session, source, "create")   # create-or-overwrite
        return f"File created successfully at: {cmd['path']}"

    def _cmd_str_replace(self, cmd, session, source) -> str:
        real = self._resolve(cmd["path"])
        if not real.is_file():
            raise MemoryError_(f"Error: The path {cmd['path']} does not exist. Please provide a valid path.")
        text, old = real.read_text(), cmd["old_str"]
        count = text.count(old)
        if count == 0:
            raise MemoryError_(f"No replacement was performed, old_str `{old}` did not appear verbatim in {cmd['path']}.")
        if count > 1:
            lines = [i + 1 for i, line in enumerate(text.split("\n")) if old in line]
            raise MemoryError_(f"No replacement was performed. Multiple occurrences of old_str `{old}` in lines: "
                               f"{lines}. Please ensure it is unique")
        self._write(real, text.replace(old, cmd.get("new_str", ""), 1), session, source, "str_replace")
        return "The memory file has been edited."

    def _cmd_insert(self, cmd, session, source) -> str:
        real = self._resolve(cmd["path"])
        if not real.is_file():
            raise MemoryError_(f"Error: The path {cmd['path']} does not exist")
        lines = real.read_text().split("\n")
        n = int(cmd["insert_line"])
        if n < 0 or n > len(lines):
            raise MemoryError_(f"Error: Invalid `insert_line` parameter: {n}. It should be within the range of "
                               f"lines of the file: [0, {len(lines)}]")
        lines[n:n] = [cmd["insert_text"].rstrip("\n")]
        self._write(real, "\n".join(lines), session, source, "insert")
        return f"The file {cmd['path']} has been edited."

    def _cmd_delete(self, cmd, session, source) -> str:
        real = self._resolve(cmd["path"])
        if real == self.root:
            raise MemoryError_("Error: cannot delete the /memories directory itself")
        if not real.exists():
            raise MemoryError_(f"Error: The path {cmd['path']} does not exist")
        shutil.rmtree(real) if real.is_dir() else real.unlink()
        self._log("delete", cmd["path"], session, source)
        return f"Successfully deleted {cmd['path']}"

    def _cmd_rename(self, cmd, session, source) -> str:
        old, new = self._resolve(cmd["old_path"]), self._resolve(cmd["new_path"])
        if old == self.root:
            raise MemoryError_("Error: cannot rename the /memories directory itself")
        if not old.exists():
            raise MemoryError_(f"Error: The path {cmd['old_path']} does not exist")
        if new.exists():
            raise MemoryError_(f"Error: The destination {cmd['new_path']} already exists")
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
        self._log("rename", f"{cmd['old_path']} -> {cmd['new_path']}", session, source)
        return f"Successfully renamed {cmd['old_path']} to {cmd['new_path']}"

    # ------------------------------------------------------------------ operations outside the model's reach
    def expire(self, older_than_days: float) -> list[str]:
        """Delete files not read or written for N days. Run nightly; the model can't call this."""
        cutoff = time.time() - older_than_days * 86400
        gone = [self._virtual(p) for p in self.root.rglob("*") if p.is_file() and p.stat().st_mtime < cutoff]
        for v in gone:
            self._resolve(v).unlink()
        return gone
