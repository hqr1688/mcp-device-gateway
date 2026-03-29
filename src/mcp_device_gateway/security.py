from __future__ import annotations

import re
from pathlib import PurePosixPath

SAFE_ARG_PATTERN = re.compile(r"^[a-zA-Z0-9_./:=+\-]+$")


def sanitize_args(args: list[str]) -> list[str]:
    sanitized: list[str] = []
    for arg in args:
        if not SAFE_ARG_PATTERN.fullmatch(arg):
            raise ValueError(
                f"Unsafe argument '{arg}'. Allowed pattern: {SAFE_ARG_PATTERN.pattern}"
            )
        sanitized.append(arg)
    return sanitized


def is_path_allowed(remote_path: str, allowed_roots: tuple[str, ...]) -> bool:
    normalized = str(PurePosixPath(remote_path))
    for root in allowed_roots:
        root_norm = str(PurePosixPath(root))
        if normalized == root_norm or normalized.startswith(root_norm.rstrip("/") + "/"):
            return True
    return False
