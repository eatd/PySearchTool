import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional


def is_binary_file(path: Path, block_size: int = 1024) -> bool:
    """Checks if a file is binary by looking for NULL bytes in the first block."""
    try:
        with path.open("rb") as f:
            return b"\0" in f.read(block_size)
    except Exception:
        return True  # Assume binary if unreadable


def atomic_write(path: Path, content: str, make_backup: bool = True):
    """
    Writes content to a file atomically to prevent corruption.
    1. Writes to a temp file.
    2. Backs up original (optional).
    3. Renames temp file to target.
    """
    dir_name = path.parent
    with tempfile.NamedTemporaryFile(
        mode="w", dir=dir_name, delete=False, encoding="utf-8"
    ) as tf:
        tf.write(content)
        temp_name = tf.name

    try:
        if make_backup and path.exists():
            bak_path = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, bak_path)
        os.replace(temp_name, path)
    except Exception as e:
        if os.path.exists(temp_name):
            os.remove(temp_name)
        raise e


def read_file_lines(
    path: Path, is_archive: bool = False, member: Optional[str] = None
) -> List[str]:
    """Unified reader for text files, zip archives, and tarballs."""
    try:
        if is_archive:
            if path.suffix == ".zip":
                with zipfile.ZipFile(path) as z:
                    with z.open(member) as f:
                        return (
                            f.read()
                            .decode("utf-8", errors="replace")
                            .splitlines(keepends=True)
                        )
            elif str(path).endswith(("tar.gz", ".tgz")):
                with tarfile.open(path) as t:
                    f = t.extractfile(member)
                    if f:
                        return (
                            f.read()
                            .decode("utf-8", errors="replace")
                            .splitlines(keepends=True)
                        )
        else:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                return f.readlines()
    except Exception:
        return []
    return []
