"""Check orphaned and duplicate models against Hugging Face and Civitai.

Results are stored in the local database, so a model that has been verified is
never looked up online again, and models that were not found are only re-checked
after a few days (see "recheck_unverified_days" in the config).

How a model is verified
  * Single files on Hugging Face: a file with the same name AND the same size in
    a Hugging Face repository. Candidate repositories come from a local index of
    the accounts listed in "huggingface_authors" plus a Hugging Face search.
  * Single files on Civitai: identical SHA-256 hash (exact match). Files are only
    hashed when Hugging Face did not find them; hashes are cached.
  * Model folders: a Hugging Face repository with the same name that contains at
    least one of the folder's weight files with the same size.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Settings
from .database import ModelDatabase
from .models import ModelUnit
from .util import human_size
from .version import APP_NAME, __version__

USER_AGENT = f"{APP_NAME}/{__version__}"
HF_API = "https://huggingface.co/api"
HF_SITE = "https://huggingface.co"
CIVITAI_BY_HASH = "https://civitai.com/api/v1/model-versions/by-hash"

MAX_QUERIES_PER_MODEL = 4
MAX_REPOS_PER_MODEL = 10

# Trailing name parts that describe precision/quantisation rather than the model.
_SUFFIX_TOKEN = re.compile(
    r"^(fp\d+|bf16|e4m3fn|e5m2|e4m3|scaled|mixed|fp8mixed|fp8scaled|int\d+|nf4|bnb|"
    r"q\d+|k|m|s|l|0|1|gguf|convrot|w\d+a\d+|pruned|ema|emaonly|\d+bit|kj|comfy|comfyui)$",
    re.IGNORECASE)
_TOKEN = re.compile(r"[^_\-\s]+")


class NetworkError(Exception):
    pass


@dataclass
class Verification:
    status: str          # verified | name_only | not_found | not_checked | error
    source: str = ""     # huggingface | civitai
    repo: str = ""
    url: str = ""
    note: str = ""
    cached: bool = False

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    @property
    def label(self) -> str:
        if self.status == "verified":
            prefix = {"huggingface": "HF", "civitai": "Civitai"}.get(self.source, self.source)
            return f"{prefix}: {self.repo}"
        return {
            "skipped": "",
            "name_only": "name found online, size differs",
            "not_found": "not found online",
            "not_checked": "not checked (offline)",
            "error": "lookup failed",
        }.get(self.status, self.status)


@dataclass
class _Hit:
    status: str          # verified | name_only
    repo: str
    url: str
    remote_path: str = ""
    sha256: str = ""


def unit_key_size(unit: ModelUnit) -> int:
    """Size used to identify a model in the database (weights only for folders)."""
    if unit.kind == "folder" and unit.weight_files:
        return sum(size for _, size in unit.weight_files)
    return unit.size


# --------------------------------------------------------------------------- HTTP
class HttpClient:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    @staticmethod
    def _retry_wait(headers, attempt: int) -> float:
        for value in (headers.get("Retry-After") if headers else None,):
            if value and str(value).strip().isdigit():
                return min(float(value), 310)
        rate = headers.get("RateLimit") if headers else None
        match = re.search(r"t=(\d+)", rate or "")
        if match:
            return min(float(match.group(1)) + 1, 310)
        return (5, 15, 30, 60)[min(attempt, 3)]

    def request(self, url: str, *, headers: dict | None = None, data: object = None):
        """Return (decoded JSON, response headers); JSON is None for 401/403/404."""
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        hdrs.update(headers or {})
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        host = urllib.parse.urlsplit(url).netloc
        for attempt in range(4):
            req = urllib.request.Request(url, data=body, headers=hdrs,
                                         method="POST" if body is not None else "GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8")), resp.headers
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 404, 410):
                    return None, exc.headers
                if (exc.code == 429 or exc.code >= 500) and attempt < 3:
                    wait = self._retry_wait(exc.headers, attempt)
                    print(f"      {host} is busy (HTTP {exc.code}); waiting {wait:.0f} s ...")
                    time.sleep(wait)
                    continue
                raise NetworkError(f"HTTP {exc.code} from {host}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                reason = getattr(exc, "reason", exc)
                raise NetworkError(f"cannot reach {host}: {reason}") from None
            except ValueError:
                raise NetworkError(f"unexpected response from {host}") from None
        raise NetworkError(f"{host} kept refusing requests")


def _next_link(link_header: str | None) -> str | None:
    for part in (link_header or "").split(","):
        if 'rel="next"' in part:
            match = re.search(r"<([^>]+)>", part)
            if match:
                return match.group(1)
    return None


def file_queries(file_name: str) -> list[str]:
    """Search terms for a model file name, from most to least specific."""
    stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    tokens = list(_TOKEN.finditer(stem))
    queries: list[str] = []

    def add(text: str) -> None:
        text = text.strip(" _-.")
        if len(text) >= 4 and text.lower() not in (q.lower() for q in queries):
            queries.append(text)

    add(stem)
    core = len(tokens)
    while core > 1 and _SUFFIX_TOKEN.match(tokens[core - 1].group(0)):
        core -= 1
    for count in range(core, 1, -1):
        add(stem[:tokens[count - 1].end()])
    if not queries and tokens:
        add(tokens[0].group(0))
    return queries[:MAX_QUERIES_PER_MODEL]


# --------------------------------------------------------------------- Hugging Face
class HuggingFace:
    def __init__(self, http: HttpClient, db: ModelDatabase, settings: Settings):
        self.http = http
        self.db = db
        self.token = settings.huggingface_token
        self.authors = [a for a in settings.config.get("huggingface_authors") or [] if a]
        self.max_age = timedelta(days=float(settings.config.get("hf_index_max_age_days") or 7))
        self.index: dict[str, list[tuple[str, str]]] = {}
        self._blobs: dict[str, dict[str, tuple[str, int, str]]] = {}
        self._searches: dict[str, list[tuple[str, list[str]]]] = {}

    def _get(self, url: str):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        return self.http.request(url, headers=headers)

    def _list_models(self, params: list[tuple[str, str]], paginate: bool) -> list[dict]:
        url = f"{HF_API}/models?{urllib.parse.urlencode(params)}"
        results: list[dict] = []
        for _page in range(100):
            data, headers = self._get(url)
            if not isinstance(data, list):
                break
            results.extend(item for item in data if isinstance(item, dict) and item.get("id"))
            url = _next_link(headers.get("Link") if headers else None) if paginate else None
            if not url:
                break
        return results

    def load_index(self) -> None:
        now = datetime.now(timezone.utc)
        for author in self.authors:
            fetched_at, rows = self.db.get_author_index(author)
            if fetched_at is None or now - fetched_at > self.max_age:
                print(f"    Updating the Hugging Face file index for '{author}' ...")
                try:
                    repos = self._list_models(
                        [("author", author), ("limit", "500"), ("expand[]", "siblings")], paginate=True)
                    rows = []
                    for repo in repos:
                        for sibling in repo.get("siblings") or []:
                            rfilename = str(sibling.get("rfilename") or "")
                            if rfilename:
                                rows.append((rfilename.rsplit("/", 1)[-1].lower(), repo["id"], rfilename))
                    self.db.replace_author_index(author, rows)
                except NetworkError as exc:
                    print(f"    Could not update the index for '{author}' ({exc}); "
                          f"{'using the saved copy' if rows else 'skipping it'}.")
            for filename, repo, rfilename in rows:
                self.index.setdefault(filename, []).append((repo, rfilename))

    def blobs(self, repo: str) -> dict[str, tuple[str, int, str]]:
        """lower-case path -> (path, size, sha256) for every file in the repository."""
        if repo not in self._blobs:
            data, _ = self._get(f"{HF_API}/models/{urllib.parse.quote(repo, safe='/')}?blobs=true")
            files: dict[str, tuple[str, int, str]] = {}
            if isinstance(data, dict):
                for sibling in data.get("siblings") or []:
                    rfilename = str(sibling.get("rfilename") or "")
                    lfs = sibling.get("lfs") or {}
                    size = sibling.get("size", lfs.get("size"))
                    if rfilename and isinstance(size, int):
                        files[rfilename.lower()] = (rfilename, size, str(lfs.get("sha256") or ""))
            self._blobs[repo] = files
        return self._blobs[repo]

    def search(self, query: str) -> list[tuple[str, list[str]]]:
        if query not in self._searches:
            items = self._list_models(
                [("search", query), ("limit", "20"), ("sort", "downloads"),
                 ("direction", "-1"), ("expand[]", "siblings")], paginate=False)
            self._searches[query] = [
                (item["id"], [str(s.get("rfilename") or "") for s in item.get("siblings") or []])
                for item in items]
        return self._searches[query]

    @staticmethod
    def _file_url(repo: str, rfilename: str) -> str:
        return f"{HF_SITE}/{repo}/resolve/main/{urllib.parse.quote(rfilename)}"

    def find_file(self, name: str, size: int) -> _Hit | None:
        lname = name.lower()
        checked: set[str] = set()
        name_only: _Hit | None = None

        def check(repo: str, rfilename: str) -> _Hit | None:
            nonlocal name_only
            checked.add(repo)
            entry = self.blobs(repo).get(rfilename.lower())
            if entry and entry[1] == size:
                return _Hit("verified", repo, self._file_url(repo, entry[0]), entry[0], entry[2])
            if name_only is None:
                name_only = _Hit("name_only", repo, self._file_url(repo, rfilename), rfilename)
            return None

        for repo, rfilename in self.index.get(lname, [])[:MAX_REPOS_PER_MODEL]:
            if repo not in checked and (hit := check(repo, rfilename)):
                return hit
        for query in file_queries(name):
            for repo, siblings in self.search(query):
                if repo in checked or len(checked) >= MAX_REPOS_PER_MODEL:
                    continue
                for rfilename in siblings:
                    if rfilename.rsplit("/", 1)[-1].lower() == lname:
                        if hit := check(repo, rfilename):
                            return hit
                        break
        return name_only

    def find_folder(self, unit: ModelUnit) -> _Hit | None:
        lname = unit.name.lower()
        parent = unit.path.parent.name.lower()
        repos = [repo for repo, _ in self.search(unit.name) if repo.rsplit("/", 1)[-1].lower() == lname]
        repos.sort(key=lambda r: r.split("/", 1)[0].lower() != parent)  # parent folder = author first
        name_only: _Hit | None = None
        for repo in repos[:5]:
            url = f"{HF_SITE}/{repo}"
            if not unit.weight_files:
                return _Hit("verified", repo, url)
            files = self.blobs(repo)
            by_name: dict[str, list[int]] = {}
            for path, size, _sha in files.values():
                by_name.setdefault(path.rsplit("/", 1)[-1].lower(), []).append(size)
            for rel, size in unit.weight_files:
                exact = files.get(rel.lower())
                if (exact and exact[1] == size) or size in by_name.get(rel.rsplit("/", 1)[-1].lower(), []):
                    return _Hit("verified", repo, url)
            if name_only is None:
                name_only = _Hit("name_only", repo, url)
        return name_only


# -------------------------------------------------------------------------- Civitai
class Civitai:
    def __init__(self, http: HttpClient, api_key: str):
        self.http = http
        self.api_key = api_key

    def lookup(self, hashes: list[str]) -> dict[str, _Hit]:
        wanted = sorted({h.lower() for h in hashes if h})
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        found: dict[str, _Hit] = {}
        for start in range(0, len(wanted), 100):
            chunk = wanted[start:start + 100]
            data, _ = self.http.request(CIVITAI_BY_HASH, headers=headers, data=[h.upper() for h in chunk])
            for version in data if isinstance(data, list) else []:
                if not isinstance(version, dict):
                    continue
                model = version.get("model") or {}
                title = f"{model.get('name') or 'model'} ({version.get('name') or 'version'})"
                url = f"https://civitai.com/models/{version.get('modelId')}?modelVersionId={version.get('id')}"
                for file in version.get("files") or []:
                    sha = str((file.get("hashes") or {}).get("SHA256") or "").lower()
                    if sha in chunk and sha not in found:
                        found[sha] = _Hit("verified", title, url, str(file.get("name") or ""), sha)
        return found


# -------------------------------------------------------------------------- hashing
def sha256_of(path, db: ModelDatabase) -> str:
    stat = os.stat(path)
    cached = db.get_hash(path, stat.st_size, stat.st_mtime_ns)
    if cached:
        return cached
    digest = hashlib.sha256()
    done = 0
    last_print = 0.0
    buffer = bytearray(16 * 1024 * 1024)
    view = memoryview(buffer)
    with open(path, "rb", buffering=0) as handle:
        while count := handle.readinto(buffer):
            digest.update(view[:count])
            done += count
            now = time.monotonic()
            if now - last_print > 0.5 and stat.st_size:
                last_print = now
                sys.stdout.write(f"\r      hashing {done * 100 // stat.st_size:3d}% of {human_size(stat.st_size)}")
                sys.stdout.flush()
    sys.stdout.write("\r" + " " * 50 + "\r")
    result = digest.hexdigest()
    db.put_hash(path, stat.st_size, stat.st_mtime_ns, result)
    return result


# ------------------------------------------------------------------------- verifier
def verify_units(units: list[ModelUnit], db: ModelDatabase, settings: Settings,
                 online: bool = True, recheck: bool = False) -> dict[str, Verification]:
    results: dict[str, Verification] = {}
    recheck_after = timedelta(days=float(settings.config.get("recheck_unverified_days") or 7))
    now = datetime.now(timezone.utc)
    pending: list[ModelUnit] = []

    for unit in units:
        key = unit_key_size(unit)
        row = db.get_verified(unit.kind, unit.name, key)
        if row:
            results[unit.rel] = Verification("verified", row["source"], row["repo"] or "",
                                             row["url"] or "", cached=True)
            continue
        failed = db.get_failed(unit.kind, unit.name, key)
        if failed and (not online or (not recheck and now - failed[2] < recheck_after)):
            results[unit.rel] = Verification(failed[0], note=failed[1], cached=True)
            continue
        if not online:
            results[unit.rel] = Verification("not_checked")
            continue
        pending.append(unit)

    if not pending:
        return results

    print(f"\nChecking {len(pending)} model(s) on Hugging Face"
          f"{' and Civitai' if settings.config.get('use_civitai', True) else ''} "
          "(results are saved, so this only happens once per model) ...")
    http = HttpClient()
    hf = HuggingFace(http, db, settings)
    hf.load_index()
    leftovers: list[tuple[ModelUnit, _Hit | None]] = []
    errors = 0

    for number, unit in enumerate(pending, 1):
        print(f"  [{number}/{len(pending)}] {unit.rel}")
        if errors >= 3:
            results[unit.rel] = Verification("error", note="skipped after repeated network errors")
            continue
        try:
            hit = hf.find_file(unit.name, unit.size) if unit.kind == "file" else hf.find_folder(unit)
        except NetworkError as exc:
            errors += 1
            print(f"      lookup failed: {exc}")
            results[unit.rel] = Verification("error", note=str(exc))
            continue
        if hit and hit.status == "verified":
            db.put_verified(unit.kind, unit.name, unit_key_size(unit), source="huggingface",
                            repo=hit.repo, url=hit.url, remote_path=hit.remote_path, sha256=hit.sha256)
            results[unit.rel] = Verification("verified", "huggingface", hit.repo, hit.url)
            print(f"      found on Hugging Face: {hit.repo}")
        else:
            leftovers.append((unit, hit))

    use_civitai = bool(settings.config.get("use_civitai", True))
    civitai_ok = use_civitai and errors < 3
    civitai_hits: dict[str, _Hit] = {}
    file_hashes: dict[str, str] = {}
    to_hash = [unit for unit, _ in leftovers if unit.kind == "file"]
    if to_hash and civitai_ok:
        print(f"\n  Hashing {len(to_hash)} file(s) not found on Hugging Face to look them up on Civitai ...")
        for unit in to_hash:
            print(f"    {unit.rel}")
            try:
                file_hashes[unit.rel] = sha256_of(unit.path, db)
            except OSError as exc:
                print(f"      cannot read the file: {exc}")
        for rel, sha in file_hashes.items():
            row = db.get_verified_by_sha(sha)
            if row:  # same file verified earlier under another name
                civitai_hits[sha] = _Hit("verified", row["repo"] or "", row["url"] or "", sha256=sha)
        try:
            civitai_hits.update(Civitai(http, settings.civitai_api_key).lookup(
                [sha for sha in file_hashes.values() if sha not in civitai_hits]))
        except NetworkError as exc:
            civitai_ok = False
            print(f"    Civitai lookup failed: {exc}")

    for unit, hf_hit in leftovers:
        key = unit_key_size(unit)
        sha = file_hashes.get(unit.rel, "")
        hit = civitai_hits.get(sha) if sha else None
        if hit:
            source = "huggingface" if "huggingface.co" in hit.url else "civitai"
            db.put_verified(unit.kind, unit.name, key, source=source, repo=hit.repo,
                            url=hit.url, remote_path=hit.remote_path, sha256=sha)
            results[unit.rel] = Verification("verified", source, hit.repo, hit.url)
            print(f"    {unit.rel}: found on Civitai: {hit.repo}")
        elif unit.kind == "file" and use_civitai and (not civitai_ok or not sha):
            # Not cached as "not found": Civitai could not be asked, so try again next run.
            results[unit.rel] = Verification("error", note="Civitai could not be checked")
        elif hf_hit:  # only a different-size file with the same name exists
            note = f"{hf_hit.repo} has a file/folder with this name but a different size"
            db.put_failed(unit.kind, unit.name, key, "name_only", note)
            results[unit.rel] = Verification("name_only", "huggingface", hf_hit.repo, hf_hit.url, note)
        else:
            db.put_failed(unit.kind, unit.name, key, "not_found")
            results[unit.rel] = Verification("not_found")
    return results
