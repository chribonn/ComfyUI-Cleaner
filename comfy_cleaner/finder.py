"""Program 1: compare models with workflows and let the user review the result.

Models a workflow loads from exactly their place are left alone. The review
list shows orphaned models (no workflow uses them), duplicates (same name in
more than one place) and misplaced models (a workflow looks for them in another
folder), and lets the user choose what to delete or move.

ComfyUI's own templates and blueprints are read too. They never make a model
used, but orphans they load are flagged as template models.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path

from .config import DB_PATH, DEFAULT_CONFIG_PATH, DEFAULT_LIST_PATH, Settings, load_settings
from .database import ModelDatabase
from .listfile import (SECTION_TITLES, ListEntry, norm_path, plural, read_list, section_of,
                       sort_entries, status_text, template_counts, template_names,
                       template_notice, write_list)
from .models import MISPLACED, ORPHAN, USED, ModelStatus, add_template_users, classify, inventory
from .templates import find_template_files, is_blueprint
from .util import ask, ask_yes_no, human_size, setup_console
from .verify import Verification, verify_units
from .version import VERSION_TEXT
from .workflows import scan_files, scan_workflows

HELP = """Commands:
  1,3,5-8   switch the listed numbers between delete [x] and keep [ ]
  m 4       switch misplaced model 4 between move [m] and keep [ ]
  a         delete all orphans        n   keep everything (also clears moves)
  v         back to the default: delete {default}
  i 4       show details of model 4
  e         edit the list in a text editor
  l         show the list again
  Enter     save the list and finish
  q         quit without saving"""


def help_text(keep_templates: bool) -> str:
    default = "orphans verified online" + (", except template models" if keep_templates else "")
    return HELP.format(default=default)


def _fit(text: str, width: int) -> str:
    """Shorten from the left, so the file name stays visible."""
    return text if len(text) <= width else "..." + text[-(width - 3):]


def _fit_end(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 3] + "..."


def short_status(model: ModelStatus) -> str:
    text = {USED: "used", MISPLACED: "misplaced", ORPHAN: "orphan"}[model.status]
    if model.is_duplicate:
        text += f" dup{model.dup_group}" + ("" if model.dup_same_size else "*")
    return text


def info_text(entry: ListEntry) -> str:
    if entry.model.status == MISPLACED:
        folders = sorted({path.rsplit("/", 1)[0] + "/" for path in entry.model.expected})
        return "wants it in " + ", ".join(folders)
    return entry.verification.label


def mark(entry: ListEntry) -> str:
    return "[x]" if entry.delete else "[m]" if entry.move else "[ ]"


def print_table(entries: list[ListEntry], keep_templates: bool = False) -> None:
    columns = shutil.get_terminal_size((120, 30)).columns
    num_w = len(str(len(entries)))
    status_w = max(len(short_status(e.model)) for e in entries)
    info_w = min(45, max(len(info_text(e)) for e in entries))
    longest = max(len(e.unit.rel) + 1 for e in entries)
    path_w = min(longest, max(30, columns - (num_w + 1 + 4 + 11 + 2 + 2 + status_w + 1 + info_w + 1)))
    section = -1
    for number, entry in enumerate(entries, 1):
        if section_of(entry) != section:
            section = section_of(entry)
            print(f"\n{SECTION_TITLES[section]}:")
        unit = entry.unit
        rel = unit.rel + ("/" if unit.kind == "folder" else "")
        flag = "!" if entry.model.custom else "T" if entry.model.template_model else " "
        print(f"{number:>{num_w}} {mark(entry)} {human_size(unit.size):>10}  "
              f"{_fit(rel, path_w):<{path_w}}{flag} {short_status(entry.model):<{status_w}} "
              f"{_fit_end(info_text(entry), info_w)}")
    deletes = [e for e in entries if e.delete]
    moves = [e for e in entries if e.move]
    print(f"\n[x] delete: {len(deletes)} model(s), {human_size(sum(e.unit.size for e in deletes))}"
          f"   [m] move: {len(moves)}   [ ] keep")
    if any(e.model.is_duplicate for e in entries):
        print("dupN = same name as the other dupN entries (* = different size, maybe not the same model)")
    if any(e.model.custom for e in entries):
        print("!    = custom node model folder. Custom nodes often load these by themselves "
              "without naming them in the workflow, so check before deleting.")
    if any(e.model.template_model and not e.model.custom for e in entries):
        print("T    = template model: only ComfyUI's own templates or blueprints load it, none of your "
              "workflows. " + ("Not marked by default (--keep-template-models)." if keep_templates
                               else "Marked like other orphans; --keep-template-models keeps them."))


def parse_numbers(text: str, count: int) -> set[int]:
    chosen: set[int] = set()
    for part in text.replace(" ", ",").split(","):
        if not part:
            continue
        if not part.replace("-", "").isdigit() or part.count("-") > 1:
            raise ValueError(f"'{part}' is not a number or range")
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


# ------------------------------------------------------------------ selection rules
def default_delete(entry: ListEntry, verify_enabled: bool, keep_templates: bool = False) -> bool:
    model = entry.model
    if keep_templates and model.template_model:
        return False
    return (model.status == ORPHAN and not model.custom
            and (entry.verification.verified or not verify_enabled))


def group_of(entries: list[ListEntry], entry: ListEntry) -> list[ListEntry]:
    return [e for e in entries if e.model.dup_group == entry.model.dup_group]


def delete_problem(entries: list[ListEntry], entry: ListEntry) -> str | None:
    """Why `entry` may not be marked for deletion, or None."""
    model = entry.model
    if not model.deletable:
        return "is misplaced: a workflow looks for it in another folder. It can be moved, not deleted."
    if model.is_duplicate:
        group = group_of(entries, entry)
        if any(e.model.needed for e in group) and all(e.delete or e is entry for e in group):
            return (f"is the last copy of {entry.unit.name} that is kept. Workflows use this file, "
                    "so one copy has to stay.")
    return None


def move_problem(entries: list[ListEntry], entry: ListEntry) -> str | None:
    model = entry.model
    if model.status != MISPLACED:
        return "is not misplaced, so there is nothing to move."
    if not model.move_to:
        return f"cannot be moved automatically: {model.move_note}."
    target = norm_path(model.move_to)
    if any(e.move and e is not entry and norm_path(e.model.move_to) == target for e in entries):
        return "has the same destination as another model marked for moving."
    return None


def warn_if_used(entry: ListEntry) -> None:
    if entry.delete and entry.model.status == USED:
        names = sorted(entry.model.users)
        print(f"  Note: {plural(len(names), 'workflow')} load this copy "
              f"({names[0]}{' ...' if len(names) > 1 else ''}). "
              "Point them at the other copy after deleting it.")


def warn_if_template_move(entry: ListEntry) -> None:
    templates = entry.model.templates
    if entry.move and templates:
        names = template_names(templates)
        one = len(names) == 1
        print(f"  Note: {template_counts(templates)} of ComfyUI {'loads' if one else 'load'} it from "
              f"where it is now ({names[0]}{' ...' if not one else ''}). "
              f"Moving the model breaks {'it' if one else 'them'}.")


def show_details(entries: list[ListEntry], entry: ListEntry) -> None:
    unit, model, check = entry.unit, entry.model, entry.verification
    print(f"\n  Path:      {unit.path}")
    print(f"  Size:      {human_size(unit.size)} ({unit.kind})")
    print(f"  Status:    {status_text(model)}")
    for workflow in sorted(model.users)[:5]:
        print(f"             used by {workflow}")
    if len(model.users) > 5:
        print(f"             ... and {len(model.users) - 5} more")
    for path, workflows in sorted(model.expected.items()):
        names = sorted(workflows)
        print(f"  Expected:  {path}\n             by {names[0]}"
              f"{f' (+{len(names) - 1} more)' if len(names) > 1 else ''}")
    if model.status == MISPLACED:
        print(f"  Move to:   {model.move_to}" if model.move_to else f"  Move:      not offered, {model.move_note}")
    if model.status == ORPHAN and model.mentions:
        print(f"  Named in:  {sorted(model.mentions)[0]}"
              f"{f' (+{len(model.mentions) - 1} more)' if len(model.mentions) > 1 else ''}")
    if model.templates:
        names = template_names(model.templates)
        one = len(names) == 1
        counts, verb = template_counts(model.templates), "loads" if one else "load"
        if model.template_model:
            print(f"  Templates: only ComfyUI's own {counts} {verb} it, none of your workflows:")
        else:
            print(f"  Templates: {counts} of ComfyUI also {verb} it from where it is now:")
        for name in names[:8]:
            print(f"             {name}")
        if len(names) > 8:
            print(f"             ... and {len(names) - 8} more")
        if model.status == MISPLACED:
            print(f"             Moving the model breaks {'it' if one else 'them'}.")
    if model.is_duplicate:
        print(f"  Copies:    duplicate {model.dup_group}"
              f"{'' if model.dup_same_size else ' (sizes differ)'}")
        for other in group_of(entries, entry):
            if other is not entry:
                print(f"             {other.unit.rel} - {human_size(other.unit.size)}, "
                      f"{short_status(other.model)}, {mark(other)}")
    if check.label:
        print(f"  Online:    {check.label}{'  (from local database)' if check.cached else ''}")
    if check.url:
        print(f"  URL:       {check.url}")
    if check.note:
        print(f"  Note:      {check.note}")
    if unit.kind == "folder" and unit.weight_files:
        print(f"  Weights:   {len(unit.weight_files)} file(s), e.g. {unit.weight_files[0][0]}")
    if model.custom:
        print("  Warning:   custom node model folder - the node may use it without naming it.")
    print(f"  Marked:    {'delete' if entry.delete else 'move' if entry.move else 'keep'}")


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


def apply_edited_list(entries: list[ListEntry], list_path: Path) -> None:
    try:
        plan = read_list(list_path)
    except OSError as exc:
        print(f"Could not read the edited list: {exc}")
        return
    by_path = {norm_path(e.unit.path): e for e in entries}
    for entry in entries:
        entry.delete = entry.move = False
    ignored: list[str] = list(plan.errors)
    for raw in plan.deletes:
        entry = by_path.get(norm_path(raw))
        if entry is None:
            ignored.append(f"{raw}: not a model from this scan")
        elif not entry.model.deletable:
            ignored.append(f"{raw}: misplaced models can only be moved")
        else:
            entry.delete = True
    for source, target in plan.moves:
        entry = by_path.get(norm_path(source))
        if entry is None or not entry.model.move_to:
            ignored.append(f"MOVE {source}: not a misplaced model that can be moved")
        elif norm_path(target) != norm_path(entry.model.move_to):
            ignored.append(f"MOVE {source}: can only be moved to {entry.model.move_to}")
        elif entry.delete:
            ignored.append(f"MOVE {source}: also marked for deletion - kept instead")
            entry.delete = False
        else:
            entry.move = True
    for entry in entries:
        if entry.delete and entry.model.is_duplicate:
            group = group_of(entries, entry)
            if any(e.model.needed for e in group) and all(e.delete for e in group):
                for other in group:
                    other.delete = False
                ignored.append(f"{entry.unit.name}: every copy was marked, but workflows use it - "
                               "all copies kept")
    if ignored:
        print(f"\n{plural(len(ignored), 'line')} in the file could not be used:")
        for item in ignored[:15]:
            print(f"  {item}")


def review(entries: list[ListEntry], list_path: Path, settings: Settings, verify_enabled: bool,
           keep_templates: bool = False) -> bool:
    """Interactive editing. Returns False if the user quits without saving."""
    print_table(entries, keep_templates)
    print("\n" + help_text(keep_templates))
    while True:
        command = ask("\nCommand (h = help, Enter = save): ")
        lower = command.lower()
        if lower == "":
            return True
        if lower in ("q", "quit", "exit"):
            return False
        if lower in ("h", "help", "?"):
            print(help_text(keep_templates))
            continue
        if lower in ("l", "list"):
            print_table(entries, keep_templates)
            continue
        if lower in ("a", "v", "n"):
            for entry in entries:
                if lower == "n":
                    entry.delete = entry.move = False
                elif lower == "a":
                    entry.delete = entry.model.status == ORPHAN
                else:
                    entry.delete = default_delete(entry, verify_enabled, keep_templates)
            print_table(entries, keep_templates)
            continue
        if lower == "e":
            write_list(list_path, entries, settings.models_dir, keep_templates)
            open_in_editor(list_path)
            apply_edited_list(entries, list_path)
            print_table(entries, keep_templates)
            continue
        if lower[:1] in ("i", "m"):
            try:
                indexes = sorted(parse_numbers(lower[1:].strip(), len(entries)))
            except ValueError as exc:
                print(f"  {exc}. Example: {lower[:1]} 4")
                continue
            for index in indexes:
                entry = entries[index]
                if lower[0] == "i":
                    show_details(entries, entry)
                elif entry.move:
                    entry.move = False
                elif problem := move_problem(entries, entry):
                    print(f"  {index + 1} {problem}")
                else:
                    entry.move, entry.delete = True, False
                    warn_if_template_move(entry)
            if lower[0] == "m":
                print_table(entries, keep_templates)
            continue
        try:
            indexes = parse_numbers(lower, len(entries))
        except ValueError as exc:
            print(f"  Not understood ({exc}). Type h for help.")
            continue
        for index in sorted(indexes):
            entry = entries[index]
            if entry.delete:
                entry.delete = False
            elif problem := delete_problem(entries, entry):
                print(f"  {index + 1} {problem}")
            else:
                entry.delete, entry.move = True, False
                warn_if_used(entry)
        print_table(entries, keep_templates)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"{VERSION_TEXT}. Compare ComfyUI models with the workflows and list orphaned, "
                    "duplicate and misplaced models, so they can be deleted or moved with "
                    "delete_unused_models.py.")
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
    parser.add_argument("--keep-template-models", action="store_true",
                        help="do not mark template models (orphans that only ComfyUI's own templates "
                             "or blueprints load) for deletion by default")
    parser.add_argument("--version", action="version", version=VERSION_TEXT,
                        help="show the version and exit")
    return parser.parse_args(argv)


def print_summary(result, units) -> None:
    def line(label: str, models: list[ModelStatus], note: str = "") -> None:
        size = human_size(sum(m.unit.size for m in models))
        print(f"  {label:<20}{len(models):4d}  {size:>10}{note}")

    print(f"Found {len(units)} model(s), {human_size(sum(u.size for u in units))}:")
    line("used by a workflow:", result.with_status(USED))
    line("misplaced:", result.with_status(MISPLACED), "  (a workflow looks for them in another folder)")
    line("orphaned:", result.with_status(ORPHAN), "  (no workflow uses them)")
    template_models = [m for m in result.statuses if m.template_model]
    if template_models:
        line("  template models:", template_models,
             "  (only ComfyUI's own templates or blueprints load them)")
    groups = result.duplicate_groups
    if groups:
        copies = [m for members in groups.values() for m in members]
        line("duplicates:", copies, f"  ({plural(len(groups), 'name')} in more than one place, "
                                    "also counted above)")
    if result.kept:
        print(f"  {'always kept:':<20}{len(result.kept):4d}  (\"always_keep\" in the config)")


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = parse_args(argv)
    print(f"{VERSION_TEXT} - find unused models")
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
        print("No workflows were found, so every model would be listed as orphaned.")
        if args.no_edit or not ask_yes_no("Continue anyway?"):
            return 1

    templates = find_template_files(settings.comfyui_dir)
    template_refs = scan_files(templates.files, settings.extensions)
    blueprints = sum(1 for label in template_refs.workflows if is_blueprint(label))
    print(f"Read {plural(blueprints, 'blueprint')} and "
          f"{plural(len(template_refs.workflows) - blueprints, 'template')} that come with ComfyUI "
          "and its custom nodes. They do not count as use.")
    if template_refs.skipped:
        print(f"  {plural(len(template_refs.skipped), 'template file')} could not be read.")
    if not templates.package_dirs:
        print("  ComfyUI's Templates browser files were not found (no comfyui_workflow_templates "
              f"package in a Python environment next to {settings.comfyui_dir}).")

    units, warnings = inventory(settings.models_dir, settings.extensions)
    for warning in warnings:
        print(f"  {warning}")
    result = classify(units, refs, settings.keep_patterns, settings.models_dir)
    add_template_users(result, template_refs)
    print_summary(result, units)

    if args.show_used:
        used = sorted(result.with_status(USED), key=lambda m: m.unit.rel.lower())
        if used:
            print("\nModels used by workflows:")
        for model in used:
            names = sorted(model.users)
            more = f" (+{len(names) - 2} more)" if len(names) > 2 else ""
            print(f"  {model.unit.rel}\n      <- {', '.join(names[:2])}{more}")
            for path, workflows in sorted(model.expected.items()):
                print(f"      also expected at {path} by {sorted(workflows)[0]}"
                      f"{f' (+{len(workflows) - 1} more)' if len(workflows) > 1 else ''}")

    listed = [m for m in result.statuses if m.listed]
    verify_enabled = bool(settings.config.get("verify_online", True))
    with ModelDatabase(DB_PATH) as db:
        checks = verify_units([m.unit for m in listed if m.deletable], db, settings,
                              online=verify_enabled and not args.offline, recheck=args.recheck)
    entries = sort_entries([ListEntry(m, checks.get(m.unit.rel, Verification("skipped")))
                            for m in listed])
    keep_templates = args.keep_template_models
    for entry in entries:
        entry.delete = default_delete(entry, verify_enabled, keep_templates)

    if not entries:
        print("\nEvery model is used by a workflow from exactly its place - nothing to review.")
    elif not args.no_edit:
        if not review(entries, list_path, settings, verify_enabled, keep_templates):
            print("Quit without saving. The previous list (if any) was not changed.")
            return 0
    write_list(list_path, entries, settings.models_dir, keep_templates)

    deletes = [e for e in entries if e.delete]
    moves = [e for e in entries if e.move]
    print(f"\nSaved the list to {list_path}")
    print(f"Marked: {len(deletes)} model(s) to delete ({human_size(sum(e.unit.size for e in deletes))}), "
          f"{len(moves)} to move.")
    notice = template_notice(entries, keep_templates, 'marked "template model" in the list')
    if notice:
        print()
        for line in textwrap.wrap(notice, 96):
            print(f"!!! {line}")
    if deletes or moves:
        extra = "" if list_path == DEFAULT_LIST_PATH.resolve() else f' --list "{list_path}"'
        print(f"To carry this out run:  python delete_unused_models.py{extra}   (add --dry-run to preview)")
    return 0
