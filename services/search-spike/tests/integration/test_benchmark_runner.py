"""Tests de integración del runner de benchmark y del CLI `benchmark` (PR-016
· FR-016 · SC-001/002/003/007 · contracts §1).

Criterios verificables (tasks.md PR-016 · spec 001):
- Informe completo del contrato CLI §1: cases/top1/top5/top10/FPR de
  negativas/latencia p50/p95/frames por vídeo/tamaño del índice/throughput
  (FR-016 · US4 esc. 1), con las 9 claves exactas del contrato.
- Métricas coherentes en fixture: 0 <= top1 <= top5 <= top10 <= 1, top5 == 1.0
  cuando el pool completo entra en el ANN (4 vídeos <= Top-5), frames/vídeo
  == VectorStore.stats(), latencia p50/p95 enteros con p50 <= p95, FPR según
  el umbral min_score (SC-002) y evaluación de la puerta SC-001 (Top-5 >= 80%).
- Reproducibilidad (SC-007): dos ejecuciones -> idénticas las métricas de
  calidad (top1/5/10, FPR, frames/vídeo, tamaño del índice); latencia y
  throughput fluctúan y solo se exige presencia y rango (precisión estable
  documentada en el handoff).
- CLI `benchmark` (FR-017): JSON exacto del contrato por stdout con exit 0;
  errores de uso (ruta de casos inválida, top_k <= 0, min_score fuera de
  [0, 1]) con JSON de error y exit 2.

Tests deterministas sin DB ni Torch (decisión PR-011): backend InMemory (sin
SUPABASE_DB_URL) + FakeEmbeddingProvider. El índice se siembra desde los
frames sintéticos del fixture de benchmark (PR-015, Pillow + numpy, sin
ffmpeg) con los mismos componentes que usa el pipeline (video_id_for +
FakeEmbeddingProvider + compute_phash): la variante "exact" (copia bit a bit)
se recupera con distancia 0 y match_score 1.0, y el vídeo esperado se
identifica por `video_id_for(expected_video_ref)` (FR-008). El test
end-to-end por la CLI (index real -> casos desde frames extraídos ->
benchmark) se salta si ffmpeg no está disponible (patrón PR-014).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from typer.testing import CliRunner

from tests.fixtures import make_test_video
from tests.fixtures.benchmark import make_benchmark_frames
from xtrace_spike.benchmark import (
    DEFAULT_SEED,
    POSITIVE_VARIANTS,
    BenchmarkError,
    SourceFrame,
    generate_benchmark_dataset,
    scan_frames_root,
)
from xtrace_spike.benchmark.runner import BenchmarkRunner
from xtrace_spike.cli import app, build_backend
from xtrace_spike.embeddings.fake import FakeEmbeddingProvider
from xtrace_spike.hashing.phash import compute_phash
from xtrace_spike.indexing import frame_id_for, video_id_for
from xtrace_spike.ingest.dataset import scan_dataset
from xtrace_spike.ingest.frames import (
    DEFAULT_FRAMES_PER_VIDEO,
    DEFAULT_SCALE_WIDTH,
    extract_frames,
)
from xtrace_spike.vectorstore.base import FrameRecord
from xtrace_spike.vectorstore.in_memory import InMemoryVectorStore

#: Dimensión del provider fake en los tests del runner (rápida; el runner no
#: depende de D). La CLI usa la D por defecto de PR-005 (768).
_EMBEDDING_DIMENSION = 64
_CLI_EMBEDDING_DIMENSION = 768

#: Tolerancia de redondeo del informe (top-K con 4 decimales, SC-007):
#: pytest.approx no admite operadores relacionales, así que las cotas
#: inferiores ">= ratio" se comparan numéricamente restando esta tolerancia
#: (5e-5 de redondeo + margen de 5e-5).
_RATIO_TOLERANCE: float = 1e-4

#: Claves exactas del informe del contrato CLI §1 (orden estable).
CONTRACT_KEYS = [
    "cases",
    "top1",
    "top5",
    "top10",
    "false_positive_rate_negatives",
    "latency_ms",
    "frames_per_video_avg",
    "index_size_bytes",
    "embedding_throughput_fps",
]


def _run(coro: Any) -> Any:
    """Ejecuta una corrutina del dominio (sin pytest-asyncio, estilo PR-003)."""
    return asyncio.run(coro)


def _rgb_of(path: Path) -> Image.Image:
    """Carga la imagen en memoria y la normaliza a RGB (paridad con search)."""
    with Image.open(path) as image:
        image.load()
        return image.convert("RGB")


def _seed_store(
    store: InMemoryVectorStore,
    pool: tuple[SourceFrame, ...],
    *,
    dimension: int,
) -> InMemoryVectorStore:
    """Siembra el índice con los frames del pool (mismos componentes que PR-010).

    video_id = video_id_for(video_ref) (FR-008, uuid5 determinista del
    local_ref) y frame_id = frame_id_for(video_id, seq): las mismas claves
    que produce el pipeline de indexación, de modo que el vídeo esperado de
    cada caso (video_ref del frame de origen) coincide con el video_id de los
    resultados rankeados.
    """
    provider = FakeEmbeddingProvider(dimension=dimension)
    records: list[FrameRecord] = []
    for seq, frame in enumerate(pool):
        rgb = _rgb_of(frame.path)
        records.append(
            FrameRecord(
                frame_id=frame_id_for(video_id_for(frame.video_ref), seq),
                video_id=video_id_for(frame.video_ref),
                timestamp_ms=None,
                phash=compute_phash(rgb),
                embedding=[float(value) for value in provider.embed_images([rgb])[0]],
            )
        )
    _run(store.upsert_frames(records))
    return store


def _runner(store: InMemoryVectorStore, **kwargs: Any) -> BenchmarkRunner:
    """Runner con el provider fake (dimensión del test)."""
    return BenchmarkRunner(
        store=store,
        embeddings=FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION),
        **kwargs,
    )


def _generate_cases(
    pool: tuple[SourceFrame, ...],
    out_dir: Path,
    *,
    seed: int = DEFAULT_SEED,
    cases_per_variant: int = 3,
    negative_cases: int = 2,
    variants: tuple[str, ...] = POSITIVE_VARIANTS,
) -> list[Any]:
    """Genera el dataset de casos (PR-015) y devuelve los casos cargados.

    El seed se propaga al generador (PR-015): mismo seed + mismos frames ->
    mismos casos (SC-007).
    """
    return list(
        generate_benchmark_dataset(
            pool,
            out_dir,
            seed=seed,
            cases_per_variant=cases_per_variant,
            negative_cases=negative_cases,
            variants=variants,
        ).cases
    )


@pytest.fixture(scope="session")
def frame_pool(tmp_path_factory: pytest.TempPathFactory) -> tuple[SourceFrame, ...]:
    """Pool compartido de 40 frames sintéticos (4 vídeos x 10), solo lectura."""
    return make_benchmark_frames(tmp_path_factory.mktemp("benchmark-pool"))


@pytest.fixture(autouse=True)
def _isolated_cli_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Backend determinista por test: sin env de DB/proveedor y cache reseteada."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("XTRACE_EMBEDDING_PROVIDER", raising=False)
    build_backend.cache_clear()
    yield
    build_backend.cache_clear()


# ---------------------------------------------------------------------------
# FR-016 · contracts §1: informe completo con las 9 claves del contrato
# ---------------------------------------------------------------------------


def test_report_contract_json_and_coherent_metrics(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """Informe del contrato CLI §1 con métricas coherentes en fixture (FR-016)."""
    store = _seed_store(InMemoryVectorStore(), frame_pool, dimension=_EMBEDDING_DIMENSION)
    cases = _generate_cases(frame_pool, tmp_path / "out")

    report = _run(_runner(store, top_k=40).run(cases))
    payload = report.to_dict()

    # Contrato exacto: 9 claves en el orden del contrato (SC-007: sin extras).
    assert list(payload) == CONTRACT_KEYS
    assert payload["cases"] == 6 * 3 + 2  # 18 positivos + 2 negativas
    for key in ("top1", "top5", "top10", "false_positive_rate_negatives"):
        value = payload[key]
        assert isinstance(value, float)
        assert 0.0 <= value <= 1.0
    # Monotonía de los top-K sobre positivos.
    assert payload["top1"] <= payload["top5"] <= payload["top10"]
    # Latencia: enteros en ms, p50 <= p95 (SC-003).
    assert list(payload["latency_ms"]) == ["p50", "p95"]
    assert isinstance(payload["latency_ms"]["p50"], int)
    assert isinstance(payload["latency_ms"]["p95"], int)
    assert 0 <= payload["latency_ms"]["p50"] <= payload["latency_ms"]["p95"]
    # frames/vídeo desde VectorStore.stats(): 40 frames / 4 vídeos.
    assert payload["frames_per_video_avg"] == 10.0
    # El tamaño del índice solo si el store lo expone en stats (hoy ninguno).
    assert payload["index_size_bytes"] == 0
    assert payload["embedding_throughput_fps"] >= 0.0


def test_empty_index_report(frame_pool: tuple[SourceFrame, ...], tmp_path: Path) -> None:
    """Índice vacío: top-K 0.0, FPR 0.0 y frames/vídeo 0.0 (guardas, FR-016)."""
    cases = _generate_cases(frame_pool, tmp_path / "out", cases_per_variant=2, negative_cases=2)
    report = _run(_runner(InMemoryVectorStore(), top_k=40).run(cases))
    payload = report.to_dict()

    assert payload["cases"] == 6 * 2 + 2
    assert payload["top1"] == payload["top5"] == payload["top10"] == 0.0
    assert payload["false_positive_rate_negatives"] == 0.0
    assert payload["frames_per_video_avg"] == 0.0
    assert payload["latency_ms"]["p50"] >= 0
    assert payload["latency_ms"]["p95"] >= 0


def test_no_negatives_fpr_zero(frame_pool: tuple[SourceFrame, ...], tmp_path: Path) -> None:
    """Sin casos negativos: FPR 0.0 (guarda de división) y positivos contados."""
    store = _seed_store(InMemoryVectorStore(), frame_pool, dimension=_EMBEDDING_DIMENSION)
    cases = _generate_cases(frame_pool, tmp_path / "out", negative_cases=0)

    report = _run(_runner(store, top_k=40).run(cases))
    assert report.cases == 6 * 3
    assert report.false_positive_rate_negatives == 0.0
    assert report.top1 >= 0.0


# ---------------------------------------------------------------------------
# SC-001 · puerta de decisión: Top-5 >= 80% sobre positivos (fixture)
# ---------------------------------------------------------------------------


def test_fixture_top5_meets_sc001_gate(frame_pool: tuple[SourceFrame, ...], tmp_path: Path) -> None:
    """SC-001 en fixture: con el pool completo visible, Top-5 == 1.0 (gate OK).

    top_k=40 >= 40 vectores -> todos los frames son hits -> los 4 vídeos del
    pool aparecen en el ranking, así que el vídeo esperado siempre está en
    Top-5 (4 <= 5) y Top-10. La puerta SC-001 (Top-5 >= 80%) se evalúa y
    pasa; la variante "exact" garantiza además Top-1 (distancia 0).
    """
    store = _seed_store(InMemoryVectorStore(), frame_pool, dimension=_EMBEDDING_DIMENSION)
    cases = _generate_cases(frame_pool, tmp_path / "out")

    report = _run(_runner(store, top_k=40).run(cases))

    assert report.top5 == 1.0
    assert report.top10 == 1.0
    assert report.meets_sc001_gate() is True
    # Las 3 consultas "exact" (píxeles idénticos al frame indexado) ganan
    # seguro (distancia 0 -> match_score 1.0); el resto de variantes puede
    # acertar o no, pero nunca reduce el mínimo garantizado.
    exact_ratio = 3 / 18
    assert report.top1 >= exact_ratio - _RATIO_TOLERANCE
    assert report.top1 <= report.top5


def test_exact_variant_always_top1_and_sc002_threshold(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """SC-002: con un umbral alto, las negativas quedan bajo él (FPR 0.0).

    Solo variante "exact" (match_score 1.0 con el frame idéntico): con
    min_score=0.9 los 3 positivos pasan y quedan Top-1; las 2 negativas
    sintéticas (ajenas al índice) no superan 0.9 -> FPR 0.0 y la puerta
    SC-002 (>= 90% de negativas bajo el umbral) se cumple.
    """
    store = _seed_store(InMemoryVectorStore(), frame_pool, dimension=_EMBEDDING_DIMENSION)
    cases = _generate_cases(frame_pool, tmp_path / "out", variants=("exact",))

    report = _run(_runner(store, top_k=40, min_score=0.9).run(cases))

    assert report.cases == 3 + 2
    assert report.top1 == 1.0
    assert report.top5 == 1.0
    assert report.top10 == 1.0
    assert report.false_positive_rate_negatives == 0.0
    assert report.meets_sc002_gate() is True


def test_negative_fpr_with_default_threshold(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """SC-002: con el umbral por defecto (0.0) toda negativa con resultados cuenta.

    El FPR mide cuántas negativas superan el umbral configurado (PR-013):
    con min_score=0.0 ningún resultado se descarta, así que las 2 negativas
    producen ranking y el FPR es 1.0 (semántica del umbral, no de las
    imágenes). El umbral real lo fija el operador en la evaluación SC-002.
    """
    store = _seed_store(InMemoryVectorStore(), frame_pool, dimension=_EMBEDDING_DIMENSION)
    cases = _generate_cases(frame_pool, tmp_path / "out")

    report = _run(_runner(store, top_k=40).run(cases))

    assert report.false_positive_rate_negatives == 1.0
    assert report.meets_sc002_gate() is False


# ---------------------------------------------------------------------------
# SC-007 · reproducibilidad: dos ejecuciones, mismas métricas de calidad
# ---------------------------------------------------------------------------


def test_report_is_reproducible_sc007(frame_pool: tuple[SourceFrame, ...], tmp_path: Path) -> None:
    """SC-007: dos ejecuciones -> mismas métricas deterministas del informe.

    top1/top5/top10, FPR, frames/vídeo y tamaño del índice son idénticos
    (mismos casos + mismo índice, sin timestamps ni aleatoriedad). La
    latencia p50/p95 y el throughput dependen del reloj: solo se exige que
    estén presentes y en rango (fluctuación tolerada, documentada).
    """
    store = _seed_store(InMemoryVectorStore(), frame_pool, dimension=_EMBEDDING_DIMENSION)
    cases = _generate_cases(frame_pool, tmp_path / "out", seed=11)

    first = _run(_runner(store, top_k=40).run(cases))
    second = _run(_runner(store, top_k=40).run(cases))

    deterministic = [
        "cases",
        "top1",
        "top5",
        "top10",
        "false_positive_rate_negatives",
        "frames_per_video_avg",
        "index_size_bytes",
    ]
    for key in deterministic:
        assert first.to_dict()[key] == second.to_dict()[key], key
    for report in (first, second):
        assert isinstance(report.latency_ms["p50"], int)
        assert isinstance(report.latency_ms["p95"], int)
        assert report.latency_ms["p50"] >= 0
        assert report.embedding_throughput_fps >= 0.0
    # El payload es exactamente el del contrato (sin ids/timestamps extra).
    assert list(first.to_dict()) == CONTRACT_KEYS


# ---------------------------------------------------------------------------
# Errores controlados del runner (dataset corrupto, configuración inválida)
# ---------------------------------------------------------------------------


def test_missing_query_image_raises_benchmark_error(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """Imagen de consulta ausente: BenchmarkError controlado (exit 2 en CLI)."""
    store = _seed_store(InMemoryVectorStore(), frame_pool, dimension=_EMBEDDING_DIMENSION)
    cases = _generate_cases(frame_pool, tmp_path / "out", cases_per_variant=2)
    cases[0].query_image_path.unlink()

    with pytest.raises(BenchmarkError, match="decodificar"):
        _run(_runner(store, top_k=40).run(cases))


def test_runner_validates_configuration(frame_pool: tuple[SourceFrame, ...]) -> None:
    """Configuración inválida -> ValueError (top_k <= 0, min_score fuera de rango)."""
    store = InMemoryVectorStore()
    embeddings = FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION)
    with pytest.raises(ValueError, match="top_k"):
        BenchmarkRunner(store=store, embeddings=embeddings, top_k=0)
    with pytest.raises(ValueError, match="min_score"):
        BenchmarkRunner(store=store, embeddings=embeddings, min_score=1.5)
    with pytest.raises(ValueError, match="min_score"):
        BenchmarkRunner(store=store, embeddings=embeddings, min_score=-0.1)


# ---------------------------------------------------------------------------
# FR-017 · CLI `benchmark`: JSON del contrato por stdout
# ---------------------------------------------------------------------------


def test_benchmark_cli_emits_contract_json(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """El comando benchmark emite el JSON exacto del contrato CLI §1 (exit 0)."""
    backend = build_backend()
    _seed_store(backend.store, frame_pool, dimension=_CLI_EMBEDDING_DIMENSION)  # type: ignore[arg-type]
    out = tmp_path / "out"
    _generate_cases(frame_pool, out, seed=3)

    result = CliRunner().invoke(app, ["benchmark", "--cases", str(out)])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert list(payload) == CONTRACT_KEYS
    assert payload["cases"] == 20
    assert 0.0 <= payload["top1"] <= payload["top5"] <= payload["top10"] <= 1.0
    assert payload["frames_per_video_avg"] == 10.0
    assert isinstance(payload["latency_ms"]["p50"], int)
    assert isinstance(payload["latency_ms"]["p95"], int)

    # El manifest se puede pasar directamente (fichero) o como directorio.
    direct = CliRunner().invoke(app, ["benchmark", "--cases", str(out / "manifest.json")])
    assert direct.exit_code == 0, direct.stderr
    assert json.loads(direct.stdout)["cases"] == 20


def test_benchmark_cli_top_k_reaches_ann_and_gate(
    frame_pool: tuple[SourceFrame, ...], tmp_path: Path
) -> None:
    """--top-k se propaga al ANN; con k=40 el fixture cumple SC-001 (top5=1.0)."""
    backend = build_backend()
    _seed_store(backend.store, frame_pool, dimension=_CLI_EMBEDDING_DIMENSION)  # type: ignore[arg-type]
    out = tmp_path / "out"
    _generate_cases(frame_pool, out)

    result = CliRunner().invoke(app, ["benchmark", "--cases", str(out), "--top-k", "40"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["top5"] == 1.0
    assert payload["top10"] == 1.0
    assert payload["top1"] >= 3 / 18 - _RATIO_TOLERANCE


def test_benchmark_cli_errors_exit_two(frame_pool: tuple[SourceFrame, ...], tmp_path: Path) -> None:
    """Errores de uso del CLI benchmark: JSON de error y exit 2 (ADR-0008)."""
    backend = build_backend()
    _seed_store(backend.store, frame_pool, dimension=_CLI_EMBEDDING_DIMENSION)  # type: ignore[arg-type]
    out = tmp_path / "out"
    _generate_cases(frame_pool, out)

    missing = CliRunner().invoke(app, ["benchmark", "--cases", str(tmp_path / "nope")])
    assert missing.exit_code == 2
    assert json.loads(missing.stdout)["error_type"] == "ValueError"

    bad_k = CliRunner().invoke(app, ["benchmark", "--cases", str(out), "--top-k", "0"])
    assert bad_k.exit_code == 2
    assert json.loads(bad_k.stdout)["error_type"] == "ValueError"

    bad_score = CliRunner().invoke(app, ["benchmark", "--cases", str(out), "--min-score", "1.5"])
    assert bad_score.exit_code == 2
    assert json.loads(bad_score.stdout)["error_type"] == "ValueError"


def test_benchmark_cli_help_exits_zero() -> None:
    """--help del comando benchmark sale con 0 y documenta las opciones (FR-017)."""
    result = CliRunner().invoke(app, ["benchmark", "--help"])
    assert result.exit_code == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "--cases" in result.stdout
    assert "--top-k" in result.stdout
    assert "--min-score" in result.stdout


# ---------------------------------------------------------------------------
# Cadena completa por la CLI: index real -> casos desde frames -> benchmark
# ---------------------------------------------------------------------------


def _require_ffmpeg() -> None:
    """Skip cuando ffmpeg/ffprobe no están disponibles (p. ej. CI sin FFmpeg)."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe no están disponibles en este entorno")


def test_benchmark_end_to_end_after_cli_index(tmp_path: Path) -> None:
    """Cadena real: CLI index -> casos desde frames extraídos -> CLI benchmark.

    El local_ref de cada vídeo (ruta relativa POSIX, PR-008) es el video_ref
    de su directorio de frames (layout de scan_frames_root, PR-015), de modo
    que video_id_for(expected_video_ref) coincide con los vídeos del índice
    indexados por la CLI (FR-008). Con top_k >= nº total de vectores, los 2
    vídeos entran en el ranking y el Top-5 == 1.0 (SC-001 evaluada).
    """
    _require_ffmpeg()
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    make_test_video(dataset / "clip_a.mp4", duration_s=2.0)
    make_test_video(dataset / "clip_b.mp4", duration_s=1.5, size="160x120", rate=30)

    indexed = CliRunner().invoke(app, ["index", "--dataset", str(dataset)])
    assert indexed.exit_code == 0, indexed.stderr
    assert json.loads(indexed.stdout)["videos_indexed"] == 2

    # Frames de origen para el generador (PR-015): <frames_root>/<local_ref>/
    frames_root = tmp_path / "frames"
    videos = scan_dataset(dataset)
    for video in videos:
        with extract_frames(
            video,
            work_root=tmp_path / "work",
            frames_per_video=DEFAULT_FRAMES_PER_VIDEO,
            scale_width=DEFAULT_SCALE_WIDTH,
        ) as extraction:
            out_dir = frames_root / video.local_ref
            out_dir.mkdir(parents=True, exist_ok=True)
            for frame in extraction.frames:
                shutil.copyfile(frame.path, out_dir / frame.path.name)

    cases_out = tmp_path / "cases"
    generate_benchmark_dataset(
        scan_frames_root(frames_root),
        cases_out,
        cases_per_variant=2,
        negative_cases=1,
    )

    result = CliRunner().invoke(app, ["benchmark", "--cases", str(cases_out), "--top-k", "100"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["cases"] == 6 * 2 + 1
    assert payload["top5"] == 1.0
    assert payload["top10"] == 1.0
    assert payload["top1"] >= 2 / 12 - _RATIO_TOLERANCE
    assert payload["frames_per_video_avg"] >= 10.0
