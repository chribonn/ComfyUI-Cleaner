"""Configuration file handling.

First run: asks where ComfyUI is installed and stores it in the config file.
Every run: checks that the user folder (ComfyUI\\user\\default) and the models
folder (ComfyUI\\models) exist. If one is missing, asks for it and stores the
answer, which supports non-standard installations.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .util import ask

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "comfyui_cleaner_config.json"
DB_PATH = APP_DIR / "comfyui_cleaner.db"
DEFAULT_LIST_PATH = APP_DIR / "unused_models.txt"
LOG_PATH = APP_DIR / "deletion_log.txt"

DEFAULT_CONFIG: dict = {
    # Set on the first run. Either the Easy-Install folder or the ComfyUI folder itself.
    "comfyui_path": None,
    # null = derive from comfyui_path. Filled in when the default location does not exist.
    "user_dir": None,
    "models_dir": None,
    # File types treated as model files.
    "model_extensions": [
        ".safetensors", ".ckpt", ".pt", ".pt2", ".pth", ".bin",
        ".gguf", ".sft", ".onnx", ".pkl",
    ],
    # Paths (relative to the models folder, wildcards allowed) that are never listed
    # for deletion. vae_approx holds the preview decoders ComfyUI uses without any
    # workflow naming them; insightface models are picked by face nodes automatically.
    "always_keep": ["vae_approx/*", "insightface/*"],
    # Online verification of unused models.
    "verify_online": True,
    "use_civitai": True,
    # Hugging Face accounts whose complete file lists are indexed locally. Most
    # ComfyUI-ready files are published by these accounts.
    "huggingface_authors": [
        "Comfy-Org", "comfyanonymous", "Kijai", "city96",
        "QuantStack", "Lightricks", "lightx2v",
    ],
    # Optional. Empty = use the HF_TOKEN / CIVITAI_API_KEY environment variables, if set.
    "huggingface_token": "",
    "civitai_api_key": "",
    # Models not found online are looked up again after this many days.
    "recheck_unverified_days": 7,
    # The Hugging Face author index is refreshed after this many days.
    "hf_index_max_age_days": 7,
}


@dataclass
class Settings:
    config: dict
    config_path: Path
    comfyui_dir: Path
    user_dir: Path
    models_dir: Path

    @property
    def extensions(self) -> list[str]:
        exts = []
        for ext in self.config.get("model_extensions") or DEFAULT_CONFIG["model_extensions"]:
            ext = str(ext).strip().lower()
            if ext:
                exts.append(ext if ext.startswith(".") else "." + ext)
        return exts

    @property
    def keep_patterns(self) -> list[str]:
        return [str(p) for p in self.config.get("always_keep") or []]

    @property
    def huggingface_token(self) -> str:
        return (self.config.get("huggingface_token") or os.environ.get("HF_TOKEN") or "").strip()

    @property
    def civitai_api_key(self) -> str:
        return (self.config.get("civitai_api_key") or os.environ.get("CIVITAI_API_KEY") or "").strip()


def _clean_path(raw: str) -> Path | None:
    text = raw.strip().strip('"').strip("'").strip()
    if not text:
        return None
    return Path(os.path.expandvars(os.path.expanduser(text)))


def prompt_for_folder(question: str) -> Path:
    while True:
        raw = ask(question)
        if raw.lower() in ("q", "quit", "exit"):
            raise SystemExit("Cancelled.")
        path = _clean_path(raw)
        if path is None:
            print("  Please enter a folder path (or q to quit).")
            continue
        if not path.is_dir():
            print(f"  Folder not found: {path}")
            continue
        return path.resolve()


def resolve_comfyui_dir(install_path: Path) -> Path:
    """Accept either the Easy-Install/portable folder or the ComfyUI folder itself."""
    nested = install_path / "ComfyUI"
    if nested.is_dir() and not (install_path / "main.py").exists():
        return nested
    return install_path


def _read_config(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read the config file {path}: {exc}\n"
                         "Fix it, or delete it to be asked for the paths again.") from None
    if not isinstance(data, dict):
        raise SystemExit(f"The config file {path} does not contain a JSON object.")
    return data


def save_config(path: Path, config: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _ensure_folder(config: dict, key: str, default: Path, label: str, hint: str) -> tuple[Path, bool]:
    configured = config.get(key)
    path = Path(configured) if configured else default
    if path.is_dir():
        return path, False
    print(f"\nThe {label} was not found:\n  {path}")
    new_path = prompt_for_folder(f"Enter the path of the {label} (normally {hint}): ")
    config[key] = str(new_path)
    return new_path, True


def load_settings(config_path: Path = DEFAULT_CONFIG_PATH, reconfigure: bool = False) -> Settings:
    config = _read_config(config_path)
    changed = False
    if config is None:
        print(f"No configuration file found ({config_path}). Let's create one.")
        config = {}
        changed = True

    for key, value in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = copy.deepcopy(value)
            changed = True

    if reconfigure or not config.get("comfyui_path"):
        install = prompt_for_folder(
            "Enter the folder where ComfyUI is installed (e.g. C:\\ComfyUI-Easy-Install): ")
        config["comfyui_path"] = str(install)
        config["user_dir"] = None
        config["models_dir"] = None
        changed = True

    comfyui_dir = resolve_comfyui_dir(Path(config["comfyui_path"]))
    user_dir, user_changed = _ensure_folder(
        config, "user_dir", comfyui_dir / "user" / "default",
        "ComfyUI user folder", "ComfyUI\\user\\default")
    models_dir, models_changed = _ensure_folder(
        config, "models_dir", comfyui_dir / "models",
        "ComfyUI models folder", "ComfyUI\\models")

    if changed or user_changed or models_changed:
        save_config(config_path, config)
        print(f"Configuration saved to {config_path}")

    return Settings(config, config_path, comfyui_dir, user_dir.resolve(), models_dir.resolve())
