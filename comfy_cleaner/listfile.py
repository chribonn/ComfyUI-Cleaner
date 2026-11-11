"""The editable review list, written by the finder and carried out by the deleter.

Format: plain text. Lines starting with '#' are comments. Every other line is
  * the full path of a model file or model folder to delete, or
  * "MOVE <from> -> <to>" to move a misplaced model to where the workflows expect it.
"""

from __future__ import annotations

import os
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import MISPLACED, USED, ModelStatus
from .templates import is_blueprint
from .util import human_size
from .verify import Verification
from .version import VERSION_TEXT

NOTICE_WIDTH = 74

MOVE_PREFIX = "MOVE "
MOVE_ARROW = " -> "


@dataclass
class ListEntry:
    model: ModelStatus
    verification: Verification
    delete: bool = False
    move: bool = False

    @property
    def unit(self):
        return self.model.unit


@dataclass
class Plan:
    deletes: list[str] = field(default_factory=list)
    moves: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def norm_path(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def section_of(entry: ListEntry) -> int:
    """0 = orphans, 1 = duplicates, 2 = misplaced."""
    if entry.model.is_duplicate:
        return 1
    return 2 if entry.model.status == MISPLACED else 0


SECTION_TITLES = (
    "Orphaned models - no workflow uses them",
    "Duplicates - the same name in more than one place",
    "Misplaced models - a workflow names them but looks in another folder",
)


def sort_entries(entries: list[ListEntry]) -> list[ListEntry]:
    return sorted(entries, key=lambda e: (section_of(e), e.model.dup_group, e.unit.rel.lower()))


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def template_counts(labels: set[str]) -> str:
    """'2 blueprints and 5 templates' for a set of template labels."""
    blueprints = sum(1 for label in labels if is_blueprint(label))
    parts = [plural(blueprints, "blueprint")] if blueprints else []
    if len(labels) > blueprints:
        parts.append(plural(len(labels) - blueprints, "template"))
    return " and ".join(parts)


def template_names(labels: set[str]) -> list[str]:
    """Blueprints first, then templates, each alphabetically."""
    return sorted(labels, key=lambda label: (not is_blueprint(label), label.lower()))


def status_text(model: ModelStatus) -> str:
    """Short description used in the list file and in the details view."""
    if model.status == USED:
        return f"used by {plural(len(model.users), 'workflow')}"
    if model.status == MISPLACED:
        return "misplaced, expected at " + ", ".join(sorted(model.expected))
    notes = []
    if model.other_copy_used:
        notes.append("the workflows that name it use another copy")
    elif model.mentions:
        notes.append(f"only named in notes/links of {plural(len(model.mentions), 'workflow')}")
    if model.template_model:
        verb = "loads" if len(model.templates) == 1 else "load"
        notes.append(f"template model: only {template_counts(model.templates)} {verb} it")
    return "orphan" + (f" ({'; '.join(notes)})" if notes else "")


def _template_line(model: ModelStatus) -> list[str]:
    """Comment naming the templates and blueprints that load this copy."""
    if not model.templates:
        return []
    names = template_names(model.templates)
    shown = ", ".join(names[:3]) + (f" (+{len(names) - 3} more)" if len(names) > 3 else "")
    return [f"#   {'moving it breaks' if model.status == MISPLACED else 'loaded by'}: {shown}"]


def _marked_subject(marked: int, total: int) -> str:
    if total == 1:
        return "It is"
    if marked == total:
        return "They are"
    return f"{marked} of them {'is' if marked == 1 else 'are'}"


def template_notice(entries: list[ListEntry], keep_templates: bool,
                    pointer: str = 'see "template model" below') -> str:
    """What the top of the list says about template models ('' if there are none)."""
    found = [e for e in entries if e.model.template_model]
    if not found:
        return ""
    total = len(found)
    marked = sum(1 for e in found if e.delete)
    them, are = ("it", "is") if total == 1 else ("them", "are")
    text = (f"ATTENTION: models outside your workflows were found. {plural(total, 'model')} "
            f"({human_size(sum(e.unit.size for e in found))}) {are} not used by any of your "
            f"workflows, only by ComfyUI's own templates or blueprints ({pointer}). ")
    if keep_templates:
        text += (f"--keep-template-models was used, so {'it is' if total == 1 else 'they are'} "
                 "not marked for deletion by default. ")
        if marked:
            text += f"{_marked_subject(marked, total)} marked by hand and will be deleted."
        else:
            text += "It will not be deleted." if total == 1 else "None of them will be deleted."
    elif marked:
        text += (f"{_marked_subject(marked, total)} marked for deletion"
                 f"{' by default' if marked == total else ''} and will be deleted. To keep {them} "
                 "by default, run find_unused_models.py with --keep-template-models.")
    else:
        text += "It is not marked for deletion." if total == 1 else "None of them is marked for deletion."
    return text


def _template_notice(entries: list[ListEntry], keep_templates: bool) -> list[str]:
    """Highlighted note for the top of the list when template models were found."""
    text = template_notice(entries, keep_templates)
    if not text:
        return []
    bar = "# !!! " + "=" * NOTICE_WIDTH
    return ["#", bar, *(f"# !!! {line}" for line in textwrap.wrap(text, NOTICE_WIDTH)), bar]


def _info_line(entry: ListEntry) -> str:
    model, check = entry.model, entry.verification
    info = [human_size(entry.unit.size), entry.unit.kind, status_text(model)]
    if model.templates and not model.template_model:
        info.append(f"also loaded here by {template_counts(model.templates)}")
    if check.label:
        info.append(check.label)
    if model.custom:
        info.append("custom node")
    if check.url:
        info.append(check.url)
    return "# " + " | ".join(info)


def write_list(path: Path, entries: list[ListEntry], models_dir: Path,
               keep_templates: bool = False) -> None:
    deletes = [e for e in entries if e.delete]
    moves = [e for e in entries if e.move]
    lines = [
        f"# {VERSION_TEXT} - review list",
        f"# Created {datetime.now():%Y-%m-%d %H:%M} for {models_dir}",
        *_template_notice(entries, keep_templates),
        "#",
        "# delete_unused_models.py carries out every line that does NOT start with '#':",
        "#   a path           the model is deleted",
        "#   MOVE a -> b      the model is moved from a to b",
        "# To skip a line, delete it or put '#' in front of it. To carry out a line",
        "# that starts with '#', remove the '#'.",
        "#",
        "# orphan          no workflow uses it. Only orphans verified online are marked",
        "#                 by default.",
        "# template model  an orphan that only ComfyUI's own templates or blueprints load.",
        "#                 Marked like other orphans unless --keep-template-models is used.",
        "# duplicate       the same name exists in more than one place. Choose which copies",
        "#                 to delete. If a workflow uses the file, one copy has to stay.",
        "# misplaced       a workflow names it but looks for it in another folder. It is not",
        "#                 deleted, but it can be moved to where the workflow expects it.",
        "# custom node     a custom node may load it without the workflow naming it - check",
        "#                 before deleting.",
        "#",
        f"# Marked: {len(deletes)} to delete ({human_size(sum(e.unit.size for e in deletes))}), "
        f"{len(moves)} to move",
    ]
    section = -1
    group = 0
    for entry in sort_entries(entries):
        model = entry.model
        if section_of(entry) != section:
            section = section_of(entry)
            lines += ["", "", f"# ===== {SECTION_TITLES[section]} ====="]
        if model.is_duplicate and model.dup_group != group:
            group = model.dup_group
            size_note = "same size" if model.dup_same_size else "DIFFERENT sizes - may not be the same model"
            lines += ["", f"# --- Duplicate {group}: {entry.unit.name} ({size_note})"]
        elif not model.is_duplicate:
            lines.append("")
        lines.append(_info_line(entry))
        lines += _template_line(model)
        if model.deletable:
            lines.append(("" if entry.delete else "# ") + str(entry.unit.path))
        if model.status == MISPLACED:
            if model.move_to:
                lines.append(("" if entry.move else "# ") + f"{MOVE_PREFIX}{entry.unit.path}"
                             f"{MOVE_ARROW}{model.move_to}")
            else:
                lines.append(f"#   {entry.unit.path}")
                lines.append(f"#   (not moved automatically: {model.move_note})")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_list(path: Path) -> Plan:
    plan = Plan()
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith(MOVE_PREFIX):
            parts = line[len(MOVE_PREFIX):].split(MOVE_ARROW)
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                plan.errors.append(f"line {number}: expected 'MOVE <from> -> <to>': {line}")
                continue
            plan.moves.append((parts[0].strip().strip('"'), parts[1].strip().strip('"')))
        else:
            plan.deletes.append(line.strip('"').strip())
    return plan

