# ComfyUI-Cleaner

ComfyUI-Cleaner compares your ComfyUI models with your workflows. It finds models that no workflow uses, files that exist in more than one place, and files that are in the wrong folder, and helps you delete or move them. Before a model is suggested for deletion, it is checked online so you know it can be downloaded again from Hugging Face or Civitai. The download links are kept in a log.

The work is split into two programs:

1. `find_unused_models.py` reads your workflows and your models folder, checks the candidates online, lets you review them and saves the result to `unused_models.txt`.
2. `delete_unused_models.py` carries out that list (deletes and moves), after checking every line again and asking you to confirm.

Nothing is deleted or moved until you run the second program and confirm.

## Requirements

- Python 3.9 or newer. Only the standard library is used, so there is nothing to install.
- An internet connection for the online check (optional, see `--offline`).
- Written for Windows. It also runs on Linux and macOS. The ComfyUI folder can be on a network share (`\\server\share\ComfyUI-Easy-Install`).

## Setup

1. Rename (or copy) `comfyui_cleaner_config.example.json` to `comfyui_cleaner_config.json`:

   ```powershell
   Copy-Item comfyui_cleaner_config.example.json comfyui_cleaner_config.json
   ```

2. Open `comfyui_cleaner_config.json` and add your API keys:

   ```json
   "huggingface_token": "hf_...",
   "civitai_api_key": "...",
   ```

   - **Hugging Face token**: create one at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens). A token with read access is enough.
   - **Civitai API key**: sign in to [civitai.com](https://civitai.com/user/account), open Account settings and click Add API key in the API Keys section.

   You can leave a key empty and set the `HF_TOKEN` or `CIVITAI_API_KEY` environment variable instead. Without any key the online check still works but runs anonymously, and Hugging Face applies lower rate limits to anonymous requests.

Leave `comfyui_path` as `null`: the first run asks where ComfyUI is installed and fills it in. If you skip this setup, the first run creates `comfyui_cleaner_config.json` with the defaults and no keys.

`comfyui_cleaner_config.json` is listed in `.gitignore`. It holds your keys, so do not share or commit it.

## Quick start

```powershell
# 1. Find unused, duplicate and misplaced models and review the list
python find_unused_models.py

# 2. Preview what would be deleted and moved
python delete_unused_models.py --dry-run

# 3. Carry it out
python delete_unused_models.py
```

On the first run you are asked where ComfyUI is installed. You can enter either the Easy-Install/portable folder (e.g. `C:\ComfyUI-Easy-Install`) or the `ComfyUI` folder itself. The answer is saved in `comfyui_cleaner_config.json`. If the user folder (`ComfyUI\user\default`) or the models folder (`ComfyUI\models`) is not in the usual place, you are asked for it as well. To work with more than one installation, give each its own config and list file with `--config` and `--list`.

## How models are compared with workflows

- **Workflows**: every `.json` file under `ComfyUI\user\default` (including subfolders, but not hidden ones) that is a ComfyUI workflow in UI or API format. Settings files and other JSON files are ignored.
- **Models**: every file in `ComfyUI\models` with a model extension (`.safetensors`, `.ckpt`, `.pt`, `.gguf`, ...), in any subfolder. Model folders downloaded from Hugging Face (for example `whisper/whisper-large-v3`) are treated as one model.

ComfyUI saves a model's path relative to its model folder, without the folder itself. A `LoraLoader` that shows `h3\whatever.safetensors` loads `models\loras\h3\whatever.safetensors`. The node type tells which model folder that is. Each model gets one of these results:

| Result | Meaning | In the list |
|---|---|---|
| used | A workflow loads it from exactly this place: same path inside the model folder, and the node reads from that folder | No (unless it is a duplicate) |
| misplaced | A workflow names the file, but looks for it in another folder or subfolder, and no copy is there | Yes. It is never deleted, but it can be moved to where the workflow expects it |
| orphan | No workflow loads it | Yes, can be deleted |
| duplicate | The same file name exists in more than one place (shown together with one of the results above) | Yes. You choose which copies to delete |

Details:

- **Model folders**: `diffusion_models` and `unet` count as one folder, as do `text_encoders` and `clip`, the same way ComfyUI treats them. The folder each node reads from is known for ComfyUI's own loaders and popular custom node packs (GGUF, KJNodes, WanVideoWrapper, rgthree, LTXVideo). For other nodes, the path inside any of ComfyUI's model folders counts.
- **What counts as loading a model**: the values of a node (its widgets), `<lora:name>` and `embedding:name` in prompts, and models in download links. Names that only appear in notes, HTML info boxes, titles or the download list ComfyUI keeps in node properties do not load anything, so such a model is an orphan. Its details say where it was named.
- **A file can be used and expected elsewhere**: if one workflow loads `loras\x.safetensors` and another looks for `loras\h3\x.safetensors`, the file is used. The other location is shown with `--show-used` and in the details.
- **Moving**: a misplaced model can be moved only when all the workflows that look for it agree on one place, the folder is clear (some nodes read from more than one folder), and nothing exists there yet.
- **Duplicates**: listed even when workflows use every copy. If a workflow uses or looks for the file, at least one copy has to stay. The list says whether the copies have the same size; same name with a different size may be a different model.
- **Custom node folders** (any folder in `models` that is not one of ComfyUI's own, for example `qwen-tts` or `LLM`) are matched loosely by name, because custom nodes follow their own conventions. Their models are never misplaced and never pre-marked for deletion: custom nodes often load a model without the workflow naming it.
- Models that match an `always_keep` pattern in the config are never listed.

### Online check

Orphans and duplicates are looked up online:

- **Single files on Hugging Face**: a file with the same name and the same size. Candidates come from a local index of the accounts in `huggingface_authors` and from a Hugging Face search.
- **Single files on Civitai**: the same SHA-256 hash. Files are only hashed if Hugging Face did not find them, and the hashes are cached. Hashing reads the whole file, which is slow over a network share; set `use_civitai` to `false` to skip it.
- **Model folders**: a Hugging Face repository with the same name that contains at least one of the folder's weight files with the same size.

Results are saved in `comfyui_cleaner.db`. A verified model is never looked up again. A model that was not found is looked up again after `recheck_unverified_days` (7 by default), or right away with `--recheck`.

Only orphans verified online (and not in a custom node folder) are marked for deletion by default. Nothing is marked for moving by default.

### Reviewing the list

After the scan you get a numbered table in three sections:

```
Orphaned models - no workflow uses them:
 1 [x]   27.14 GB  checkpoints/ltx-2.3-22b-dev-fp8.safetensors       orphan    HF: Lightricks/LTX-2.3-fp8
 2 [ ]    4.10 GB  LLM/some-model/                                  ! orphan    not found online

Duplicates - the same name in more than one place:
 3 [ ]  591.55 MB  loras/h3/some_lora.safetensors                     used dup1 HF: some-author/some-repo
 4 [x]  591.55 MB  loras/old/some_lora.safetensors                    orphan dup1 HF: some-author/some-repo

Misplaced models - a workflow names them but looks in another folder:
 5 [m]    5.78 GB  diffusion_models/z_image_turbo_int8_convrot.safetensors  misplaced wants it in diffusion_models/z-image/
```

`[x]` = delete, `[m]` = move, `[ ]` = keep. `dupN` marks the copies of one file (`*` = the sizes differ). `!` marks a model in a custom node folder.

| Command | Action |
|---|---|
| `1,3,5-8` | Switch the listed numbers between delete `[x]` and keep `[ ]` |
| `m 4` | Switch misplaced model 4 between move `[m]` and keep `[ ]` |
| `a` | Mark all orphans for deletion |
| `n` | Keep everything (also clears moves) |
| `v` | Mark only orphans verified online (the default) |
| `i 4` | Show details of model 4: which workflows use it or where they expect it, its other copies, download link |
| `e` | Edit the list in a text editor (Notepad on Windows, otherwise `$EDITOR` or `nano`) |
| `l` | Show the list again |
| `h` | Show the help |
| Enter | Save the list and finish |
| `q` | Quit without saving (the previous list is not changed) |

You cannot mark a misplaced model for deletion (unless it is a duplicate), or the last copy of a file that workflows need. Deleting a copy that a workflow uses is allowed when another copy stays; you are reminded to point the workflow at the other copy.

### Deleting and moving

`delete_unused_models.py` reads the workflows and the models folder again and checks every line of the list before it does anything. A line is skipped if the path:

- is outside the models folder, or is the models folder or one of its model category folders (`checkpoints`, `loras`, ...)
- is not a model found by the scan (model file by extension, or model folder)
- is a symlink or junction, or lies inside a linked folder
- is inside a hidden or cache folder
- matches an `always_keep` pattern
- no longer exists

Deletions are only carried out for orphans, and for duplicates as long as one copy of a file that workflows use stays. Used and misplaced models are skipped. Moves are only carried out for models that are still misplaced, to exactly the place the workflows expect, and never over an existing file. If no workflow files are found at all, nothing is changed.

You then see the planned moves and deletions. Moves need a `y`; deletions need you to type `DELETE`. **Deleted files do not go to the Recycle Bin.** Every deletion and move is written to `deletion_log.txt`, deletions with their download link (if the model was verified), so you can get the model back later.

## Command-line arguments

### find_unused_models.py

| Argument | Description |
|---|---|
| `--config CONFIG` | Configuration file to use. Default: `comfyui_cleaner_config.json` in the program folder. |
| `--list LIST` | Where to save the list. Default: `unused_models.txt` in the program folder. |
| `--reconfigure` | Ask for the ComfyUI installation folder again. |
| `--offline` | Do not go online. Use only what the local database already knows. |
| `--recheck` | Look up models online again that were not found before, even if `recheck_unverified_days` has not passed. |
| `--show-used` | Also print the models that are used, by which workflows, and where other workflows expect them. |
| `--no-edit` | Save the list without the interactive review. The default selection is used. |
| `-h`, `--help` | Show the help and exit. |

### delete_unused_models.py

| Argument | Description |
|---|---|
| `--config CONFIG` | Configuration file to use. Default: `comfyui_cleaner_config.json` in the program folder. |
| `--list LIST` | List to carry out. Default: `unused_models.txt` in the program folder. |
| `--dry-run` | Only show what would be deleted and moved. Nothing is changed. |
| `-h`, `--help` | Show the help and exit. |

### Examples

```powershell
# Scan without internet, also showing which workflows use which models
python find_unused_models.py --offline --show-used

# Point the program at a different ComfyUI installation
python find_unused_models.py --reconfigure

# A second installation on a network share, with its own config and list
python find_unused_models.py --config remote.json --list remote_list.txt
python delete_unused_models.py --config remote.json --list remote_list.txt --dry-run
```

## The list file

`unused_models.txt` is plain text that you can edit by hand. Lines starting with `#` are comments. Every other line is carried out:

- a path: the model is deleted
- `MOVE <from> -> <to>`: the model is moved

Each model has a comment line above it with its size, type, result, online status and download link.

```
# ===== Orphaned models - no workflow uses them =====

# 27.14 GB | file | orphan | HF: Lightricks/LTX-2.3-fp8 | https://huggingface.co/...
C:\ComfyUI\models\checkpoints\ltx-2.3-22b-dev-fp8.safetensors

# ===== Duplicates - the same name in more than one place =====

# --- Duplicate 1: some_lora.safetensors (same size)
# 591.55 MB | file | used by 2 workflows | HF: some-author/some-repo
# C:\ComfyUI\models\loras\h3\some_lora.safetensors
# 591.55 MB | file | orphan (the workflows that name it use another copy) | HF: some-author/some-repo
C:\ComfyUI\models\loras\old\some_lora.safetensors

# ===== Misplaced models - a workflow names them but looks in another folder =====

# 5.78 GB | file | misplaced, expected at diffusion_models/z-image/z_image_turbo_int8_convrot.safetensors
# MOVE C:\ComfyUI\models\diffusion_models\z_image_turbo_int8_convrot.safetensors -> C:\ComfyUI\models\diffusion_models\z-image\z_image_turbo_int8_convrot.safetensors
```

To skip a line, delete it or put `#` in front of it. To carry out a line that starts with `#`, remove the `#`.

## Configuration

`comfyui_cleaner_config.json` is made from `comfyui_cleaner_config.example.json` (see [Setup](#setup)), or created on the first run if it does not exist. Missing keys are filled in with their defaults.

| Key | Default | Description |
|---|---|---|
| `comfyui_path` | set on first run | Easy-Install folder or ComfyUI folder |
| `user_dir` | `null` | Folder with the workflows. `null` means `ComfyUI\user\default` |
| `models_dir` | `null` | Models folder. `null` means `ComfyUI\models` |
| `model_extensions` | `.safetensors`, `.ckpt`, `.pt`, `.pt2`, `.pth`, `.bin`, `.gguf`, `.sft`, `.onnx`, `.pkl` | File types treated as models |
| `always_keep` | `["vae_approx/*", "insightface/*"]` | Paths relative to the models folder that are never listed (wildcards allowed) |
| `verify_online` | `true` | Check orphans and duplicates online. If `false`, all orphans outside custom node folders are marked for deletion by default |
| `use_civitai` | `true` | Also look up files on Civitai by hash |
| `huggingface_authors` | `Comfy-Org`, `comfyanonymous`, `Kijai`, `city96`, `QuantStack`, `Lightricks`, `lightx2v` | Hugging Face accounts whose file lists are indexed locally |
| `huggingface_token` | `""` | Optional. If empty, the `HF_TOKEN` environment variable is used |
| `civitai_api_key` | `""` | Optional. If empty, the `CIVITAI_API_KEY` environment variable is used |
| `recheck_unverified_days` | `7` | Days before a model that was not found is looked up again |
| `hf_index_max_age_days` | `7` | Days before the Hugging Face account index is refreshed |

The config file contains your API keys, so do not share or commit it. Keep `comfyui_cleaner_config.example.json` free of keys and paths.

## Files

All files are stored in the program folder:

| File | Contents |
|---|---|
| `comfyui_cleaner_config.example.json` | Template with the default settings. Rename it to `comfyui_cleaner_config.json` |
| `comfyui_cleaner_config.json` | Your settings and API keys |
| `comfyui_cleaner.db` | Cache of online checks, file hashes and the Hugging Face index (shared by all configs) |
| `unused_models.txt` | The review list |
| `deletion_log.txt` | Tab-separated log: time, `DELETED`/`MOVED`/`FAILED`, size in bytes, path, then the download link, new location or error |

You can delete `comfyui_cleaner.db` at any time. The next scan will look everything up online again. The download links in `deletion_log.txt` come from this database, so run a new scan before deleting if you removed it.
