"""Carry out the review list created by find_unused_models.py.

Path lines are deleted and "MOVE <from> -> <to>" lines are moved, after every
line has been checked again against the current workflows and models folder.

Usage:  python delete_unused_models.py [--dry-run] [--config FILE] [--list unused_models.txt]
"""

import sys

from comfy_cleaner.deleter import main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nStopped - models deleted or moved so far are listed in deletion_log.txt.")
