"""Delete the models listed in unused_models.txt (created by find_unused_models.py).

Usage:  python delete_unused_models.py [--dry-run] [--list unused_models.txt]
"""

import sys

from comfy_cleaner.deleter import main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nStopped - models deleted so far are listed in deletion_log.txt.")
