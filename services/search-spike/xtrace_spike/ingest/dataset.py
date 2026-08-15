"""Carga del dataset local de vídeos (FR-001 · ADR-0006).

Recorre un directorio local (sin red ni fuentes externas) y produce entradas
de vídeo con un "local_ref" **estable**: ruta relativa al root del dataset en
formato POSIX, independiente del cwd del proceso. Es el identificador local
estable de la entidad Video (spec: "identificador local estable, referencia
al fichero de origen").

La validación de contenido (corrupción, streams) no es responsabilidad del
loader: los ficheros se listan por extensión y el fallo de un vídeo concreto
se detecta en frames.py (FFprobe) como error controlado, de forma que el
resto del dataset continúa (FR-001, acceptance scenario 3 de US1).
"""

from __future__ import annotations

import os
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from xtrace_spike.ingest import IngestError

DEFAULT_VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".webm",
        ".m4v",
        ".ts",
        ".flv",
        ".wmv",
        ".mpg",
        ".mpeg",
        ".3gp",
    }
)


class DatasetError(IngestError):
    """Error controlado del dataset local (root inexistente o inválido)."""


@dataclass(frozen=True)
class DatasetVideo:
    """Vídeo del dataset local listo para ingesta (FR-001).

    Atributos:
        local_ref: identificador local estable — ruta relativa POSIX al root
            del dataset (sin resolver symlinks); único por vídeo.
        path: ruta absoluta al fichero de origen.
        size_bytes: tamaño del fichero en bytes.
    """

    local_ref: str
    path: Path
    size_bytes: int


def scan_dataset(
    root: str | Path,
    *,
    allowed_extensions: Collection[str] = DEFAULT_VIDEO_EXTENSIONS,
) -> tuple[DatasetVideo, ...]:
    """Recorre root recursivamente y devuelve los vídeos soportados.

    - Orden determinista (orden lexicográfico de local_ref).
    - Ignora directorios y ficheros ocultos (prefijo punto).
    - Extensión comparada en minúsculas (".MOV" == ".mov").
    - No sigue symlinks de directorios (os.walk, followlinks=False).

    Raises:
        DatasetError: si root no existe o no es un directorio.
    """
    root_path = Path(root).absolute()
    if not root_path.is_dir():
        raise DatasetError(f"el dataset '{root}' no existe o no es un directorio")

    allowed = {ext.lower() for ext in allowed_extensions}
    videos: list[DatasetVideo] = []
    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            file_path = Path(dirpath) / name
            if file_path.suffix.lower() not in allowed:
                continue
            videos.append(
                DatasetVideo(
                    local_ref=file_path.relative_to(root_path).as_posix(),
                    path=file_path.absolute(),
                    size_bytes=file_path.stat().st_size,
                )
            )
    videos.sort(key=lambda video: video.local_ref)
    return tuple(videos)
