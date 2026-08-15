"""Validación de entrada y manejo seguro de temporales de la media de consulta
(PR-014 · SEC/privacidad · ASSUMPTION-6 · spec §80 · ADR-0006/0008 · FR-018/009).

La CLI `search` (PR-014) recibe una imagen de consulta del operador: media
sensible (privacidad) que el sistema no debe conservar. Este módulo concentra
la validación de entrada y el ciclo de vida seguro de la media:

- **Validación de entrada** (spec §80 · ASSUMPTION-6 · ADR-0008): la imagen
  debe ser un fichero regular de tamaño ≤ `MAX_QUERY_IMAGE_BYTES` (10 MB) y
  con una firma MIME reconocida (JPEG/PNG/WebP). La firma se comprueba por
  magic bytes de la cabecera, no por extensión ni Content-Type declarado:
  un fichero renombrado o con extensión falsa se rechaza.
- **Rutas temporales seguras** (ADR-0006 · FR-009): la media se copia a un
  temporal propio antes de procesarla (`copy_query_to_secure_temp`): nombre
  aleatorio (mkstemp), permisos 0600 (solo propietario) y dentro del
  `work_root` del llamador. Así el procesado nunca vuelve a tocar la ruta
  original tras la validación (defensa ante modificación entre validación y
  uso) y el fichero del sistema nace con permisos mínimos.
- **Borrado inmediato** (FR-018 · ASSUMPTION-6 · ADR-0006): la media de
  consulta se borra inmediatamente tras procesar la búsqueda.
  `QueryMediaContext` es un context manager que garantiza en `finally`
  (vía `__exit__`) el borrado de la copia temporal y del fichero original,
  tanto si la búsqueda termina con éxito como si falla (FR-009 · SC-006).
  El borrado del original solo ocurre tras superar la validación: la media
  rechazada no se toca (documentado en el handoff PR-014).

La decodificación PIL (`open_query_image`) fuerza la lectura de los píxeles
(load()) para detectar contenido ilegible lo antes posible, antes de entrar
en el pipeline de búsqueda.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Literal

from PIL import Image

logger = logging.getLogger(__name__)

#: Límite de tamaño de la imagen de consulta: ≤ 10 MB (ASSUMPTION-6 · spec §80).
MAX_QUERY_IMAGE_BYTES: int = 10 * 1024 * 1024

#: MIME permitidos para la imagen de consulta (firma verificada por magic bytes).
ALLOWED_QUERY_IMAGE_MIME = frozenset({"image/jpeg", "image/png", "image/webp"})

_JPEG_MAGIC = bytes.fromhex("ffd8ff")
_PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")
_WEBP_RIFF_TAG = b"RIFF"
_WEBP_MAGIC = b"WEBP"

#: Prefijo de los temporales de la media de consulta (localizable en work_root).
_QUERY_TEMP_PREFIX = "xtrace-query-"


class QueryMediaError(ValueError):
    """Media de consulta rechazada por la validación de entrada (exit 2, ADR-0008)."""


def detect_query_image_mime(path: str | Path) -> str | None:
    """Firma MIME de la imagen por magic bytes de la cabecera, o None si no aplica.

    Comprueba los primeros bytes del fichero (JPEG `FF D8 FF`, PNG
    `89 50 4E 47 0D 0A 1A 0A`, WebP `RIFF....WEBP`) sin depender de la
    extensión ni del nombre (ADR-0008). Un fichero ilegible devuelve None
    (la validación lo rechaza con un mensaje de firma, no de I/O).
    """
    target = Path(path)
    try:
        with target.open("rb") as handle:
            head = handle.read(12)
    except OSError:
        return None
    if head.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if head.startswith(_PNG_MAGIC):
        return "image/png"
    if head[:4] == _WEBP_RIFF_TAG and head[8:12] == _WEBP_MAGIC:
        return "image/webp"
    return None


def validate_query_image(path: str | Path) -> None:
    """Valida la imagen de consulta: fichero regular, ≤ 10 MB y firma MIME.

    Orden de comprobaciones: existencia/fichero regular → tamaño → firma
    MIME. En caso de rechazo la media NO se modifica: el borrado inmediato
    (FR-018) aplica solo a la media aceptada y procesada.

    Raises:
        QueryMediaError: si el fichero no existe, no es regular, supera
            `MAX_QUERY_IMAGE_BYTES` o su firma MIME no está permitida.
    """
    target = Path(path)
    if not target.is_file():
        raise QueryMediaError(
            f"la imagen de consulta no existe o no es un fichero regular: {target}"
        )
    size = target.stat().st_size
    if size > MAX_QUERY_IMAGE_BYTES:
        raise QueryMediaError(
            f"la imagen de consulta supera el límite de 10 MB "
            f"({size} bytes > {MAX_QUERY_IMAGE_BYTES} bytes)"
        )
    if detect_query_image_mime(target) is None:
        permitted = ", ".join(sorted(ALLOWED_QUERY_IMAGE_MIME))
        raise QueryMediaError(
            f"firma MIME no reconocida en {target.name} (permitidas: {permitted})"
        )


def copy_query_to_secure_temp(path: str | Path, work_root: str | Path) -> Path:
    """Copia la media de consulta a un temporal seguro (0600, nombre aleatorio).

    El temporal se crea con `tempfile.mkstemp` dentro de `work_root` (prefijo
    `xtrace-query-`): nombre impredecible y permisos 0600 (solo el
    propietario). El procesado posterior trabaja sobre esta copia controlada
    y nunca vuelve a abrir la ruta original (rutas temporales seguras,
    ADR-0006 · FR-009).

    Raises:
        OSError: si el fichero no se puede copiar (p. ej. work_root
            inexistente o permisos insuficientes).
    """
    source = Path(path)
    fd, raw_name = tempfile.mkstemp(prefix=_QUERY_TEMP_PREFIX, dir=str(work_root))
    os.close(fd)
    target = Path(raw_name)
    try:
        shutil.copyfile(source, target)
    except BaseException:
        target.unlink(missing_ok=True)  # no dejar temporales a medias (SC-006)
        raise
    return target


def open_query_image(path: str | Path) -> Image.Image:
    """Abre y decodifica la imagen de consulta (PIL), detectando contenido ilegible.

    `load()` fuerza la decodificación completa de los píxeles: una imagen
    truncada o corrupta (con firma válida pero contenido ilegible) falla aquí,
    antes de entrar en el pipeline de búsqueda. El llamador es responsable del
    borrado de la media (FR-018).
    """
    with Image.open(path) as image:
        image.load()
        return image


@dataclass
class QueryMediaContext:
    """Media de consulta bajo gestión de borrado inmediato (FR-018 · ADR-0006).

    Uso (CLI search de PR-014):

        with QueryMediaContext.from_file(image, work_root=WORK_ROOT) as media:
            query_image = open_query_image(media.secure_copy)
            ...

    `__exit__` borra SIEMPRE (try/finally, FR-009 · SC-006) la copia temporal
    y la media original, tanto si el bloque termina con éxito como con
    excepción. El llamador debe validar la media con `validate_query_image`
    ANTES de entrar en el contexto: la media rechazada no se borra.
    Un fallo de borrado se registra como warning y no enmascara el resultado
    del bloque (el error primario, si lo hay, se propaga).
    """

    original: Path
    secure_copy: Path | None = None

    @classmethod
    def from_file(cls, original: str | Path, *, work_root: str | Path) -> QueryMediaContext:
        """Crea el contexto copiando la media a un temporal seguro (0600)."""
        return cls(
            original=Path(original),
            secure_copy=copy_query_to_secure_temp(original, work_root),
        )

    def __enter__(self) -> QueryMediaContext:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        self.cleanup()
        return False  # no tragar excepciones: el error primario se propaga

    def cleanup(self) -> None:
        """Borra la copia temporal y la media original (FR-018), sin propagar."""
        if self.secure_copy is not None:
            self._unlink(self.secure_copy)
            self.secure_copy = None
        self._unlink(self.original)

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("no se pudo borrar la media de consulta: %s", path)
