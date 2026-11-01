"""Inventory of the ComfyUI models folder.

A "model" is either
  * a single model file (e.g. loras/foo.safetensors), or
  * a model folder downloaded from Hugging Face (e.g. whisper/whisper-large-v3),
    recognised by a .cache/huggingface folder or a config.json/model_index.json
    next to the weights. Such folders are handled (and deleted) as one unit.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from .workflows import WorkflowRefs

# Folder names ComfyUI itself defines inside the models folder. These are never
# treated as a single model and are never deleted as a whole.
STANDARD_CATEGORIES = frozenset({
    "audio_encoders", "background_removal", "checkpoints", "classifiers", "clip",
    "clip_vision", "configs", "controlnet", "custom_nodes", "datasets", "detection",
    "diffusers", "diffusion_models", "embeddings", "frame_interpolation",
    "geometry_estimation", "gligen", "hypernetworks", "latent_upscale_models",
    "loras", "model_patches", "optical_flow", "photomaker", "style_models",
    "t2i_adapter", "text_encoders", "unet", "upscale_models", "vae", "vae_approx",
})


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
class Classification:
    used: list[tuple[ModelUnit, set[str]]]
    unused: list[ModelUnit]
    kept: list[ModelUnit]


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


def is_model_folder(path: Path, models_dir: Path, extensions: list[str]) -> bool:
    """True if `path` is a folder the inventory would treat as one model."""
    exts = {e.lower() for e in extensions}
    try:
        depth = len(path.relative_to(models_dir).parts)
        entries = list(os.scandir(path))
    except (OSError, ValueError):
        return False
    names = {e.name.lower() for e in entries}
    has_weights = any(_is_file(e) and os.path.splitext(e.name)[1].lower() in exts for e in entries)
    return _is_folder_model(path, depth, names, has_weights)


def is_custom_node_folder(unit: ModelUnit) -> bool:
    """Models outside ComfyUI's own folders belong to custom nodes, which often
    choose their model from a preset or the node type instead of a file name."""
    return unit.rel.split("/", 1)[0].lower() not in STANDARD_CATEGORIES


def matches_keep(rel: str, patterns: list[str]) -> bool:
    rel = rel.lower()
    return any(fnmatch.fnmatchcase(rel, p.strip().lower().replace("\\", "/")) for p in patterns)


def classify(units: list[ModelUnit], refs: WorkflowRefs, keep_patterns: list[str]) -> Classification:
    result = Classification([], [], [])
    for unit in units:
        if matches_keep(unit.rel, keep_patterns):
            result.kept.append(unit)
            continue
        users = refs.references_to(unit.kind, unit.name)
        if users:
            result.used.append((unit, users))
        else:
            result.unused.append(unit)
    return result
