"""Program 2: carry out the list written by find_unused_models.py.

The workflows and the models folder are read again and every line is checked
against the current state before anything happens:
  * only paths inside the configured models folder; never the folder itself or
    one of ComfyUI's model category folders (checkpoints, loras, ...)
  * only models the scan finds (model files by extension, or model folders)
  * links/junctions are skipped, so nothing outside the models folder is touched
  * "always_keep" patterns from the config are honoured
  * deleted: orphans, and duplicates as long as one copy of a file that
    workflows use stays. Used and misplaced models are never deleted otherwise.
  * moved: only misplaced models, only to where the workflows expect them, and
    never over an existing file
  * moves need a "y", deletions need you to type DELETE (--dry-run only previews)
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import DB_PATH, DEFAULT_CONFIG_PATH, DEFAULT_LIST_PATH, LOG_PATH, Settings, load_settings
from .database import ModelDatabase
from .listfile import norm_path, plural, read_list
from .models import MISPLACED, ORPHAN, USED, Classification, ModelStatus, classify, inventory
from .util import ask, ask_yes_no, human_size, setup_console
from .verify import unit_key_size
from .workflows import scan_workflows


@dataclass
class Delete:
    model: ModelStatus
    url: str = ""
    warning: str = ""


@dataclass
class Move:
    model: ModelStatus
    target: Path


@dataclass
class Checked:
    deletes: list[Delete] = field(default_factory=list)
    moves: list[Move] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def _is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(isjunction and isjunction(path))


def _inside(path: Path, root: Path) -> bool:
    try:
        rel = os.path.relpath(norm_path(path), norm_path(root))
    except ValueError:
        return False
    return rel != "." and not rel.startswith("..") and not os.path.isabs(rel)


def locate(raw: str, settings: Settings) -> tuple[Path | None, str]:
    """Absolute path for a list entry, or None and the reason it is refused."""
    path = Path(os.path.expandvars(raw))
    if not path.is_absolute():
        path = settings.models_dir / path
    path = Path(os.path.abspath(path))
    root = settings.models_dir
    if not _inside(path, root):
        return None, "not inside the models folder"
    if not os.path.lexists(path):
        return None, "does not exist (already deleted or moved?)"
    if _is_link(path):
        return None, "is a link/junction - remove it by hand if wanted"
    rel = Path(os.path.relpath(path, root))
    if norm_path(os.path.realpath(path)) != norm_path(os.path.join(os.path.realpath(root), rel)):
        return None, "lies inside a linked folder"
    if any(part.startswith(".") for part in rel.parts):
        return None, "is inside a hidden/cache folder"
    return path, ""


def check_plan(plan, settings: Settings, result: Classification, db: ModelDatabase | None) -> Checked:
    checked = Checked()
    models = {norm_path(m.unit.path): m for m in result.statuses}
    kept = {norm_path(u.path) for u in result.kept}

    def find(raw: str) -> tuple[ModelStatus | None, str]:
        path, reason = locate(raw, settings)
        if path is None:
            return None, reason
        key = norm_path(path)
        if key in kept:
            return None, 'protected by "always_keep" in the config'
        if key not in models:
            return None, "is not a model file or model folder found by the scan"
        return models[key], ""

    delete_keys: set[str] = set()
    for raw in dict.fromkeys(plan.deletes):
        model, reason = find(raw)
        if model is None:
            checked.skipped.append((raw, reason))
        elif norm_path(model.unit.path) in delete_keys:
            continue
        elif model.status == ORPHAN or model.is_duplicate:
            delete_keys.add(norm_path(model.unit.path))
            warning = ""
            if model.status == USED:
                first = sorted(model.users)[0]
                warning = (f"used by {plural(len(model.users), 'workflow')} ({first}"
                           f"{' ...' if len(model.users) > 1 else ''}) - point them at the other copy")
            elif model.status == MISPLACED:
                warning = "a workflow looks for this file in another folder"
            row = db.get_verified(model.unit.kind, model.unit.name, unit_key_size(model.unit)) if db else None
            checked.deletes.append(Delete(model, (row["url"] or "") if row else "", warning))
        elif model.status == MISPLACED:
            checked.skipped.append((raw, "is misplaced (a workflow looks for it in another folder) - "
                                         "it can only be moved"))
        else:
            first = sorted(model.users)[0]
            checked.skipped.append((raw, f"is used by workflow {first}"
                                         f"{f' (+{len(model.users) - 1} more)' if len(model.users) > 1 else ''}"))

    # One copy of a file that workflows use has to stay.
    groups = result.duplicate_groups
    for number, members in groups.items():
        if any(m.needed for m in members) and all(norm_path(m.unit.path) in delete_keys for m in members):
            for delete in [d for d in checked.deletes if d.model.dup_group == number]:
                checked.deletes.remove(delete)
                checked.skipped.append((str(delete.model.unit.path),
                                        "every copy of this file is listed, but workflows use it - "
                                        "keep at least one"))

    targets: set[str] = set()
    for source, target in dict.fromkeys(plan.moves):
        model, reason = find(source)
        label = f"MOVE {source}"
        if model is None:
            checked.skipped.append((label, reason))
            continue
        if model.status != MISPLACED or model.move_to is None:
            checked.skipped.append((label, "is no longer misplaced, or cannot be moved automatically"
                                    + (f" ({model.move_note})" if model.move_note else "")))
            continue
        target_path = Path(os.path.abspath(os.path.expandvars(target)))
        if norm_path(target_path) != norm_path(model.move_to):
            checked.skipped.append((label, f"the workflows expect it at {model.move_to}"))
        elif not _inside(target_path, settings.models_dir) or os.path.lexists(target_path):
            checked.skipped.append((label, "the destination already exists or is outside the models folder"))
        elif norm_path(model.unit.path) in delete_keys or norm_path(target_path) in targets:
            checked.skipped.append((label, "is also listed for deletion, or another model has the "
                                           "same destination"))
        else:
            targets.add(norm_path(target_path))
            checked.moves.append(Move(model, target_path))
    return checked


def _make_writable_and_retry(func, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def delete_model(model: ModelStatus) -> None:
    unit = model.unit
    if unit.kind == "file":
        try:
            os.remove(unit.path)
        except PermissionError:
            os.chmod(unit.path, stat.S_IWRITE)
            os.remove(unit.path)
    elif sys.version_info >= (3, 12):
        shutil.rmtree(unit.path, onexc=_make_writable_and_retry)
    else:
        shutil.rmtree(unit.path, onerror=_make_writable_and_retry)


def move_model(move: Move) -> None:
    if os.path.lexists(move.target):
        raise FileExistsError(f"{move.target} already exists")
    move.target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(move.model.unit.path, move.target)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete and move the models listed by find_unused_models.py.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help="configuration file (default: %(default)s)")
    parser.add_argument("--list", type=Path, default=DEFAULT_LIST_PATH,
                        help="list to carry out (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="only show what would be deleted and moved")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = parse_args(argv)
    list_path = args.list.resolve()
    if not list_path.is_file():
        print(f"List not found: {list_path}\nRun find_unused_models.py first.")
        return 1
    settings = load_settings(args.config)
    plan = read_list(list_path)
    for error in plan.errors:
        print(f"  Ignored {error}")
    if not plan.deletes and not plan.moves:
        print(f"{list_path} has no models marked for deletion or moving.")
        return 0

    print(f"Re-reading the workflows in {settings.user_dir} and the models folder to check the list ...")
    refs = scan_workflows(settings.user_dir, settings.extensions)
    if not refs.workflows:
        print("No workflow files were found - refusing to change anything.")
        return 1
    units, warnings = inventory(settings.models_dir, settings.extensions)
    for warning in warnings:
        print(f"  {warning}")
    result = classify(units, refs, settings.keep_patterns, settings.models_dir)
    db = ModelDatabase(DB_PATH) if DB_PATH.exists() else None
    try:
        checked = check_plan(plan, settings, result, db)
    finally:
        if db:
            db.close()

    if checked.skipped:
        print(f"\nSkipped {plural(len(checked.skipped), 'line')}:")
        for raw, reason in checked.skipped:
            print(f"  {raw}\n      -> {reason}")
    if not checked.deletes and not checked.moves:
        print("\nNothing to do.")
        return 0

    would = "Would" if args.dry_run else "Will"
    if checked.moves:
        print(f"\n{would} move {plural(len(checked.moves), 'model')}:")
        for move in checked.moves:
            print(f"  {move.model.unit.path}\n      -> {move.target}")
    if checked.deletes:
        total = sum(d.model.unit.size for d in checked.deletes)
        print(f"\n{would} delete {plural(len(checked.deletes), 'model')}, {human_size(total)}:")
        for delete in checked.deletes:
            kind = " (folder)" if delete.model.unit.kind == "folder" else ""
            print(f"  {human_size(delete.model.unit.size):>10}  {delete.model.unit.path}{kind}")
            if delete.warning:
                print(f"              ! {delete.warning}")
    if args.dry_run:
        print("\nDry run - nothing was changed.")
        return 0

    failures = 0
    freed = 0
    with open(LOG_PATH, "a", encoding="utf-8") as log:
        def write_log(action: str, model: ModelStatus, detail: str) -> None:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log.write(f"{stamp}\t{action}\t{model.unit.size}\t{model.unit.path}\t{detail}\n")

        if checked.moves and ask_yes_no(f"\nMove these {plural(len(checked.moves), 'model')}?"):
            for move in checked.moves:
                try:
                    move_model(move)
                except OSError as exc:
                    failures += 1
                    print(f"  FAILED   {move.model.unit.path}: {exc}")
                    write_log("MOVE FAILED", move.model, str(exc))
                    continue
                print(f"  moved    {move.model.unit.path}\n        -> {move.target}")
                write_log("MOVED", move.model, str(move.target))
        elif checked.moves:
            print("Nothing was moved.")

        if checked.deletes:
            print("\nDeleted files do NOT go to the Recycle Bin.")
            answer = ask(f"Type DELETE to permanently delete these "
                         f"{plural(len(checked.deletes), 'model')}: ")
            if answer != "DELETE":
                print("Nothing was deleted.")
            else:
                for delete in checked.deletes:
                    try:
                        delete_model(delete.model)
                    except OSError as exc:
                        failures += 1
                        print(f"  FAILED   {delete.model.unit.path}: {exc}")
                        write_log("FAILED", delete.model, str(exc))
                        continue
                    freed += delete.model.unit.size
                    print(f"  deleted  {delete.model.unit.path}")
                    write_log("DELETED", delete.model, delete.url)

    print(f"\nFreed {human_size(freed)}. {failures} failure(s).")
    print(f"Log (with download links for re-downloading): {LOG_PATH}")
    return 1 if failures else 0
