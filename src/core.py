import fnmatch
import os
import queue
import re
import tarfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .utils import is_binary_file

# --- Config ---
MAX_FILE_SIZE_MB = 10
DEFAULT_MAX_MATCHES = 5000


@dataclass(frozen=True)
class Match:
    path: Path
    line_no: int
    preview: str
    is_archive: bool = False
    member: Optional[str] = None


@dataclass
class SearchStats:
    scanned_files: int = 0
    total_candidates: int = 0
    matches_found: int = 0
    start_time: float = 0.0


class SearchEngine:
    def __init__(
        self,
        root: Path,
        opts: dict,
        include: List[str],
        exclude: List[str],
        stop_event: threading.Event,
    ):
        self.root = root
        self.opts = opts
        self.include = include
        self.exclude = exclude
        self.stop_event = stop_event
        self.stats = SearchStats()

        # Compile Regex
        flags = 0 if opts.get("case") else re.IGNORECASE
        pattern = opts.get("text", "")
        try:
            if opts.get("regex"):
                self.regex = re.compile(pattern, flags)
            elif opts.get("whole_word"):
                self.regex = re.compile(rf"\b{re.escape(pattern)}\b", flags)
            else:
                self.regex = re.compile(re.escape(pattern), flags)
        except re.error as e:
            raise ValueError(f"Invalid regex: {e}")

    def _matches_globs(self, name: str) -> bool:
        if self.include and not any(fnmatch.fnmatch(name, p) for p in self.include):
            return False
        if any(fnmatch.fnmatch(name, p) for p in self.exclude):
            return False
        return True

    def _search_content(
        self, path: Path, is_archive: bool, member: Optional[str]
    ) -> List[Match]:
        matches = []
        try:
            lines = []
            if is_archive:
                # Basic reading for regex matching (loading into memory for now)
                if path.suffix == ".zip":
                    with zipfile.ZipFile(path) as z:
                        with z.open(member) as f:
                            lines = (
                                f.read().decode("utf-8", errors="replace").splitlines()
                            )
                elif str(path).endswith(("tar.gz", ".tgz")):
                    with tarfile.open(path) as t:
                        f = t.extractfile(member)
                        if f:
                            lines = (
                                f.read().decode("utf-8", errors="replace").splitlines()
                            )
            else:
                if path.stat().st_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                    return []
                if is_binary_file(path):
                    return []

                with path.open("r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()

            for i, line in enumerate(lines, start=1):
                if self.stop_event.is_set():
                    break
                if self.regex.search(line):
                    # Truncate preview
                    preview = line.strip()[:200]
                    matches.append(Match(path, i, preview, is_archive, member))
        except Exception:
            pass
        return matches

    def run(self, out_q: queue.Queue):
        self.stats.start_time = time.time()
        max_matches = self.opts.get("max_matches", DEFAULT_MAX_MATCHES)

        # 1. Scan Phase
        candidates = []
        for dirpath, dirnames, filenames in os.walk(
            self.root, followlinks=self.opts.get("follow_symlinks")
        ):
            if self.stop_event.is_set():
                break

            # Prune directories
            if not self.opts.get("include_hidden"):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if self.opts.get("use_default_ignores"):
                dirnames[:] = [
                    d
                    for d in dirnames
                    if d
                    not in {
                        ".git",
                        "venv",
                        "__pycache__",
                        "node_modules",
                        ".idea",
                        ".vscode",
                    }
                ]

            for name in filenames:
                if not self.opts.get("include_hidden") and name.startswith("."):
                    continue
                if not self._matches_globs(name):
                    continue

                path = Path(dirpath) / name
                candidates.append((path, False, None))

                # Scan Archives?
                if self.opts.get("search_archives") and name.endswith(
                    (".zip", ".tar.gz", ".tgz")
                ):
                    try:
                        if name.endswith(".zip"):
                            with zipfile.ZipFile(path) as z:
                                for info in z.infolist():
                                    if not info.is_dir() and self._matches_globs(
                                        info.filename
                                    ):
                                        candidates.append((path, True, info.filename))
                    except:
                        pass

        self.stats.total_candidates = len(candidates)
        out_q.put(("progress", self.stats))

        # 2. Execution Phase
        # Use optimal thread count for IO/CPU mix
        workers = min(32, (os.cpu_count() or 1) + 4)
        with ThreadPoolExecutor(workers) as ex:
            futures = {
                ex.submit(self._search_content, p, arc, mem): (p, mem)
                for p, arc, mem in candidates
            }

            for f in as_completed(futures):
                if self.stop_event.is_set():
                    break
                if self.stats.matches_found >= max_matches:
                    out_q.put(
                        (
                            "warn",
                            f"Max matches ({max_matches}) reached. Search stopped.",
                        )
                    )
                    self.stop_event.set()
                    break

                self.stats.scanned_files += 1
                if self.stats.scanned_files % 50 == 0:
                    out_q.put(("progress", self.stats))

                try:
                    res = f.result()
                    if res:
                        self.stats.matches_found += len(res)
                        for m in res:
                            out_q.put(("match", m))
                except:
                    pass

        out_q.put(("done", self.stats))
