"""Read ComfyUI workflow files and collect the models they reference.

Only JSON files that are workflows (UI format with a "nodes" list, or API format
with "class_type" entries) are read. Settings and other JSON files are ignored.

Two kinds of names are collected:
  * node references: the widget values (UI format) or inputs (API format) of a
    node, plus <lora:name> / embedding:name tags in prompt text. Together with
    the node type they say where ComfyUI looks for the model: a LoraLoader with
    "h3\\x.safetensors" loads models/loras/h3/x.safetensors.
  * mentions: every other text - notes, links, titles and the download list
    ComfyUI keeps in node properties. They do not load a model, but they still
    count for the loose matching used for custom node model folders.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .categories import STANDARD_CATEGORIES, node_categories

# Strings up to this length (on one line) are treated as possible paths.
_MAX_NAME_LEN = 300
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)
_EMBEDDING_RE = re.compile(r"embedding:\s*([^\s,()\[\]<>:\"']+)", re.IGNORECASE)
_LORA_TAG_RE = re.compile(r"<lora:([^:>]+)", re.IGNORECASE)
_DRIVE_RE = re.compile(r"^[a-z]:/", re.IGNORECASE)
# Nodes that only hold text for people to read.
_NOTE_NODE_RE = re.compile(r"note|markdown|label|comment", re.IGNORECASE)
# Text formatted for people (e.g. an HTML "models used in this workflow" box) is
# documentation, even inside a node that is not called a note.
_DOCUMENT_RE = re.compile(r"<(?:h[1-6]|p|div|span|br|hr|ul|ol|li|a|table|b|strong|em|img)[\s>/]",
                          re.IGNORECASE)
# Node keys whose strings are the node's settings (newer frontends also save
# the widgets by name).
_WIDGET_KEYS = ("widgets_values", "widgets_values_named")


def _file_name_regex(extensions: list[str]) -> re.Pattern[str]:
    exts = sorted({e.lower().lstrip(".") for e in extensions}, key=len, reverse=True)
    return re.compile(
        r"[^\s\"'<>|*?:,;()\[\]{}=]+\.(?:" + "|".join(map(re.escape, exts)) + r")(?![a-z0-9])",
        re.IGNORECASE,
    )


def normalize(text: str) -> str:
    """Forward slashes, no surrounding quotes or './'; the case is kept."""
    text = text.strip().strip("\"'").strip().replace("\\", "/")
    text = re.sub(r"/{2,}", "/", text)
    while text.startswith("./"):
        text = text[2:]
    return text.strip()


def strip_ext(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return name[: len(name) - len(base)] + base.rsplit(".", 1)[0] if "." in base else name


@dataclass(frozen=True)
class Reference:
    """A model named by a node, e.g. UNETLoader -> "h3/x.safetensors"."""

    original: str    # path as saved (forward slashes, original case)
    node: str        # node type
    workflow: str
    # Model folders the node reads from; None = unknown node (any ComfyUI folder).
    categories: tuple[str, ...] | None
    # Folder named in the path itself (unknown nodes that store "loras/x" or a full path).
    folder: str | None = None
    # Path inside the model folder, original case.
    inner_original: str = ""
    # True when the location cannot be judged (links, absolute paths outside the
    # models folder): the model counts as used wherever it is.
    anywhere: bool = False

    @property
    def inner(self) -> str:
        return self.inner_original.lower()

    @property
    def basename(self) -> str:
        return self.original.rsplit("/", 1)[-1].lower()


def make_reference(original: str, node: str, workflow: str,
                   categories: tuple[str, ...] | None, anywhere: bool = False) -> Reference:
    parts = [p for p in original.split("/") if p]
    folder = None
    inner = "/".join(parts)
    if categories is None:
        # The path may contain the model folder ("loras/h3/x" or "C:/ComfyUI/models/loras/x").
        for index in range(len(parts) - 2, -1, -1):
            if parts[index].lower() in STANDARD_CATEGORIES:
                folder, inner = parts[index], "/".join(parts[index + 1:])
                break
    if folder is None and (_DRIVE_RE.match(original) or original.startswith("/")
                           or ":" in inner or ".." in parts):
        anywhere = True
    return Reference(original, node, workflow, categories, folder, inner, anywhere)


@dataclass
class WorkflowRefs:
    # File name (lower case, also without extension) -> node references.
    node_refs: dict[str, list[Reference]] = field(default_factory=dict)
    # Last path part of every reference and mention -> workflows.
    names: dict[str, set[str]] = field(default_factory=dict)
    # Every path part of every reference and mention -> workflows.
    segments: dict[str, set[str]] = field(default_factory=dict)
    workflows: list[str] = field(default_factory=list)
    ignored_json: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    _seen: set[tuple] = field(default_factory=set, repr=False)

    @staticmethod
    def _add(table: dict[str, set[str]], key: str, workflow: str) -> None:
        if key:
            table.setdefault(key, set()).add(workflow)

    def add_mention(self, text: str, workflow: str) -> None:
        parts = [p.strip() for p in normalize(text).lower().split("/") if p.strip()]
        if not parts:
            return
        self._add(self.names, parts[-1], workflow)
        for part in parts:
            self._add(self.segments, part, workflow)

    def add_node_ref(self, text: str, node: str, workflow: str,
                     categories: tuple[str, ...] | None, anywhere: bool = False) -> None:
        original = normalize(text)
        if not original or original.endswith("/"):
            return
        self.add_mention(original, workflow)
        key = (original.lower(), node, workflow, categories, anywhere)
        if key in self._seen:
            return
        self._seen.add(key)
        ref = make_reference(original, node, workflow, categories, anywhere)
        self.node_refs.setdefault(ref.basename, []).append(ref)

    def refs_named(self, name: str) -> list[Reference]:
        """Node references whose file name is `name`, with or without its extension."""
        name = name.lower()
        found = list(self.node_refs.get(name, ()))
        stem = strip_ext(name)
        if stem != name:
            found += self.node_refs.get(stem, [])
        return found

    def mentions_of(self, kind: str, name: str) -> set[str]:
        """Loose matching: workflows that name this file or folder anywhere."""
        name = name.lower()
        if kind == "file":
            found = set(self.names.get(name, ()))
            stem = strip_ext(name)
            if stem != name:  # some nodes list models without the extension
                found |= self.names.get(stem, set())
            return found
        return set(self.segments.get(name, ())) | self.names.get(name, set())


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


class _Scanner:
    def __init__(self, refs: WorkflowRefs, workflow: str, file_re: re.Pattern[str]):
        self.refs = refs
        self.workflow = workflow
        self.file_re = file_re

    def mention(self, value: str) -> None:
        text = value.strip()
        if not text:
            return
        if len(text) <= _MAX_NAME_LEN and "\n" not in text:
            self.refs.add_mention(text, self.workflow)
        for match in self.file_re.finditer(normalize(text)):
            self.refs.add_mention(match.group(0), self.workflow)
        for match in _URL_RE.finditer(text):
            self._url_path(match.group(0), self.refs.add_mention)
        for regex in (_EMBEDDING_RE, _LORA_TAG_RE):
            for match in regex.finditer(text):
                self.refs.add_mention(match.group(1), self.workflow)

    def _url_path(self, url: str, add) -> None:
        url = url.split("?", 1)[0].split("#", 1)[0]
        host_and_path = url.split("://", 1)[1].split("/", 1)
        if len(host_and_path) == 2:
            add(host_and_path[1], self.workflow)

    def node_value(self, value: str, node: str, categories: tuple[str, ...] | None) -> None:
        text = value.strip()
        if not text:
            return
        if _DOCUMENT_RE.search(text):
            self.mention(text)
            return
        add = self.refs.add_node_ref
        wf = self.workflow
        # A model in a link (download nodes) counts as used wherever it is.
        for match in _URL_RE.finditer(text):
            self._url_path(match.group(0), lambda path, w: add(path, node, w, None, anywhere=True))
        rest = _URL_RE.sub(" ", text)
        if rest == text and len(text) <= _MAX_NAME_LEN and "\n" not in text:
            add(text, node, wf, categories)
        for match in self.file_re.finditer(normalize(rest)):
            add(match.group(0), node, wf, categories)
        for match in _LORA_TAG_RE.finditer(rest):
            add(match.group(1), node, wf, ("loras",))
        for match in _EMBEDDING_RE.finditer(rest):
            add(match.group(1), node, wf, ("embeddings",))

    def ui_node(self, node: dict) -> None:
        node_type = str(node.get("type") or "")
        is_note = bool(_NOTE_NODE_RE.search(node_type))
        categories = node_categories(node_type)
        for key, value in node.items():
            if key in _WIDGET_KEYS and not is_note:
                for text in _iter_strings(value):
                    self.node_value(text, node_type, categories)
            else:
                for text in _iter_strings(value):
                    self.mention(text)

    def api_node(self, node: dict) -> None:
        node_type = str(node.get("class_type") or "")
        categories = node_categories(node_type)
        for key, value in node.items():
            if key == "inputs" and not _NOTE_NODE_RE.search(node_type):
                for text in _iter_strings(value):
                    self.node_value(text, node_type, categories)
            else:
                for text in _iter_strings(value):
                    self.mention(text)

    def walk(self, obj: object) -> None:
        stack = [obj]
        while stack:
            current = stack.pop()
            if isinstance(current, str):
                self.mention(current)
            elif isinstance(current, list):
                stack.extend(current)
            elif isinstance(current, dict):
                if isinstance(current.get("type"), str) and "id" in current and (
                        "properties" in current or any(k in current for k in _WIDGET_KEYS)):
                    self.ui_node(current)
                elif isinstance(current.get("class_type"), str) and isinstance(current.get("inputs"), dict):
                    self.api_node(current)
                else:
                    stack.extend(current.values())


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
        _Scanner(refs, rel, file_re).walk(data)
    return refs

