"""Ingesta del dataset local de vídeos (FR-001/FR-002 · ADR-0006).

- dataset.py: carga del dataset local y local_ref estable (FR-001).
- frames.py: FFprobe + FFmpeg para extraer frames representativos (FR-002)
  con temporales seguros (cleanup garantizado, FR-009).
"""


class IngestError(Exception):
    """Error controlado de la ingesta (dataset o extracción de frames).

    Los fallos de ingesta (dataset inválido, fichero corrupto, FFmpeg/FFprobe
    con error) se propagan como subclases de esta excepción para que el
    pipeline pueda marcar el vídeo como failed sin abortar el resto del
    dataset (FR-001, acceptance scenario 3 de US1).
    """
