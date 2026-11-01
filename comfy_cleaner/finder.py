"""Program 1: find models that no workflow uses and let the user edit the list."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from .config import DB_PATH, DEFAULT_CONFIG_PATH, DEFAULT_LIST_PATH, Settings, load_settings
from .database import ModelDatabase
from .listfile import ListEntry, norm_path, read_list, write_list
from .models import classify, inventory, is_custom_node_folder
from .util import ask, ask_yes_no, human_size, setup_console
from .verify import verify_units
from .workflows import scan_workflows

HELP = """Commands:
  1,3,5-8   switch the listed numbers between delete [x] and keep [ ]
  a         delete all          n   keep all
  v         delete only models verified online (the default)
  i 4       show details of model 4
  e         edit the list in a text editor
  l         show the list again
  Enter     save the list and finish
  q         quit without saving"""


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else "..." + text[-(width - 3):]


def print_table(entries: list[ListEntry]) -> None:
    columns = shutil.get_terminal_size((120, 30)).columns
    num_w = len(str(len(entries)))
    label_w = min(40, max(len(e.verification.label) for e in entries))
    path_w = max(30, columns - (num_w + 1 + 4 + 11 + 2 + 2 + label_w + 1))
    print()
    for number, entry in enumerate(entries, 1):
        unit = entry.unit
        mark = "[x]" if entry.selected else "[ ]"
        rel = unit.rel + ("/" if unit.kind == "folder" else "")
        flag = "!" if is_custom_node_folder(unit) else " "
        print(f"{number:>{num_w}} {mark} {human_size(unit.size):>10}  "
              f"{_fit(rel, path_w):<{path_w}}{flag} {_fit(entry.verification.label, label_w)}")
    chosen = [e for e in entries if e.selected]
    print(f"\n[x] = will be deleted: {len(chosen)} of {len(entries)} models, "
          f"{human_size(sum(e.unit.size for e in chosen))} of "
          f"{human_size(sum(e.unit.size for e in entries))}")
    if any(is_custom_node_folder(e.unit) for e in entries):
        print("!   = custom node model folder. Custom nodes often load these by themselves "
              "without naming them in the workflow, so check before deleting.")


def parse_numbers(text: str, count: int) -> set[int]:
    chosen: set[int] = set()
    for part in text.replace(" ", ",").split(","):
        if not part:
            continue
        if "-" in part:
            first, last = part.split("-", 1)
            start, end = int(first), int(last)
            if start > end:
                start, end = end, start
            numbers = range(start, end + 1)
        else:
            numbers = [int(part)]
        for number in numbers:
            if not 1 <= number <= count:
                raise ValueError(f"{number} is not between 1 and {count}")
            chosen.add(number - 1)
    if not chosen:
        raise ValueError("no numbers given")
    return chosen


def show_details(entry: ListEntry) -> None:
    unit, check = entry.unit, entry.verification
    print(f"\n  Path:     {unit.path}")
    print(f"  Size:     {human_size(unit.size)} ({unit.kind})")
    print(f"  Online:   {check.label}{'  (from local database)' if check.cached else ''}")
    if check.url:
        print(f"  URL:      {check.url}")
    if check.note:
        print(f"  Note:     {check.note}")
    if unit.kind == "folder" and unit.weight_files:
        print(f"  Weights:  {len(unit.weight_files)} file(s), e.g. {unit.weight_files[0][0]}")
    if is_custom_node_folder(unit):
        print("  Warning:  custom node model folder - the node may use it without naming it.")
    print(f"  Status:   {'will be deleted' if entry.selected else 'kept'}")


def open_in_editor(path: Path) -> None:
    editor = os.environ.get("EDITOR")
    if os.name == "nt" and not editor:
        # Notepad may hand the file to an already open window and return at once,
        # so wait for the user instead of the process.
        subprocess.Popen(["notepad.exe", str(path)])
        ask("Edit the list in Notepad, save it (Ctrl+S), then press Enter here to continue... ")
        return
    command = shlex.split(editor, posix=os.name != "nt") if editor else ["nano"]
    try:
        subprocess.run([*command, str(path)], check=False)
    except OSError as exc:
        print(f"Could not start the editor ({exc}). Edit {path} yourself, then press Enter.")
        ask("")


def edit_in_editor(entries: list[ListEntry], list_path: Path, models_dir: Path) -> None:
    write_list(list_path, entries, models_dir)
    open_in_editor(list_path)
    try:
        chosen = {norm_path(p) for p in read_list(list_path)}
    except OSError as exc:
        print(f"Could not read the edited list: {exc}")
        return
    known = {norm_path(e.unit.path) for e in entries}
    for entry in entries:
        entry.selected = norm_path(entry.unit.path) in chosen
    unknown = chosen - known
    if unknown:
        print(f"\n{len(unknown)} line(s) in the file are not unused models from this scan and "
              "were ignored (only models found by the scan can be listed):")
        for item in sorted(unknown)[:10]:
            print(f"  {item}")


def review(entries: list[ListEntry], list_path: Path, settings: Settings) -> bool:
    """Interactive editing. Returns False if the user quits without saving."""
    print_table(entries)
    print("\n" + HELP)
    while True:
        command = ask("\nCommand (h = help, Enter = save): ")
        lower = command.lower()
        if lower == "":
            return True
        if lower in ("q", "quit", "exit"):
            return False
        if lower in ("h", "help", "?"):
            print(HELP)
            continue
        if lower in ("l", "list"):
            print_table(entries)
            continue
        if lower in ("a", "n", "v"):
            for entry in entries:
                entry.selected = {"a": True, "n": False}.get(lower, entry.verification.verified)
            print_table(entries)
            continue
        if lower == "e":
            edit_in_editor(entries, list_path, settings.models_dir)
            print_table(entries)
            continue
        if lower.startswith("i"):
            try:
                for index in sorted(parse_numbers(lower[1:].strip(), len(entries))):
                    show_details(entries[index])
            except ValueError as exc:
                print(f"  {exc}. Example: i 4")
            continue
        try:
            indexes = parse_numbers(lower, len(entries))
        except ValueError as exc:
            print(f"  Not understood ({exc}). Type h for help.")
            continue
        for index in indexes:
            entries[index].selected = not entries[index].selected
        print_table(entries)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List ComfyUI models that are not used by any workflow, so they can be deleted "
                    "with delete_unused_models.py.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help="configuration file (default: %(default)s)")
    parser.add_argument("--list", type=Path, default=DEFAULT_LIST_PATH,
                        help="where to save the list (default: %(default)s)")
    parser.add_argument("--reconfigure", action="store_true",
                        help="ask for the ComfyUI installation folder again")
    parser.add_argument("--offline", action="store_true",
                        help="do not go online; use only what the local database already knows")
    parser.add_argument("--recheck", action="store_true",
                        help="look up models online again that were not found before")
    parser.add_argument("--show-used", action="store_true",
                        help="also print the models that are used and by which workflows")
    parser.add_argument("--no-edit", action="store_true",
                        help="save the list without the interactive review")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = parse_args(argv)
    settings = load_settings(args.config, reconfigure=args.reconfigure)
    list_path = args.list.resolve()
    print(f"\nWorkflows: {settings.user_dir}")
    print(f"Models:    {settings.models_dir}")

    refs = scan_workflows(settings.user_dir, settings.extensions)
    print(f"\nRead {len(refs.workflows)} workflow file(s) "
          f"({refs.ignored_json} other JSON file(s) ignored).")
    for rel, reason in refs.skipped:
        print(f"  Skipped {rel}: {reason}")
    if not refs.workflows:
        print("No workflows were found, so every model would be listed as unused.")
        if args.no_edit or not ask_yes_no("Continue anyway?"):
            return 1

    units, warnings = inventory(settings.models_dir, settings.extensions)
    for warning in warnings:
        print(f"  {warning}")
    result = classify(units, refs, settings.keep_patterns)
    used_size = sum(u.size for u, _ in result.used)
    unused_size = sum(u.size for u in result.unused)
    print(f"Found {len(units)} model(s), {human_size(sum(u.size for u in units))}:")
    print(f"  used by a workflow: {len(result.used):4d}  {human_size(used_size):>10}")
    print(f"  not used:           {len(result.unused):4d}  {human_size(unused_size):>10}")
    if result.kept:
        print(f"  always kept:        {len(result.kept):4d}  (\"always_keep\" in the config)")

    if args.show_used and result.used:
        print("\nModels used by workflows:")
        for unit, users in sorted(result.used, key=lambda item: item[0].rel.lower()):
            names = sorted(users)
            more = f" (+{len(names) - 2} more)" if len(names) > 2 else ""
            print(f"  {unit.rel}\n      <- {', '.join(names[:2])}{more}")

    verify_enabled = bool(settings.config.get("verify_online", True))
    with ModelDatabase(DB_PATH) as db:
        checks = verify_units(result.unused, db, settings,
                              online=verify_enabled and not args.offline, recheck=args.recheck)
    entries = [ListEntry(unit, checks[unit.rel], checks[unit.rel].verified or not verify_enabled)
               for unit in sorted(result.unused, key=lambda u: u.rel.lower())]

    if not entries:
        print("\nNo unused models found - nothing to delete.")
    elif not args.no_edit:
        if not review(entries, list_path, settings):
            print("Quit without saving. The previous list (if any) was not changed.")
            return 0
    write_list(list_path, entries, settings.models_dir)

    chosen = [e for e in entries if e.selected]
    print(f"\nSaved the list to {list_path}")
    print(f"{len(chosen)} model(s) marked for deletion, {human_size(sum(e.unit.size for e in chosen))}.")
    if chosen:
        print("To delete them run:  python delete_unused_models.py   (add --dry-run to preview)")
    return 0
