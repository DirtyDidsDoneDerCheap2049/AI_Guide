"""本地持久卷上的图片存储与“实际内容”校验。

- 存储名使用随机对象键，原始文件名只作为元数据。
- 上传目录不是静态目录：读取图片必须经过项目会话校验（见 api/media.py）。
"""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.config import Settings

FORMAT_TO_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
FORMAT_TO_EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


class MediaValidationError(RuntimeError):
    """上传校验失败。code 用于 API 错误码和测试断言，不含文件内容。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True)
class ImageInfo:
    mime_type: str
    extension: str
    width: int
    height: int
    size_bytes: int
    sha256: str


def validate_image_bytes(data: bytes, settings: Settings, declared_mime: str | None = None) -> ImageInfo:
    """按实际字节内容判断类型，不信任 Content-Type。"""
    if not data:
        raise MediaValidationError("empty_file")
    if len(data) > settings.media_max_bytes:
        raise MediaValidationError("file_too_large", f"max={settings.media_max_bytes}")

    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError):
        raise MediaValidationError("unsupported_image_content") from None

    mime = FORMAT_TO_MIME.get(image_format)
    if mime is None or mime not in settings.allowed_mime_set:
        raise MediaValidationError("unsupported_mime_type", f"detected={image_format.lower() or 'unknown'}")
    if declared_mime and declared_mime.split(";")[0].strip().lower() not in (mime, "application/octet-stream"):
        raise MediaValidationError("mime_mismatch")
    if width <= 0 or height <= 0:
        raise MediaValidationError("invalid_dimensions")

    return ImageInfo(
        mime_type=mime,
        extension=FORMAT_TO_EXT[image_format],
        width=width,
        height=height,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def store_image(data: bytes, project_id: str, info: ImageInfo, media_root: Path) -> str:
    """写入随机对象键，返回 storage_key（相对 media_root）。"""
    relative = Path(project_id) / f"{uuid.uuid4().hex}.{info.extension}"
    absolute = media_root / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(data)
    return relative.as_posix()


def storage_path(media_root: Path, storage_key: str) -> Path:
    root = media_root.resolve()
    candidate = (root / storage_key).resolve()
    if root not in candidate.parents and candidate != root:
        raise MediaValidationError("invalid_storage_key")
    return candidate


def read_image_bytes(media_root: Path, storage_key: str, max_bytes: int) -> bytes:
    path = storage_path(media_root, storage_key)
    if not path.is_file():
        raise MediaValidationError("media_file_missing")
    size = path.stat().st_size
    if size > max_bytes:
        raise MediaValidationError("file_too_large", f"max={max_bytes}")
    return path.read_bytes()


def delete_stored_file(media_root: Path, storage_key: str) -> bool:
    try:
        path = storage_path(media_root, storage_key)
    except MediaValidationError:
        return False
    if path.is_file():
        path.unlink()
        return True
    return False
