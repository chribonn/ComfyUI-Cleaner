"""Read ComfyUI workflow files and collect every model name they mention.

Only JSON files that are actual workflows (UI format with a "nodes" list, or API
format with "class_type" entries) are used. Settings files, media and other JSON
files in the user folder are ignored.

Matching errs on the side of keeping models: every string in a workflow
(widget values, subgraph definitions, the model list ComfyUI stores in node
properties, URLs and file names inside notes) counts as a possible reference.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Strings up to this length are treated as possible file names / paths.
_MAX_NAME_LEN = 300
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)
_EMBEDDING_RE = re.compile(r"embedding:\s*([^\s,()\[\]<>:\"']+)", re.IGNORECASE)
_LORA_TAG_RE = re.compile(r"<lora:([^:>]+)", re.IGNORECASE)


def _file_name_regex(extensions: list[str]) -> re.Pattern[str]:
    exts = sorted({e.lower().lstrip(".") for e in extensions}, key=len, reverse=True)
    return re.compile(
        r"[^\s\"'<>|*?:,;()\[\]{}=]+\.(?:" + "|".join(map(re.escape, exts)) + r")(?![a-z0-9])",
        re.IGNORECASE,
    )


@dataclass
class WorkflowRefs:
    """Model names referenced by workflows, mapped to the workflows that use them."""

    basenames: dict[str, set[str]] = field(default_factory=dict)  # last path part
    segments: dict[str, set[str]] = field(default_factory=dict)   # every path part
    workflows: list[str] = field(default_factory=list)
    ignored_json: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def _add(self, table: dict[str, set[str]], key: str, workflow: str) -> None:
        key = key.strip().strip("\"'").strip()
        if key:
            table.setdefault(key, set()).add(workflow)

    def add_path_like(self, text: str, workflow: str) -> None:
        """`text` must already be lower-case with forward slashes."""
        parts = [p.strip() for p in text.split("/") if p.strip()]
        if not parts:
            return
        self._add(self.basenames, parts[-1], workflow)
        for part in parts:
            self._add(self.segments, part, workflow)

    def references_to(self, kind: str, name: str) -> set[str]:
        """Workflows that reference a model file or model folder with this name."""
        name = name.lower()
        if kind == "file":
            found = set(self.basenames.get(name, ()))
            stem = name.rsplit(".", 1)[0]
            if stem != name:  # some nodes list models without the extension
                found |= self.basenames.get(stem, set())
            return found
        return set(self.segments.get(name, ())) | self.basenames.get(name, set())


def is_workflow(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    if isinstance(data.get("nodes"), list):
        return True
    values = list(data.values())
    return bool(values) and all(isinstance(v, dict) for v in values) and any(
        "class_type" in v for v in values)


def _iter_strings(obj: object):
    stack = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            yield current
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _add_string(refs: WorkflowRefs, value: str, workflow: str, file_re: re.Pattern[str]) -> None:
    text = value.strip().lower().replace("\\", "/")
    if not text:
        return
    if len(text) <= _MAX_NAME_LEN and "\n" not in text:
        refs.add_path_like(text, workflow)
    for match in file_re.finditer(text):
        refs.add_path_like(match.group(0), workflow)
    for match in _URL_RE.finditer(text):
        url = match.group(0).split("?", 1)[0].split("#", 1)[0]
        host_and_path = url.split("://", 1)[1].split("/", 1)
        if len(host_and_path) == 2:
            refs.add_path_like(host_and_path[1], workflow)
    for regex in (_EMBEDDING_RE, _LORA_TAG_RE):
        for match in regex.finditer(text):
            refs.add_path_like(match.group(1), workflow)


def scan_workflows(user_dir: Path, extensions: list[str]) -> WorkflowRefs:
    refs = WorkflowRefs()
    file_re = _file_name_regex(extensions)
    for path in sorted(user_dir.rglob("*.json")):
        rel_parts = path.relative_to(user_dir).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        rel = "/".join(rel_parts)
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError) as exc:
            refs.skipped.append((rel, f"could not be read ({exc})"))
            continue
        if not is_workflow(data):
            refs.ignored_json += 1
            continue
        refs.workflows.append(rel)
        for value in _iter_strings(data):
            _add_string(refs, value, rel, file_re)
    return refs
