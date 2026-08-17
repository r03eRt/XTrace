"""Validación y ciclo de vida seguro de la media de consulta (PR-055 · FR-002/003
· SEC-002/003/005 · SC-006 · contracts §5).

Reutiliza la lógica del spike (`xtrace_spike.security`, ADR-0008) **sin
duplicar reglas**: límite de tamaño (`MAX_QUERY_IMAGE_BYTES`), firma MIME por
magic bytes (`detect_query_image_mime`), fichero regular/tamaño
(`validate_query_image`) y decodificación forzada (`open_query_image`).

Mapeo validación → HTTP (contracts §5):
- `413 media_too_large`: la subida supera 10 MB — se corta por streaming al
  volcarla a temporal, **sin procesar** (FR-002/SC-006).
- `415 media_type_not_supported`: firma MIME no soportada (no JPEG/PNG/WebP).
- `400 media_corrupt`: firma válida pero contenido corrupto/ilegible.

Ciclo de vida (SEC-003/FR-003): la subida se vuelca a un **temporal seguro**
(`mkstemp`, 0600) en `work_root` (SEC-005); el borrado está garantizado por el
`finally` del handler **y** por `QueryMediaContext` del spike durante el
procesado. Un fallo de borrado se registra como warning sin enmascarar el
resultado (edge case de la spec).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError
from xtrace_spike.security import (  # type: ignore[import-untyped]
    MAX_QUERY_IMAGE_BYTES,
    QueryMediaError,
    detect_query_image_mime,
    open_query_image,
    validate_query_image,
)

logger = logging.getLogger(__name__)

#: Prefijo de los temporales de subida (localizables en `work_root`, SEC-005).
_UPLOAD_TEMP_PREFIX = "xtrace-api-upload-"

#: Tamaño de lectura por chunk al volcar la subida (límite por streaming).
_CHUNK_SIZE = 64 * 1024


class MediaValidationError(Exception):
    """Media/petición rechazada por validación: transporte HTTP del contrato §5.

    Atributos:
        status_code: código HTTP de la respuesta (400/413/415).
        error_type: tipo máquina estable (contracts §5).
        message: mensaje en español (UX-001), **sin rutas ni nombres de
            fichero** (SEC-005: la media nunca se loguea ni se filtra).
    """

    def __init__(self, status_code: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.message = message


def save_upload_to_temp(upload: UploadFile, work_root: Path) -> Path:
    """Vuelca la subida a un temporal seguro (0600) con límite por streaming.

    El nombre es aleatorio (`mkstemp`) y el fichero nace con permisos 0600
    (solo el propietario; ADR-0006). El límite de
    `MAX_QUERY_IMAGE_BYTES + 1` corta la escritura en cuanto la media supera
    10 MB: **413 sin procesar** (FR-002/SC-006) y sin dejar temporales a
    medias (el parcial se borra).

    Raises:
        MediaValidationError: si la media supera el límite de 10 MB
            (`413 media_too_large`).
    """
    fd, raw_name = tempfile.mkstemp(prefix=_UPLOAD_TEMP_PREFIX, dir=str(work_root))
    os.close(fd)
    target = Path(raw_name)
    total = 0
    try:
        with target.open("wb") as out:
            while chunk := upload.file.read(_CHUNK_SIZE):
                total += len(chunk)
                if total > MAX_QUERY_IMAGE_BYTES:
                    raise MediaValidationError(
                        413,
                        "media_too_large",
                        "la imagen de consulta supera el límite de 10 MB",
                    )
                out.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def validate_query_media(path: Path) -> None:
    """Valida la media con la lógica del spike y mapea el rechazo a 415/400.

    Se reutiliza `xtrace_spike.security.validate_query_image` (fichero
    regular, ≤ 10 MB y firma MIME por cabecera — FR-002/ADR-0008; el tamaño
    ya quedó garantizado por streaming en `save_upload_to_temp`, así que el
    413 se decide en la subida). Para distinguir el rechazo por **firma**
    (415) del resto (400) se usa `detect_query_image_mime`, la misma función
    de firma del spike.

    Raises:
        MediaValidationError: `415 media_type_not_supported` si la firma MIME
            no es JPEG/PNG/WebP; `400 media_invalid` en el resto de rechazos.
    """
    try:
        validate_query_image(path)
    except QueryMediaError as exc:
        if detect_query_image_mime(path) is None:
            raise MediaValidationError(
                415,
                "media_type_not_supported",
                "la imagen de consulta debe ser JPEG, PNG o WebP (firma por cabecera)",
            ) from exc
        raise MediaValidationError(
            400,
            "media_invalid",
            "la imagen de consulta no es un fichero válido",
        ) from exc


def open_query_image_checked(path: Path) -> Image.Image:
    """Decodifica la imagen de consulta (PIL); contenido ilegible → 400.

    Reutiliza `xtrace_spike.security.open_query_image` (`load()` forzado):
    una imagen con firma válida pero contenido corrupto/truncado falla aquí,
    **antes de ejecutar la búsqueda** (SC-006, contracts §5 `media_corrupt`).
    El borrado de la media es responsabilidad del llamador (FR-003).
    """
    try:
        image: Image.Image = open_query_image(path)
        return image
    except (UnidentifiedImageError, OSError) as exc:
        raise MediaValidationError(
            400,
            "media_corrupt",
            "la imagen de consulta está corrupta o ilegible",
        ) from exc
