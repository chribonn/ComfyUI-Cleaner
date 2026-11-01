"""The editable list of models to delete, written by the finder and read by the deleter.

Format: plain text. Every line that does not start with '#' is the full path of a
model file or model folder to delete. Lines starting with '#' are comments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import ModelUnit, is_custom_node_folder
from .util import human_size
from .verify import Verification


@dataclass
class ListEntry:
    unit: ModelUnit
    verification: Verification
    selected: bool


def norm_path(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def write_list(path: Path, entries: list[ListEntry], models_dir: Path) -> None:
    selected = [e for e in entries if e.selected]
    lines = [
        "# ComfyUI-Cleaner - models that no workflow uses",
        f"# Created {datetime.now():%Y-%m-%d %H:%M} for {models_dir}",
        "#",
        "# delete_unused_models.py deletes every line below that does NOT start with '#'.",
        "# To keep a model: delete its line, or put '#' in front of it.",
        "# Models that could not be verified online start with '#' (kept). Remove the '#'",
        "# to delete them anyway. 'custom node' marks models a custom node may load by",
        "# itself without naming them in the workflow - check those before deleting.",
        "#",
        f"# Selected: {len(selected)} of {len(entries)} unused models, "
        f"{human_size(sum(e.unit.size for e in selected))}",
    ]
    for entry in entries:
        unit, check = entry.unit, entry.verification
        info = [human_size(unit.size), "folder" if unit.kind == "folder" else "file", check.label]
        if is_custom_node_folder(unit):
            info.append("custom node")
        if check.url:
            info.append(check.url)
        lines.append("")
        lines.append("# " + " | ".join(info))
        lines.append(("" if entry.selected else "# ") + str(unit.path))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_list(path: Path) -> list[str]:
    paths: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            paths.append(line.strip('"').strip())
    return paths
