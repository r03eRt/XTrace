"""CLI interna del spike de búsqueda visual (FR-017 · ADR-0008 · contracts §1).

Comandos (contratos en `specs/001-visual-search-spike/contracts/README.md` §1):
- `index` (PR-011): ingiere el dataset local a través del pipeline (PR-010)
  y emite `{"videos_indexed": n, "videos_failed": m, "frames": f, "vectors": v}`.
- `stats` (PR-011): métricas del índice (videos/frames/vectors + backend).

Contrato de salida (contracts §1): SIEMPRE JSON por stdout; los logs de
progreso van a stderr. Códigos de salida: 0 = éxito; 2 = error de
uso/validación; 1 = fallo en ejecución.

Decisión PR-011 (backend y embeddings configurables, documentada también en
docs/handoffs/PR-011.md):
- Backend: si `SUPABASE_DB_URL` está definida se usa `PgVectorStore` +
  `PgVideoStateStore` (producción, PR-007/PR-010); si no,
  `InMemoryVectorStore` + `InMemoryVideoStateStore` (determinista, sin DB;
  PR-003). El backend in-memory se comparte por proceso (lru_cache) para que
  `index` -> `stats` reporten los mismos totales en una sesión; entre
  procesos no persiste (índice volátil por diseño, ADR-0006).
- Embeddings: `FakeEmbeddingProvider` (PR-002) por defecto;
  `SiglipLocalProvider` (PR-005) con `--provider siglip` o la env
  `XTRACE_EMBEDDING_PROVIDER=siglip`. El flag explícito gana sobre la env.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from xtrace_spike import __version__
from xtrace_spike.embeddings.fake import FakeEmbeddingProvider
from xtrace_spike.embeddings.provider import EmbeddingProvider
from xtrace_spike.embeddings.siglip_local import SiglipLocalProvider
from xtrace_spike.indexing import (
    IndexingConfig,
    IndexingPipeline,
    InMemoryVideoStateStore,
    PgVideoStateStore,
    VideoStateStore,
)
from xtrace_spike.ingest.dataset import DatasetError, scan_dataset
from xtrace_spike.ingest.dedupe import DEFAULT_HAMMING_THRESHOLD
from xtrace_spike.ingest.frames import DEFAULT_FRAMES_PER_VIDEO
from xtrace_spike.repo import DATABASE_URL_ENV
from xtrace_spike.vectorstore.base import VectorStore
from xtrace_spike.vectorstore.in_memory import InMemoryVectorStore
from xtrace_spike.vectorstore.pgvector import PgVectorStore

#: Variable de entorno que selecciona el proveedor de embeddings (decisión PR-011).
EMBEDDING_PROVIDER_ENV = "XTRACE_EMBEDDING_PROVIDER"

#: Tamaño de lote por defecto (paridad con IndexingConfig de PR-010 y contracts §1).
DEFAULT_BATCH_SIZE: int = 64

#: Raíz de temporales de la CLI; el pipeline crea y limpia los suyos (FR-009).
WORK_ROOT: Path = Path(tempfile.gettempdir())


def _version_callback(value: bool) -> None:
    """Imprime la versión y sale cuando se pasa `--version`."""
    if value:
        typer.echo(f"xtrace-spike {__version__}")
        raise typer.Exit()


def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Muestra la versión y sale.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Punto de entrada de la CLI; los subcomandos index/stats llegan en PR-011."""


app = typer.Typer(
    name="xtrace-spike",
    callback=main,
    help="CLI interna del spike de búsqueda visual de XTrace (validación, no producto).",
    no_args_is_help=True,
)


@dataclass(frozen=True, slots=True)
class CliBackend:
    """Backend del índice resuelto por la CLI (store + estado + etiqueta).

    Atributos:
        store: VectorStore del índice (contracts §2).
        video_states: estado por vídeo (FR-007).
        label: etiqueta estable para el JSON de stats ("in-memory" | "postgres").
    """

    store: VectorStore
    video_states: VideoStateStore
    label: str


@functools.lru_cache(maxsize=1)
def build_backend() -> CliBackend:
    """Backend del índice según `SUPABASE_DB_URL` (decisión PR-011).

    - Env definida -> `PgVectorStore` + `PgVideoStateStore` (producción;
      el DSN lo resuelve `PgRepo`, repo.py PR-007).
    - Env ausente -> `InMemoryVectorStore` + `InMemoryVideoStateStore`
      (determinista, sin DB; PR-003).

    La caché por proceso permite que `index` y `stats` compartan el índice
    in-memory en una misma sesión (tests y uso interactivo); entre procesos
    distintos el índice in-memory no persiste (por diseño, ADR-0006).
    """
    if os.environ.get(DATABASE_URL_ENV):
        return CliBackend(store=PgVectorStore(), video_states=PgVideoStateStore(), label="postgres")
    return CliBackend(
        store=InMemoryVectorStore(), video_states=InMemoryVideoStateStore(), label="in-memory"
    )


def resolve_embedding_provider(provider: str | None) -> EmbeddingProvider:
    """Proveedor de embeddings (decisión PR-011): flag `--provider` > env > default.

    - `siglip` -> `SiglipLocalProvider` (real, PR-005; Torch se importa lazy).
    - `fake` (default) -> `FakeEmbeddingProvider` (determinista, PR-002).
    - La env `XTRACE_EMBEDDING_PROVIDER` aplica solo si no se pasa el flag.

    Raises:
        ValueError: proveedor desconocido (la CLI lo traduce a exit 2).
    """
    chosen = provider or os.environ.get(EMBEDDING_PROVIDER_ENV, "").strip().lower() or "fake"
    if chosen == "fake":
        return FakeEmbeddingProvider(dimension=768)  # D fijada por PR-005
    if chosen == "siglip":
        return SiglipLocalProvider()
    raise ValueError(f"proveedor de embeddings desconocido: {chosen!r} (opciones: fake, siglip)")


def _emit_json(payload: dict[str, Any]) -> None:
    """Escribe el payload como JSON estable por stdout (contracts §1)."""
    typer.echo(json.dumps(payload))


def _fail(message: str, error_type: str, exit_code: int) -> NoReturn:
    """JSON de error por stdout y salida con el código indicado.

    Códigos (documentados en el handoff PR-011): 2 = error de uso/validación
    (dataset inválido, configuración inválida, proveedor desconocido);
    1 = fallo en ejecución (p. ej. DB inaccesible).
    """
    _emit_json({"error": message, "error_type": error_type})
    raise typer.Exit(code=exit_code)


@app.command(help="Ingiere el dataset local y construye el índice (FR-017, contracts §1).")
def index(
    dataset: Annotated[
        Path,
        typer.Option("--dataset", help="Directorio raíz del dataset local de vídeos (FR-001)."),
    ],
    frames_per_video: Annotated[
        int,
        typer.Option("--frames-per-video", help="Frames representativos por vídeo (FR-002)."),
    ] = DEFAULT_FRAMES_PER_VIDEO,
    dedupe_threshold: Annotated[
        int,
        typer.Option("--dedupe-threshold", help="Umbral de Hamming del dedupe (FR-003)."),
    ] = DEFAULT_HAMMING_THRESHOLD,
    batch_size: Annotated[
        int,
        typer.Option("--batch-size", help="Tamaño de lote del embedding (FR-005)."),
    ] = DEFAULT_BATCH_SIZE,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Proveedor de embeddings: fake (default) o siglip."),
    ] = None,
) -> None:
    """Indexa el dataset (idempotente, FR-008) y emite el JSON del contrato CLI §1.

    Salida por stdout: {"videos_indexed": n, "videos_failed": m, "frames": f,
    "vectors": v}. Un vídeo que falla se cuenta en `videos_failed` y el
    resto del dataset continúa (FR-001 US1 esc. 3); los logs van a stderr.
    """
    try:
        backend = build_backend()
        embeddings = resolve_embedding_provider(provider)
        config = IndexingConfig(
            frames_per_video=frames_per_video,
            dedupe_threshold=dedupe_threshold,
            batch_size=batch_size,
        )
        videos = scan_dataset(dataset)
        typer.echo(
            f"index: backend={backend.label} proveedor={embeddings.model_id} "
            f"vídeos={len(videos)} dataset={dataset}",
            err=True,
        )
        report = asyncio.run(
            IndexingPipeline(
                store=backend.store,
                embeddings=embeddings,
                video_states=backend.video_states,
                config=config,
            ).index_dataset(videos, work_root=WORK_ROOT)
        )
        _emit_json(
            {
                "videos_indexed": report.videos_indexed,
                "videos_failed": report.videos_failed,
                "frames": report.frames,
                "vectors": report.vectors,
            }
        )
    except DatasetError as exc:
        _fail(str(exc), type(exc).__name__, exit_code=2)
    except ValueError as exc:
        _fail(str(exc), type(exc).__name__, exit_code=2)
    except Exception as exc:
        _fail(str(exc), type(exc).__name__, exit_code=1)


@app.command(help="Métricas del índice en JSON estable (observabilidad, FR-017).")
def stats(
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Proveedor de embeddings reportado: fake o siglip."),
    ] = None,
) -> None:
    """Reporta los totales del índice (videos/frames/vectors) y su backend en JSON.

    `videos`/frames/vectors provienen de `VectorStore.stats()` (contracts
    §2; `videos` = vídeos con ≥ 1 frame indexado); `backend` y
    `embedding_provider` (model_id) aportan observabilidad al índice.
    """
    try:
        backend = build_backend()
        embeddings = resolve_embedding_provider(provider)
        index_stats = asyncio.run(backend.store.stats())
        _emit_json(
            {
                "videos": index_stats["videos"],
                "frames": index_stats["frames"],
                "vectors": index_stats["vectors"],
                "backend": backend.label,
                "embedding_provider": embeddings.model_id,
            }
        )
    except ValueError as exc:
        _fail(str(exc), type(exc).__name__, exit_code=2)
    except Exception as exc:
        _fail(str(exc), type(exc).__name__, exit_code=1)
