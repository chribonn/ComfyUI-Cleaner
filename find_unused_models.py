"""Compare ComfyUI models with the workflows and save an editable review list.

The list shows orphaned models (no workflow uses them), duplicates (same name in
more than one place) and misplaced models (a workflow looks for them in another
folder), so they can be deleted or moved with delete_unused_models.py.

Usage:  python find_unused_models.py [--offline] [--recheck] [--show-used] [--no-edit]
                                     [--reconfigure] [--config FILE] [--list FILE]
"""

import sys

from comfy_cleaner.finder import main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nStopped.")
