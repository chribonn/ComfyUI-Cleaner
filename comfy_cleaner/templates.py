"""ComfyUI's own example workflows: templates and blueprints.

They are not the user's workflows, so they never make a model "used". An orphan
that one of them loads is a "template model": it is flagged in the review list
and marked for deletion like any other orphan, unless the finder runs with
--keep-template-models.

Where ComfyUI (0.43) keeps them:
  blueprints  ready-made subgraph nodes: ComfyUI/blueprints/*.json and
              custom_nodes/<pack>/subgraphs/*.json
  templates   the Templates browser: <package>/templates/*.json of the
              comfyui_workflow_templates* packages in ComfyUI's Python
              environment, and custom_nodes/<pack>/example_workflows/*.json
              (or example, examples, workflow, workflows)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Folders of a custom node pack that ComfyUI shows in the Templates browser.
EXAMPLE_FOLDERS = ("example_workflows", "example", "examples", "workflow", "workflows")
# Python environments of portable/Easy-Install, venv and desktop installs.
PYTHON_FOLDERS = ("python_embeded", "python_embedded", "venv", ".venv")


@dataclass
class TemplateFiles:
    files: list[tuple[str, Path]] = field(default_factory=list)  # (label, path)
    package_dirs: list[Path] = field(default_factory=list)       # built-in template folders found


def is_blueprint(label: str) -> bool:
    return label.startswith("blueprint ")


def _json_files(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.glob("*.json") if p.is_file())
    except OSError:
        return []


def _node_packs(comfyui_dir: Path) -> list[Path]:
    """Enabled custom node packs (ComfyUI skips hidden and *.disabled folders)."""
    try:
        packs = [p for p in (comfyui_dir / "custom_nodes").iterdir() if p.is_dir()]
    except OSError:
        return []
    return sorted((p for p in packs
                   if not p.name.startswith(".") and not p.name.lower().endswith(".disabled")),
                  key=lambda p: p.name.lower())


def _site_packages(comfyui_dir: Path) -> list[Path]:
    """Package folders of the Python environment ComfyUI runs in."""
    found: list[Path] = []
    for base in dict.fromkeys((comfyui_dir.parent, comfyui_dir)):
        for name in PYTHON_FOLDERS:
            env = base / name
            if not env.is_dir():
                continue
            for site in [env / "Lib" / "site-packages", *sorted(env.glob("lib/python3*/site-packages"))]:
                if site.is_dir() and site not in found:
                    found.append(site)
    return found


def find_template_files(comfyui_dir: Path) -> TemplateFiles:
    """Every blueprint and template JSON file of this ComfyUI installation."""
    result = TemplateFiles()
    seen: set[str] = set()

    def add(label: str, path: Path) -> None:
        if label.lower() not in seen:  # the same template in two packages counts once
            seen.add(label.lower())
            result.files.append((label, path))

    packs = _node_packs(comfyui_dir)
    for path in _json_files(comfyui_dir / "blueprints"):
        add(f'blueprint "{path.stem}"', path)
    for pack in packs:
        for path in _json_files(pack / "subgraphs"):
            add(f'blueprint "{path.stem}" ({pack.name})', path)
    for site in _site_packages(comfyui_dir):
        for folder in sorted(site.glob("comfyui_workflow_templates*/templates")):
            files = _json_files(folder)
            if files:
                result.package_dirs.append(folder)
            for path in files:
                add(f'template "{path.stem}"', path)
    for pack in packs:
        for name in EXAMPLE_FOLDERS:
            for path in _json_files(pack / name):
                add(f'template "{path.stem}" ({pack.name})', path)
    return result
