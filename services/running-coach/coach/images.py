"""Validate and normalize raster images before either direction of handoff."""

import io
import warnings

from PIL import Image

from coach.artifacts import MAX_IMAGE_BYTES


def normalize(content):
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds upload limit")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "is_animated", False):
                raise ValueError("Unsupported image format")
            if image.width * image.height > 20_000_000:
                raise ValueError("Decoded image exceeds pixel budget")
            output = io.BytesIO()
            image.convert("RGB").save(output, format="PNG")
            data = output.getvalue()
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("Normalized image exceeds upload limit")
            return data, image.width, image.height
