"""Find ComfyUI models that no workflow uses and save an editable list of them.

Usage:  python find_unused_models.py [--offline] [--recheck] [--show-used] [--reconfigure]
"""

import sys

from comfy_cleaner.finder import main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nStopped.")
