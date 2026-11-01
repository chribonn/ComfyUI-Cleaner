"""Program 2: delete the models in the list written by find_unused_models.py.

Safety checks before anything is deleted:
  * only paths inside the configured models folder, never the folder itself or
    one of ComfyUI's model category folders (checkpoints, loras, ...)
  * only model files (by extension) or recognised model folders
  * links/junctions are skipped, so nothing outside the models folder is touched
  * the workflows are read again; a model that a workflow uses now is skipped
  * "always_keep" patterns from the config are honoured
  * you must type DELETE to confirm (use --dry-run to only preview)
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import DB_PATH, DEFAULT_CONFIG_PATH, DEFAULT_LIST_PATH, LOG_PATH, Settings, load_settings
from .database import ModelDatabase
from .listfile import norm_path, read_list
from .models import STANDARD_CATEGORIES, ModelUnit, folder_stats, is_model_folder, matches_keep
from .util import ask, human_size, setup_console
from .verify import unit_key_size
from .workflows import WorkflowRefs, scan_workflows


@dataclass
class Target:
    unit: ModelUnit
    url: str = ""


def _is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(isjunction and isjunction(path))


def check_target(raw: str, settings: Settings, refs: WorkflowRefs) -> tuple[ModelUnit | None, str]:
    path = Path(os.path.expandvars(raw))
    if not path.is_absolute():
        path = settings.models_dir / path
    path = Path(os.path.abspath(path))
    root = settings.models_dir
    try:
        rel = os.path.relpath(norm_path(path), norm_path(root))
    except ValueError:
        return None, "not inside the models folder"
    if rel == "." or rel.startswith("..") or os.path.isabs(rel):
        return None, "not inside the models folder"
    if not os.path.lexists(path):
        return None, "does not exist (already deleted?)"
    if _is_link(path):
        return None, "is a link/junction - remove it by hand if wanted"
    if Path(os.path.realpath(path)) != Path(os.path.realpath(root)) / path.relative_to(root):
        return None, "lies inside a linked folder"
    rel_posix = path.relative_to(root).as_posix()
    if any(part.startswith(".") for part in path.relative_to(root).parts):
        return None, "is inside a hidden/cache folder"
    if matches_keep(rel_posix, settings.keep_patterns):
        return None, 'protected by "always_keep" in the config'

    if path.is_file():
        if path.suffix.lower() not in settings.extensions:
            return None, "is not a model file"
        unit = ModelUnit(path, rel_posix, "file", path.name, path.stat().st_size)
    elif path.is_dir():
        if len(path.relative_to(root).parts) == 1 and path.name.lower() in STANDARD_CATEGORIES:
            return None, "is a ComfyUI model category folder"
        if not is_model_folder(path, root, settings.extensions):
            return None, "is not recognised as a model folder"
        size, weights = folder_stats(path, set(settings.extensions))
        unit = ModelUnit(path, rel_posix, "folder", path.name, size, weights)
    else:
        return None, "is not a regular file or folder"

    users = refs.references_to(unit.kind, unit.name)
    if users:
        first = sorted(users)[0]
        more = f" (+{len(users) - 1} more)" if len(users) > 1 else ""
        return None, f"is now used by workflow {first}{more}"
    return unit, ""


def _make_writable_and_retry(func, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def delete_unit(unit: ModelUnit) -> None:
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


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete the models listed by find_unused_models.py.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help="configuration file (default: %(default)s)")
    parser.add_argument("--list", type=Path, default=DEFAULT_LIST_PATH,
                        help="list of models to delete (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="only show what would be deleted")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = parse_args(argv)
    list_path = args.list.resolve()
    if not list_path.is_file():
        print(f"List not found: {list_path}\nRun find_unused_models.py first.")
        return 1
    settings = load_settings(args.config)
    paths = read_list(list_path)
    if not paths:
        print(f"{list_path} has no models marked for deletion.")
        return 0

    print(f"Re-reading the workflows in {settings.user_dir} to make sure the models are still unused ...")
    refs = scan_workflows(settings.user_dir, settings.extensions)
    if not refs.workflows:
        print("No workflow files were found - refusing to delete anything.")
        return 1

    targets: list[Target] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    db = ModelDatabase(DB_PATH) if DB_PATH.exists() else None
    try:
        for raw in paths:
            key = norm_path(raw)
            if key in seen:
                continue
            seen.add(key)
            unit, reason = check_target(raw, settings, refs)
            if unit is None:
                skipped.append((raw, reason))
                continue
            row = db.get_verified(unit.kind, unit.name, unit_key_size(unit)) if db else None
            targets.append(Target(unit, (row["url"] or "") if row else ""))
    finally:
        if db:
            db.close()

    if skipped:
        print(f"\nSkipped {len(skipped)} line(s):")
        for raw, reason in skipped:
            print(f"  {raw}\n      -> {reason}")
    if not targets:
        print("\nNothing to delete.")
        return 0

    total = sum(t.unit.size for t in targets)
    print(f"\n{'Would delete' if args.dry_run else 'Will delete'} {len(targets)} model(s), {human_size(total)}:")
    for target in targets:
        kind = " (folder)" if target.unit.kind == "folder" else ""
        print(f"  {human_size(target.unit.size):>10}  {target.unit.path}{kind}")
    if args.dry_run:
        print("\nDry run - nothing was deleted.")
        return 0

    print("\nDeleted files do NOT go to the Recycle Bin.")
    if ask(f"Type DELETE to permanently delete these {len(targets)} model(s): ") != "DELETE":
        print("Cancelled - nothing was deleted.")
        return 0

    freed = 0
    failures = 0
    with open(LOG_PATH, "a", encoding="utf-8") as log:
        for target in targets:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                delete_unit(target.unit)
            except OSError as exc:
                failures += 1
                print(f"  FAILED   {target.unit.path}: {exc}")
                log.write(f"{stamp}\tFAILED\t{target.unit.size}\t{target.unit.path}\t{exc}\n")
                continue
            freed += target.unit.size
            print(f"  deleted  {target.unit.path}")
            log.write(f"{stamp}\tDELETED\t{target.unit.size}\t{target.unit.path}\t{target.url}\n")

    print(f"\nFreed {human_size(freed)}. {failures} failure(s).")
    print(f"Log (with download links for re-downloading): {LOG_PATH}")
    return 1 if failures else 0
