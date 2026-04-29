"""Deezer result types replacing tuple return values."""

from typing import Any

import msgspec

from haberlea.utils.models import CodecEnum, ImageFileTypeEnum


class TrackAvailability(msgspec.Struct, frozen=True):
    """Track availability check result, replaces tuple[str, str | None]."""

    audio_format: str
    fallback_id: str | None


class TrackCodecInfo(msgspec.Struct, frozen=True):
    """Track codec info, replaces tuple[CodecEnum, int | None]."""

    codec: CodecEnum
    bitrate: int | None


class LoginResult(msgspec.Struct, frozen=True):
    """Login result, replaces tuple[str, dict[str, Any]]."""

    arl: str
    user_data: dict[str, Any]


class ImageRequest(msgspec.Struct, frozen=True):
    """Image request params, replaces 5-param _get_image_url."""

    md5_hash: str
    img_type: str
    file_type: ImageFileTypeEnum
    resolution: int
