"""Shared MIME policy for browser-rendered artifact media."""

from __future__ import annotations

INLINE_RASTER_MIMES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/webp"})
VIDEO_PREVIEW_MIMES: frozenset[str] = frozenset({"video/mp4", "video/webm", "video/ogg"})
INLINE_MEDIA_MIMES: frozenset[str] = INLINE_RASTER_MIMES | VIDEO_PREVIEW_MIMES


def is_previewable_video(mime: str | None) -> bool:
    return (mime or "").lower() in VIDEO_PREVIEW_MIMES
