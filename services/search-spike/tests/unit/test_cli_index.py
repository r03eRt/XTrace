"""Tests del CLI `index` y `stats` (PR-011 · FR-017 · contracts §1).

Criterios verificables (contrato CLI §1 y tasks.md PR-011):
- `index --dataset <path> [--frames-per-video N=30] [--dedupe-threshold T]
  [--batch-size B=64]` emite por stdout el JSON exacto del contrato:
  `{"videos_indexed": n, "videos_failed": m, "frames": f, "vectors": v}`.
- `stats` reporta los totales del índice (videos/frames/vectors) en JSON
  estable (observabilidad, FR-017).
- Salida SIEMPRE en JSON por stdout; los logs van a stderr.
- Backend determinista sin DB ni Torch: sin `SUPABASE_DB_URL` se usan
  `InMemoryVectorStore` + `InMemoryVideoStateStore` y
  `FakeEmbeddingProvider` (decisión PR-011 documentada en cli.py).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fixtures import FFMPEG, make_corrupt_video, make_test_video
from xtrace_spike.cli import app, build_backend, resolve_embedding_provider
from xtrace_spike.embeddings.fake import FakeEmbeddingProvider
from xtrace_spike.embeddings.siglip_local import SiglipLocalProvider


@pytest.fixture(autouse=True)
def _isolated_cli_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Backend determinista por test: sin env de DB/proveedor y cache reseteada.

    Cada test prepara y limpia su estado (constitución §6): sin
    `SUPABASE_DB_URL` el backend es InMemory (no toca DB) y el proveedor
    por defecto es Fake (no toca Torch).
    """
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("XTRACE_EMBEDDING_PROVIDER", raising=False)
    build_backend.cache_clear()
    yield
    build_backend.cache_clear()


@pytest.fixture()
def dataset_dir(tmp_path: Path) -> Path:
    """Dataset sintético de 2 vídeos (testsrc2, PR-008): determinista, sin red."""
    if FFMPEG is None:
        pytest.skip("ffmpeg no está disponible en el PATH")
    root = tmp_path / "dataset"
    root.mkdir()
    make_test_video(root / "clip_a.mp4", duration_s=2.0, size="320x240", rate=25)
    make_test_video(root / "clip_b.mp4", duration_s=1.5, size="320x240", rate=25)
    return root


def test_index_emits_contract_json(dataset_dir: Path) -> None:
    """`index` sobre el fixture emite el JSON exacto del contrato CLI §1 (FR-017)."""
    result = CliRunner().invoke(
        app, ["index", "--dataset", str(dataset_dir), "--frames-per-video", "5"]
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert list(payload) == ["videos_indexed", "videos_failed", "frames", "vectors"]
    assert payload["videos_indexed"] == 2
    assert payload["videos_failed"] == 0
    assert 0 < payload["frames"] <= 10  # dedupe puede colapsar; nunca más que 2x5
    assert payload["vectors"] == payload["frames"]  # 1 vector por frame (FR-006)


def test_index_keeps_stdout_json_and_logs_to_stderr(dataset_dir: Path) -> None:
    """stdout es solo JSON; los logs de progreso van a stderr (contracts §1)."""
    result = CliRunner().invoke(app, ["index", "--dataset", str(dataset_dir)])
    assert result.exit_code == 0, result.stderr
    json.loads(result.stdout)  # stdout parsea como JSON puro
    assert "vídeos" in result.stderr


def test_index_accepts_batch_size_option(dataset_dir: Path) -> None:
    """`--batch-size` llega al pipeline (FR-005) sin romper el JSON."""
    result = CliRunner().invoke(app, ["index", "--dataset", str(dataset_dir), "--batch-size", "2"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["videos_indexed"] == 2


def test_stats_reports_totals_after_index(dataset_dir: Path) -> None:
    """`stats` tras `index` reporta los totales del índice (observabilidad)."""
    index_result = CliRunner().invoke(
        app, ["index", "--dataset", str(dataset_dir), "--frames-per-video", "4"]
    )
    assert index_result.exit_code == 0, index_result.stderr
    index_payload = json.loads(index_result.stdout)

    stats_result = CliRunner().invoke(app, ["stats"])
    assert stats_result.exit_code == 0, stats_result.stderr
    stats = json.loads(stats_result.stdout)
    assert stats["videos"] == index_payload["videos_indexed"]
    assert stats["frames"] == index_payload["frames"]
    assert stats["vectors"] == index_payload["vectors"]
    assert stats["backend"] == "in-memory"
    assert stats["embedding_provider"] == FakeEmbeddingProvider().model_id


def test_stats_reports_zeros_without_indexed_data() -> None:
    """`stats` sobre un índice vacío reporta ceros con JSON estable."""
    result = CliRunner().invoke(app, ["stats"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert list(payload) == ["videos", "frames", "vectors", "backend", "embedding_provider"]
    assert payload == {
        "videos": 0,
        "frames": 0,
        "vectors": 0,
        "backend": "in-memory",
        "embedding_provider": FakeEmbeddingProvider().model_id,
    }


def test_stats_reports_siglip_provider_model_id() -> None:
    """`stats --provider siglip` reporta el model_id sin cargar Torch (lazy, PR-005)."""
    result = CliRunner().invoke(app, ["stats", "--provider", "siglip"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["embedding_provider"] == "openclip-ViT-B-16-SigLIP-webli"


def test_index_missing_dataset_emits_json_error_exit_2(tmp_path: Path) -> None:
    """Dataset inexistente: JSON de error en stdout y exit 2 (uso inválido, FR-001)."""
    result = CliRunner().invoke(app, ["index", "--dataset", str(tmp_path / "nope")])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert "error" in payload
    assert payload["error_type"] == "DatasetError"


def test_index_invalid_frames_per_video_emits_json_error(dataset_dir: Path) -> None:
    """Config inválida (frames_per_video=0): JSON de error y exit 2 (FR-002)."""
    result = CliRunner().invoke(
        app, ["index", "--dataset", str(dataset_dir), "--frames-per-video", "0"]
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error_type"] == "ValueError"


def test_index_unknown_provider_emits_json_error(dataset_dir: Path) -> None:
    """Proveedor desconocido: JSON de error y exit 2 (decisión PR-011)."""
    result = CliRunner().invoke(
        app, ["index", "--dataset", str(dataset_dir), "--provider", "bogus"]
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert "bogus" in payload["error"]


def test_index_corrupt_video_counts_videos_failed(tmp_path: Path) -> None:
    """Vídeo corrupto: videos_failed=1 y el resto del dataset continúa (FR-001 US1 esc. 3)."""
    if FFMPEG is None:
        pytest.skip("ffmpeg no está disponible en el PATH")
    root = tmp_path / "dataset"
    root.mkdir()
    make_test_video(root / "ok.mp4", duration_s=2.0)
    make_corrupt_video(root / "broken.mp4")
    result = CliRunner().invoke(app, ["index", "--dataset", str(root), "--frames-per-video", "4"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["videos_indexed"] == 1
    assert payload["videos_failed"] == 1
    assert payload["frames"] > 0


def test_embedding_provider_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolución del proveedor: `--provider` y env XTRACE_EMBEDDING_PROVIDER (PR-011)."""
    assert isinstance(resolve_embedding_provider(None), FakeEmbeddingProvider)
    assert isinstance(resolve_embedding_provider("fake"), FakeEmbeddingProvider)
    assert isinstance(resolve_embedding_provider("siglip"), SiglipLocalProvider)

    monkeypatch.setenv("XTRACE_EMBEDDING_PROVIDER", "siglip")
    assert isinstance(resolve_embedding_provider(None), SiglipLocalProvider)
    # El flag explícito gana sobre la env.
    assert isinstance(resolve_embedding_provider("fake"), FakeEmbeddingProvider)

    monkeypatch.setenv("XTRACE_EMBEDDING_PROVIDER", "fake")
    assert isinstance(resolve_embedding_provider(None), FakeEmbeddingProvider)


def test_index_and_stats_help_exit_zero() -> None:
    """`index --help` y `stats --help` salen con 0 y documentan las opciones (FR-017)."""
    for command in (["index"], ["stats"]):
        result = CliRunner().invoke(app, [*command, "--help"])
        assert result.exit_code == 0, result.stderr
        assert "Usage:" in result.stdout
    index_help = CliRunner().invoke(app, ["index", "--help"])
    assert "--frames-per-video" in index_help.stdout
    assert "--dedupe-threshold" in index_help.stdout
    assert "--batch-size" in index_help.stdout
