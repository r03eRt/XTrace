"""CLI interna del spike de búsqueda visual (FR-017 · ADR-0008 · contracts §1).

Comandos (contratos en `specs/001-visual-search-spike/contracts/README.md` §1):
- `index` (PR-011): ingiere el dataset local a través del pipeline (PR-010)
  y emite `{"videos_indexed": n, "videos_failed": m, "frames": f, "vectors": v}`.
- `stats` (PR-011): métricas del índice (videos/frames/vectors + backend).
- `search` (PR-014): búsqueda por imagen con validación de entrada
  (fichero regular, ≤ 10 MB y firma MIME, spec §80 · ASSUMPTION-6) y
  borrado inmediato de la media de consulta (FR-018 · ADR-0006); salida del
  contrato CLI §1 (search_id/processing_ms/results).
- `benchmark` (PR-016): ejecuta el benchmark (FR-016) sobre el dataset de
  casos generado (PR-015, manifest.json) y emite el informe reproducible del
  contrato CLI §1 (top1/5/10, FPR negativas, latencia p50/p95, frames/vídeo,
  tamaño del índice, throughput) para evaluar SC-001/SC-002/SC-003/SC-007.
- `exclude` (PR-014): excluye un vídeo del índice (FR-014).

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
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from PIL import Image, UnidentifiedImageError

from xtrace_spike import __version__
from xtrace_spike.benchmark import load_manifest
from xtrace_spike.benchmark.runner import BenchmarkRunner
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
from xtrace_spike.repo import DATABASE_URL_ENV, PgRepo, parse_uuid
from xtrace_spike.search import DEFAULT_TOP_K, ImageSearch, ImageSearchResult
from xtrace_spike.search.ranking import DEFAULT_MIN_SCORE, rank_candidates
from xtrace_spike.security import (
    QueryMediaContext,
    QueryMediaError,
    open_query_image,
    validate_query_image,
)
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
    """Punto de entrada de la CLI; los subcomandos index/stats (PR-011) y
    search/exclude (PR-014) se registran como comandos de la app."""


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


def _resolve_frame_phashes(backend: CliBackend, result: ImageSearchResult) -> dict[str, int]:
    """pHash de los mejores frames para la evidencia pHash del ranking (FR-013).

    Postgres: PgRepo.get_frame_phashes (pHash persistido en frames.phash,
    decisión PR-013). In-memory: InMemoryVectorStore.get_frame expone el
    pHash real del frame (PR-003 · FIX-phash). Cualquier otro backend
    (inexistente hoy) devuelve {} → evidencia pHash neutra (PR-013).
    """
    frame_ids = [candidate.best_frame_id for candidate in result.candidates]
    if isinstance(backend.store, PgVectorStore):
        return asyncio.run(PgRepo().get_frame_phashes(frame_ids))
    if isinstance(backend.store, InMemoryVectorStore):
        phashes: dict[str, int] = {}
        for frame_id in frame_ids:
            record = asyncio.run(backend.store.get_frame(frame_id))
            if record is not None:
                phashes[frame_id] = record["phash"]
        return phashes
    return {}


async def _fetch_local_refs(video_ids: Sequence[str]) -> dict[str, str | None]:
    """local_ref de los vídeos desde public.videos (contracts §1 · PR-010).

    El valor real de local_ref lo fija el pipeline de indexación (PR-010);
    aquí se lee la columna para el JSON de search. Los video_ids provienen
    de los resultados rankeados (sus vídeos existen por FK, PR-007).
    """
    video_uuids = [parse_uuid(video_id, "video_id") for video_id in video_ids]
    if not video_uuids:
        return {}
    async with await PgRepo().connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "select id::text, local_ref from public.videos where id = any(%s::uuid[])",
                ([str(video_uuid) for video_uuid in video_uuids],),
            )
            rows = await cur.fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def _resolve_local_refs(backend: CliBackend, video_ids: Sequence[str]) -> dict[str, str | None]:
    """local_ref por vídeo para el JSON de search (contracts §1).

    Postgres: _fetch_local_refs vía PgRepo (el local_ref real del vídeo).
    In-memory: el store y el estado de vídeo no exponen el local_ref (sin
    getter en el contrato VideoStateStore), así que los resultados llevan
    local_ref: null (decisión PR-014, documentada en el handoff).
    """
    if not isinstance(backend.store, PgVectorStore):
        return {}
    return asyncio.run(_fetch_local_refs(video_ids))


def _open_query_image(path: Path) -> Image.Image:
    """Abre la imagen de consulta; contenido ilegible → error de validación (exit 2).

    open_query_image (security.py) fuerza la decodificación (load()). Una
    imagen con firma válida pero contenido ilegible se trata como media de
    entrada inválida (ADR-0008): UnidentifiedImageError (PIL) se traduce a
    QueryMediaError; el resto de OSErrors (I/O real) se propaga como fallo
    de ejecución (exit 1). El borrado de la media ya está garantizado por el
    context manager del llamador (FR-018).
    """
    try:
        return open_query_image(path)
    except UnidentifiedImageError as exc:
        raise QueryMediaError(f"la imagen de consulta no se puede decodificar: {exc}") from exc


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


@app.command(help="Busca un vídeo por imagen (FR-010/017/018, contracts §1).")
def search(
    image: Annotated[
        Path,
        typer.Option(
            "--image",
            help="Imagen de consulta (JPEG/PNG/WebP ≤ 10 MB; se borra tras procesar, FR-018).",
        ),
    ],
    top_k: Annotated[
        int,
        typer.Option("--top-k", help="Nº de frames candidatos del ANN (contracts §1)."),
    ] = DEFAULT_TOP_K,
    min_score: Annotated[
        float,
        typer.Option(
            "--min-score",
            help="Umbral de match en [0, 1]; descarta resultados débiles (SC-002).",
        ),
    ] = DEFAULT_MIN_SCORE,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Proveedor de embeddings: fake (default) o siglip."),
    ] = None,
) -> None:
    """Busca por imagen y devuelve los vídeos rankeados (FR-010/012/013/017).

    Cadena completa (PR-012/013/014): validate_query_image (fichero regular,
    ≤ 10 MB y firma MIME, spec §80) → copia a temporal seguro → ImageSearch
    (normalizar → pHash → embed → ANN → agrupar) → rank_candidates
    (match_score, timestamp y evidencia, FR-012/013; vídeos excluidos fuera,
    FR-014) → JSON del contrato CLI §1.

    La media de consulta se borra inmediatamente tras procesar (FR-018 ·
    ADR-0006), garantizado en try/finally aunque la búsqueda falle (FR-009 ·
    SC-006); una media rechazada por validación no se toca. local_ref llega
    desde la DB (backend postgres) o queda null en in-memory (documentado).
    """
    started = time.perf_counter()
    try:
        validate_query_image(image)
        backend = build_backend()
        embeddings = resolve_embedding_provider(provider)
        searcher = ImageSearch(store=backend.store, embeddings=embeddings, top_k=top_k)
        if not 0.0 <= min_score <= 1.0:
            raise ValueError(f"min_score debe estar en [0, 1] (recibido {min_score})")
        with QueryMediaContext.from_file(image, work_root=WORK_ROOT) as media:
            assert media.secure_copy is not None
            query_image = _open_query_image(media.secure_copy)
            typer.echo(
                f"search: backend={backend.label} proveedor={embeddings.model_id} "
                f"top_k={top_k} min_score={min_score} imagen={image}",
                err=True,
            )
            result = asyncio.run(searcher.search_image(query_image))
            frame_phashes = _resolve_frame_phashes(backend, result)
            ranked = rank_candidates(result, frame_phashes=frame_phashes, min_score=min_score)
            local_refs = _resolve_local_refs(backend, [item.video_id for item in ranked])
            results = [
                {
                    "video_id": item.video_id,
                    "local_ref": local_refs.get(item.video_id),
                    "match_score": item.match_score,
                    "matching_frames": item.matching_frames,
                    "match_timestamp_ms": item.match_timestamp_ms,
                    "evidence": {
                        "visual": item.visual_similarity,
                        "phash": item.phash_score,
                    },
                }
                for item in ranked
            ]
        _emit_json(
            {
                "search_id": str(uuid.uuid4()),
                "processing_ms": round((time.perf_counter() - started) * 1000),
                "results": results,
            }
        )
    except QueryMediaError as exc:
        _fail(str(exc), type(exc).__name__, exit_code=2)
    except ValueError as exc:
        _fail(str(exc), type(exc).__name__, exit_code=2)
    except Exception as exc:
        _fail(str(exc), type(exc).__name__, exit_code=1)


def _benchmark_manifest_path(cases: Path) -> Path:
    """Manifest del dataset de benchmark: directorio (manifest.json) o fichero directo.

    El comando `benchmark` acepta el directorio de casos generado por
    PR-015 (contiene manifest.json) o la ruta del manifest en sí; un path
    inexistente o un directorio sin manifest es un error de uso (exit 2).
    """
    if cases.is_dir():
        manifest = cases / "manifest.json"
        if not manifest.is_file():
            raise ValueError(f"no se encuentra manifest.json en el directorio de casos '{cases}'")
        return manifest
    if cases.is_file():
        return cases
    raise ValueError(f"la ruta de casos '{cases}' no existe ni es un directorio")


@app.command(help="Ejecuta el benchmark y emite el informe del contrato CLI §1 (FR-016/017).")
def benchmark(
    cases: Annotated[
        Path,
        typer.Option(
            "--cases",
            help="Directorio del dataset de benchmark (manifest.json, PR-015) o el manifest.",
        ),
    ],
    top_k: Annotated[
        int,
        typer.Option("--top-k", help="Nº de frames candidatos del ANN (contracts §1)."),
    ] = DEFAULT_TOP_K,
    min_score: Annotated[
        float,
        typer.Option(
            "--min-score",
            help="Umbral de match en [0, 1]; FPR de negativas (SC-002, PR-013).",
        ),
    ] = DEFAULT_MIN_SCORE,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Proveedor de embeddings: fake (default) o siglip."),
    ] = None,
) -> None:
    """Ejecuta el benchmark y emite el informe reproducible del contrato CLI §1 (FR-016).

    Cadena (PR-015/016): carga los casos del manifest (load_manifest), los
    ejecuta contra el índice del backend con la cadena de búsqueda real
    (ImageSearch + rank_candidates, PR-012/013) y agrega el informe:
    top1/top5/top10 sobre positivos (SC-001), FPR de negativas con el umbral
    `min_score` (SC-002), latencia p50/p95 (SC-003), frames/vídeo y tamaño
    del índice desde VectorStore.stats(), y throughput de embeddings medido
    durante el run. Reproducible (SC-007): sin timestamps ni aleatoriedad
    (salvo latencia/throughput, que fluctúan y se reportan con precisión
    estable). Las consultas del dataset NO se borran (no son media del
    operador; FR-018 aplica a la CLI search, PR-014).
    """
    try:
        backend = build_backend()
        embeddings = resolve_embedding_provider(provider)
        runner = BenchmarkRunner(
            store=backend.store,
            embeddings=embeddings,
            top_k=top_k,
            min_score=min_score,
        )
        manifest = _benchmark_manifest_path(cases)
        benchmark_cases = load_manifest(manifest)
        if not benchmark_cases:
            raise ValueError(f"el manifest '{manifest}' no contiene casos")
        typer.echo(
            f"benchmark: backend={backend.label} proveedor={embeddings.model_id} "
            f"casos={len(benchmark_cases)} top_k={top_k} min_score={min_score} "
            f"manifest={manifest}",
            err=True,
        )
        report = asyncio.run(runner.run(benchmark_cases))
        _emit_json(report.to_dict())
    except ValueError as exc:
        _fail(str(exc), type(exc).__name__, exit_code=2)
    except Exception as exc:
        _fail(str(exc), type(exc).__name__, exit_code=1)


@app.command(help="Excluye un vídeo del índice (FR-014, contracts §1).")
def exclude(
    video: Annotated[
        str,
        typer.Option("--video", help="video_id del vídeo a excluir (UUID, contracts §2)."),
    ],
) -> None:
    """Marca el vídeo como excluido: deja de aparecer en search (FR-014).

    Postgres: PgRepo.exclude (solo videos.excluded = true, sin borrar
    registros — FR-014). In-memory: InMemoryVectorStore.delete_video
    (elimina los frames del vídeo y lo marca excluido; equivalente en
    memoria, PR-003 — el índice in-memory es volátil por diseño, ADR-0006).
    Salida JSON: {"video_id", "excluded", "changed"} (el JSON de este
    comando no está en el contrato CLI §1; se documenta en el handoff PR-014).
    """
    try:
        backend = build_backend()
        if isinstance(backend.store, PgVectorStore):
            changed = asyncio.run(PgRepo().exclude(video))
            _emit_json({"video_id": video, "excluded": True, "changed": changed})
        else:
            asyncio.run(backend.store.delete_video(video))
            _emit_json({"video_id": video, "excluded": True, "changed": True})
    except ValueError as exc:
        _fail(str(exc), type(exc).__name__, exit_code=2)
    except Exception as exc:
        _fail(str(exc), type(exc).__name__, exit_code=1)
