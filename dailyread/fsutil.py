"""Small file helpers shared by the pipeline and the CLI."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_private(path: Path, text: str) -> None:
    """Atomic write (temp file + rename) with owner-only permissions. Reports embed the helper token and hold fetched
    article text, so other accounts on the Mac get no read access."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")      # mkstemp creates it 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
