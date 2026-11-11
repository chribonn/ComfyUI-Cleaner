"""The version of ComfyUI-Cleaner. This is the only place where it is set.

Both programs take it from here: it is printed when they start and with
--version, written at the top of the review list (unused_models.txt), added to
every line of deletion_log.txt and sent as the User-Agent of the online lookups.

It is deliberately not stored in comfyui_cleaner_config.json, which holds the
user's own settings, so an upgrade never has to change that file.

When you change the version, also change it at the top of README.md.
"""

APP_NAME = "ComfyUI-Cleaner"
__version__ = "1.0.0-beta"

# "ComfyUI-Cleaner 1.0.0-beta", as shown in the console, the list and the log.
VERSION_TEXT = f"{APP_NAME} {__version__}"
