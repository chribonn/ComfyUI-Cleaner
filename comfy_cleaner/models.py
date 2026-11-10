"""Inventory of the ComfyUI models folder, and how each model relates to the workflows.

A "model" is either
  * a single model file (e.g. loras/foo.safetensors), or
  * a model folder downloaded from Hugging Face (e.g. whisper/whisper-large-v3),
    recognised by a .cache/huggingface folder or a config.json/model_index.json
    next to the weights. Such folders are handled (and deleted) as one unit.

Every model gets one status:
  used       a workflow loads it from exactly this place
  misplaced  a workflow names the file, but looks for it in another folder and
             no copy is there. It is never deleted; it can be moved instead.
  orphan     no workflow uses it
and models with the same name in more than one place are marked as duplicates.

Files in ComfyUI's own model folders are matched strictly: path inside the model
folder plus the folder the node reads from. Custom node folders and model folders
are matched loosely by name, because custom nodes follow their own conventions.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from .categories import STANDARD_CATEGORIES, canonical_category, target_category
from .workflows import Reference, WorkflowRefs, strip_ext

USED, MISPLACED, ORPHAN = "used", "misplaced", "orphan"


@dataclass
class ModelUnit:
    path: Path
    rel: str    # relative to the models folder, forward slashes
    kind: str   # "file" or "folder"
    name: str
    size: int
    # For folders: (path inside the folder, size) of each weight file.
    weight_files: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class ModelStatus:
    unit: ModelUnit
    status: str            # USED, MISPLACED or ORPHAN
    custom: bool           # in a custom node folder (loose matching, never pre-marked)
    users: set[str] = field(default_factory=set)        # workflows that load this copy
    # Where workflows look for this file without finding any copy: models-relative
    # path ("<folder>/..." when the model folder is unknown) -> workflows.
    expected: dict[str, set[str]] = field(default_factory=dict)
    move_to: Path | None = None   # misplaced: the one place all those workflows expect it
    move_note: str = ""           # why no move is offered
    mentions: set[str] = field(default_factory=set)     # orphans: workflows that only name it
    other_copy_used: bool = False  # orphans: the workflows that name it use another copy
    dup_group: int = 0             # > 0: same name exists elsewhere (group number)
    dup_same_size: bool = True

    @property
    def is_duplicate(self) -> bool:
        return self.dup_group > 0

    @property
    def deletable(self) -> bool:
        """Misplaced models are only deletable when another copy exists."""
        return self.status != MISPLACED or self.is_duplicate

    @property
    def listed(self) -> bool:
        return self.status != USED or self.is_duplicate

    @property
    def needed(self) -> bool:
        """A workflow uses or looks for this file."""
        return self.status in (USED, MISPLACED)


@dataclass
class Classification:
    statuses: list[ModelStatus]
    kept: list[ModelUnit]

    def with_status(self, status: str) -> list[ModelStatus]:
        return [s for s in self.statuses if s.status == status]

    @property
    def duplicate_groups(self) -> dict[int, list[ModelStatus]]:
        groups: dict[int, list[ModelStatus]] = {}
        for status in self.statuses:
            if status.dup_group:
                groups.setdefault(status.dup_group, []).append(status)
        return groups


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _is_file(entry: os.DirEntry) -> bool:
    try:
        return entry.is_file()
    except OSError:
        return False


def folder_stats(root: Path, extensions: set[str]) -> tuple[int, list[tuple[str, int]]]:
    total = 0
    weights: list[tuple[str, int]] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        hidden = any(part.startswith(".") for part in rel_dir.parts)
        for filename in filenames:
            try:
                size = os.stat(os.path.join(dirpath, filename), follow_symlinks=False).st_size
            except OSError:
                continue
            total += size
            if not hidden and os.path.splitext(filename)[1].lower() in extensions:
                weights.append(((rel_dir / filename).as_posix(), size))
    return total, weights


def _is_folder_model(path: Path, depth: int, names: set[str], has_weights: bool) -> bool:
    if depth == 0:
        return False
    if ".cache" in names and (path / ".cache" / "huggingface").is_dir():
        return not (depth == 1 and path.name.lower() in STANDARD_CATEGORIES)
    if depth >= 2:
        if "model_index.json" in names:
            return True
        if "config.json" in names and has_weights:
            return True
    return False


def inventory(models_dir: Path, extensions: list[str]) -> tuple[list[ModelUnit], list[str]]:
    exts = {e.lower() for e in extensions}
    units: list[ModelUnit] = []
    warnings: list[str] = []

    def rel_of(path: Path) -> str:
        return path.relative_to(models_dir).as_posix()

    def walk(directory: Path, depth: int) -> None:
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError as exc:
            warnings.append(f"Cannot read {directory}: {exc}")
            return
        names = {e.name.lower() for e in entries}
        has_weights = any(
            _is_file(e) and os.path.splitext(e.name)[1].lower() in exts for e in entries)
        if _is_folder_model(directory, depth, names, has_weights):
            size, weights = folder_stats(directory, exts)
            units.append(ModelUnit(directory, rel_of(directory), "folder", directory.name, size, weights))
            return
        for entry in sorted(entries, key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            path = Path(entry.path)
            if _is_dir(entry):
                walk(path, depth + 1)
            elif _is_file(entry) and path.suffix.lower() in exts:
                try:
                    size = entry.stat().st_size
                except OSError as exc:
                    warnings.append(f"Cannot read {path}: {exc}")
                    continue
                units.append(ModelUnit(path, rel_of(path), "file", entry.name, size))

    walk(models_dir, 0)
    return units, warnings


def is_custom_node_folder(unit: ModelUnit) -> bool:
    """Models outside ComfyUI's own folders belong to custom nodes, which often
    choose their model from a preset or the node type instead of a file name."""
    return unit.rel.split("/", 1)[0].lower() not in STANDARD_CATEGORIES


def matches_keep(rel: str, patterns: list[str]) -> bool:
    rel = rel.lower()
    return any(fnmatch.fnmatchcase(rel, p.strip().lower().replace("\\", "/")) for p in patterns)


# ------------------------------------------------------------------ strict matching
def _location(unit: ModelUnit) -> tuple[str, str, str] | None:
    """(model folder as on disk, canonical model folder, path inside it) for files
    in one of ComfyUI's own model folders; None for everything else."""
    if unit.kind != "file":
        return None
    parts = unit.rel.split("/")
    if len(parts) < 2 or parts[0].lower() not in STANDARD_CATEGORIES:
        return None
    return parts[0], canonical_category(parts[0]), "/".join(parts[1:])


def is_exact(ref: Reference, unit: ModelUnit) -> bool:
    """Does the node that made `ref` load exactly this file?"""
    loc = _location(unit)
    if loc is None:
        return False
    if ref.anywhere:
        return True
    _raw, canon, inner = loc
    inner = inner.lower()
    if ref.inner not in (inner, strip_ext(inner)):
        return False
    if ref.folder is not None:
        return canonical_category(ref.folder) == canon
    if ref.categories is not None:
        return canon in ref.categories
    return True  # unknown node: the path may be inside any of ComfyUI's model folders


def _destination(ref: Reference, unit: ModelUnit) -> tuple[str, bool]:
    """Where `ref` expects this file: (models-relative path, folder known)."""
    raw, canon, _inner = _location(unit)  # type: ignore[misc]
    parts = ref.inner_original.split("/")
    parts[-1] = unit.name  # the reference may leave out the extension
    inner = "/".join(parts)
    if ref.folder is not None:
        folder = ref.folder
    elif ref.categories is not None:
        folder = raw if canon in ref.categories else target_category(ref.categories)
    else:
        folder = raw  # unknown node: assume the file is in the right model folder
    if folder is None:
        return f"<{'|'.join(ref.categories or ())}>/{inner}", False
    return f"{folder}/{inner}", True


def _plan_move(status: ModelStatus, models_dir: Path, places: dict[str, tuple[str, bool]]) -> None:
    keys = {path.lower() for path, _ in places.values()}
    if len(keys) != 1:
        status.move_note = "the workflows expect it in different places"
        return
    path, known = next(iter(places.values()))
    if not known:
        status.move_note = "the node can read from more than one model folder"
        return
    target = models_dir.joinpath(*path.split("/"))
    if os.path.lexists(target):
        status.move_note = "a file or folder already exists there"
        return
    status.move_to = target


def classify(units: list[ModelUnit], refs: WorkflowRefs, keep_patterns: list[str],
             models_dir: Path) -> Classification:
    kept = [u for u in units if matches_keep(u.rel, keep_patterns)]
    active = [u for u in units if not matches_keep(u.rel, keep_patterns)]

    # Files by name and by name without extension, to see if a reference is satisfied.
    by_name: dict[str, list[ModelUnit]] = {}
    for unit in units:
        if unit.kind == "file":
            name = unit.name.lower()
            by_name.setdefault(name, []).append(unit)
            if strip_ext(name) != name:
                by_name.setdefault(strip_ext(name), []).append(unit)
    satisfied_cache: dict[Reference, bool] = {}

    def satisfied(ref: Reference) -> bool:
        if ref not in satisfied_cache:
            satisfied_cache[ref] = any(is_exact(ref, u) for u in by_name.get(ref.basename, ()))
        return satisfied_cache[ref]

    statuses: list[ModelStatus] = []
    for unit in active:
        custom = is_custom_node_folder(unit)
        if _location(unit) is None:  # custom node folders and model folders: loose matching
            users = refs.mentions_of(unit.kind, unit.name)
            statuses.append(ModelStatus(unit, USED if users else ORPHAN, custom, users=users))
            continue

        named = refs.refs_named(unit.name)
        exact = [r for r in named if is_exact(r, unit)]
        missing = [r for r in named if r not in exact and not satisfied(r)]
        status = ModelStatus(unit, ORPHAN, custom)
        places: dict[str, tuple[str, bool]] = {}
        for ref in missing:
            path, known = _destination(ref, unit)
            status.expected.setdefault(path, set()).add(ref.workflow)
            places[path.lower()] = (path, known)
        if exact:
            status.status = USED
            status.users = {r.workflow for r in exact}
        elif missing:
            status.status = MISPLACED
            _plan_move(status, models_dir, places)
        else:
            status.mentions = refs.mentions_of("file", unit.name)
            status.other_copy_used = bool(named)
        statuses.append(status)

    # Duplicates: the same file or folder name in more than one place.
    groups: dict[tuple[str, str], list[ModelStatus]] = {}
    for status in statuses:
        groups.setdefault((status.unit.kind, status.unit.name.lower()), []).append(status)
    number = 0
    for members in sorted(groups.values(), key=lambda m: min(s.unit.rel.lower() for s in m)):
        if len(members) < 2:
            continue
        number += 1
        same_size = len({s.unit.size for s in members}) == 1
        for status in members:
            status.dup_group = number
            status.dup_same_size = same_size
    return Classification(statuses, kept)
