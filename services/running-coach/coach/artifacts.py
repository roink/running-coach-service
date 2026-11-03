"""Copy only bounded regular artifacts without following path components."""

import os
import stat
from pathlib import Path, PurePosixPath

MAX_IMAGE_BYTES = 20 * 1024 * 1024


def read_export(root: Path, relative: str, limit=MAX_IMAGE_BYTES):
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(p in {".", ".."} for p in path.parts):
        raise ValueError("Invalid artifact path")
    if "\\" in relative or "\x00" in relative:
        raise ValueError("Invalid artifact path")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        image = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(image, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                raise ValueError("Artifact must be a bounded regular file")
            content = source.read(limit + 1)
            if len(content) > limit:
                raise ValueError("Artifact exceeds size limit")
            return content
    finally:
        os.close(descriptor)
