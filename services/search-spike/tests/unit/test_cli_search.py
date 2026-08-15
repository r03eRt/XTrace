"""Tests del CLI `search` y `exclude` (PR-014 · FR-014/017/018 · SEC/privacidad
· ASSUMPTION-6 · spec §80 · ADR-0006/0008 · contracts §1).

Criterios verificables (tasks.md PR-014 · spec 001):
- `search --image <path> [--top-k K=10]` emite por stdout el JSON exacto del
  contrato CLI §1: `{"search_id": uuid, "processing_ms": n, "results": [...]}`
  con cada resultado `{video_id, local_ref, match_score, matching_frames,
  match_timestamp_ms, evidence: {visual, phash}}`.
- Validación de entrada (ASSUMPTION-6 · spec §80): rechazo por tamaño
  > 10 MB y por firma MIME no válida (exit 2, JSON de error; la media
  rechazada NO se borra).
- Borrado inmediato de la media de consulta (FR-018 · ADR-0006): el fichero
  pasado con `--image` se borra tras procesar la búsqueda, tanto si termina
  con éxito como si falla el procesamiento (try/finally, FR-009 · SC-006).
- `exclude --video <id>` (FR-014): el vídeo deja de aparecer en resultados.

Tests deterministas sin DB ni Torch (decisión PR-011): backend InMemory
(sin SUPABASE_DB_URL) + FakeEmbeddingProvider. El índice se siembra
directamente en el store in-memory con embeddings/pHash calculados con los
mismos componentes que usa la CLI (Fake D=768 + compute_phash), de modo que
la búsqueda es exacta (distancia 0, evidencia pHash 1). El test end-to-end
(index CLI → search CLI) se salta si ffmpeg no está disponible.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from tests.fixtures import make_test_video
from xtrace_spike.cli import app, build_backend
from xtrace_spike.embeddings.fake import FakeEmbeddingProvider
from xtrace_spike.hashing.phash import compute_phash
from xtrace_spike.indexing import video_id_for
from xtrace_spike.ingest.dataset import scan_dataset
from xtrace_spike.ingest.frames import (
    DEFAULT_FRAMES_PER_VIDEO,
    DEFAULT_SCALE_WIDTH,
    extract_frames,
)
from xtrace_spike.security import (
    MAX_QUERY_IMAGE_BYTES,
    QueryMediaContext,
    copy_query_to_secure_temp,
)
from xtrace_spike.vectorstore.base import FrameRecord

#: Dimensión del provider fake por defecto de la CLI (D fijada por PR-005).
_EMBEDDING_DIMENSION = 768

#: Timestamp del frame sembrado (paridad con el contrato CLI §1).
_SEEDED_TIMESTAMP_MS = 94_000


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


def _make_query_image(path: Path, *, size: tuple[int, int] = (64, 48)) -> Path:
    """PNG determinista generado con PIL (sin aleatoriedad; SC-007 mindset).

    Mismos parámetros → mismos píxeles → mismo embedding (Fake determinista)
    y mismo pHash, de modo que los tests no dependen de fixtures binarios.
    """
    width, height = size
    image = Image.new("RGB", (width, height))
    for y in range(height):
        for x in range(width):
            image.putpixel((x, y), (x * 4 % 256, y * 4 % 256, (x + y) % 256))
    image.save(path, format="PNG")
    return path


def _rgb_of(path: Path) -> Image.Image:
    """Carga la imagen en memoria y la normaliza a RGB (paridad con search)."""
    with Image.open(path) as image:
        image.load()
        return image.convert("RGB")


def _embedding_of(path: Path) -> list[float]:
    """Embedding determinista de la imagen (mismo proveedor que la CLI)."""
    rgb = _rgb_of(path)
    vector = FakeEmbeddingProvider(dimension=_EMBEDDING_DIMENSION).embed_images([rgb])[0]
    return [float(value) for value in vector]


def _seed_index(query_path: Path, *, video_id: str | None = None) -> tuple[str, str]:
    """Siembra el índice in-memory con un frame idéntico a la imagen de consulta.

    El embedding y el pHash se calculan con los mismos componentes que usa la
    CLI (FakeEmbeddingProvider D=768 y compute_phash), de modo que `search`
    encuentra el frame con distancia 0.0, match_score 1.0 y evidencia pHash 1.0
    (FR-010 · FR-012 · FR-013).
    """
    backend = build_backend()
    resolved_video = video_id or str(uuid.uuid4())
    frame_id = str(uuid.uuid4())
    rgb = _rgb_of(query_path)
    record = FrameRecord(
        frame_id=frame_id,
        video_id=resolved_video,
        timestamp_ms=_SEEDED_TIMESTAMP_MS,
        phash=compute_phash(rgb),
        embedding=_embedding_of(query_path),
    )
    asyncio.run(backend.store.upsert_frames([record]))
    return resolved_video, frame_id


def _require_ffmpeg() -> None:
    """Skip cuando ffmpeg/ffprobe no están disponibles (p. ej. CI sin FFmpeg)."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe no están disponibles en este entorno")


# ---------------------------------------------------------------------------
# FR-017 · contracts §1: JSON estable de `search`
# ---------------------------------------------------------------------------


def test_search_emits_contract_json(tmp_path: Path) -> None:
    """`search` emite el JSON exacto del contrato CLI §1 (FR-017)."""
    query = _make_query_image(tmp_path / "query.png")
    video_id, _ = _seed_index(query)

    result = CliRunner().invoke(app, ["search", "--image", str(query)])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert list(payload) == ["search_id", "processing_ms", "results"]
    parsed_id = uuid.UUID(payload["search_id"])
    assert parsed_id.version == 4  # search_id: uuid4 (contracts §1)
    assert isinstance(payload["processing_ms"], int)
    assert payload["processing_ms"] >= 0
    assert len(payload["results"]) == 1

    top = payload["results"][0]
    assert list(top) == [
        "video_id",
        "local_ref",
        "match_score",
        "matching_frames",
        "match_timestamp_ms",
        "evidence",
    ]
    assert top["video_id"] == video_id
    # In-memory: el store no expone local_ref → null (decisión PR-014 documentada).
    assert top["local_ref"] is None
    assert top["match_score"] == 1.0  # distancia 0 + frames 1/1 + pHash idéntico
    assert top["matching_frames"] == 1
    assert top["match_timestamp_ms"] == _SEEDED_TIMESTAMP_MS
    assert list(top["evidence"]) == ["visual", "phash"]
    assert top["evidence"]["visual"] == 1.0
    assert top["evidence"]["phash"] == 1.0


def test_search_empty_index_returns_empty_results(tmp_path: Path) -> None:
    """Índice vacío: `results: []` con JSON estable (sin falsos positivos)."""
    query = _make_query_image(tmp_path / "query.png")
    result = CliRunner().invoke(app, ["search", "--image", str(query)])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert list(payload) == ["search_id", "processing_ms", "results"]
    assert payload["results"] == []


def test_search_top_k_reaches_ann(tmp_path: Path) -> None:
    """`--top-k` se propaga al ANN sin romper el JSON (contracts §1)."""
    query = _make_query_image(tmp_path / "query.png")
    video_id, _ = _seed_index(query)
    result = CliRunner().invoke(app, ["search", "--image", str(query), "--top-k", "5"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [item["video_id"] for item in payload["results"]] == [video_id]


def test_search_invalid_top_k_emits_json_error_and_keeps_file(tmp_path: Path) -> None:
    """`--top-k 0`: error de uso (exit 2) y la media NO se borra (no procesada)."""
    query = _make_query_image(tmp_path / "query.png")
    result = CliRunner().invoke(app, ["search", "--image", str(query), "--top-k", "0"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error_type"] == "ValueError"
    assert query.exists()


# ---------------------------------------------------------------------------
# SEC · ASSUMPTION-6 · spec §80: validación de entrada (MIME/firma y tamaño)
# ---------------------------------------------------------------------------


def test_search_rejects_invalid_mime_signature(tmp_path: Path) -> None:
    """Firma MIME no válida: JSON de error, exit 2 y la media no se toca (ADR-0008)."""
    bogus = tmp_path / "fake.png"
    bogus.write_bytes(b"XTrace fake image - not really an image" + bytes(range(16)))
    result = CliRunner().invoke(app, ["search", "--image", str(bogus)])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error_type"] == "QueryMediaError"
    assert "MIME" in payload["error"]
    assert bogus.exists()  # media rechazada: sin borrado (FR-018 aplica al procesado)


def test_search_rejects_oversize_image(tmp_path: Path) -> None:
    """Imagen > 10 MB: rechazo por tamaño con JSON de error (spec §80 · ASSUMPTION-6)."""
    big = tmp_path / "big.png"
    big.write_bytes(bytes.fromhex("89504e470d0a1a0a") + bytes(MAX_QUERY_IMAGE_BYTES))
    assert big.stat().st_size > MAX_QUERY_IMAGE_BYTES
    result = CliRunner().invoke(app, ["search", "--image", str(big)])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error_type"] == "QueryMediaError"
    assert "10 MB" in payload["error"]
    assert big.exists()  # media rechazada: sin borrado


def test_search_rejects_missing_image(tmp_path: Path) -> None:
    """Imagen inexistente: JSON de error y exit 2 (uso inválido)."""
    missing = tmp_path / "nope.png"
    result = CliRunner().invoke(app, ["search", "--image", str(missing)])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error_type"] == "QueryMediaError"


# ---------------------------------------------------------------------------
# FR-018 · ADR-0006: borrado inmediato de la media de consulta
# ---------------------------------------------------------------------------


def test_search_deletes_query_media_after_search(tmp_path: Path) -> None:
    """FR-018/ADR-0006: la media de consulta se borra tras procesar la búsqueda."""
    query = _make_query_image(tmp_path / "query.png")
    _seed_index(query)
    assert query.exists()

    result = CliRunner().invoke(app, ["search", "--image", str(query)])
    assert result.exit_code == 0, result.stderr
    assert not query.exists()
    # Sin artefactos en el directorio del test (SC-006: no quedan temporales).
    assert list(tmp_path.iterdir()) == []


def test_search_deletes_query_media_even_on_processing_failure(tmp_path: Path) -> None:
    """Try/finally: aunque el procesamiento falle, la media se borra (FR-009/SC-006).

    Firma PNG válida (pasa la validación MIME) pero contenido ilegible: la
    decodificación PIL falla dentro del procesamiento y el context manager de
    la media garantiza el borrado igualmente.
    """
    bogus = tmp_path / "truncated.png"
    bogus.write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"garbage garbage garbage garbage")
    result = CliRunner().invoke(app, ["search", "--image", str(bogus)])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert "error" in payload
    assert not bogus.exists()


def test_query_media_context_uses_secure_temp_and_cleans_up(tmp_path: Path) -> None:
    """Rutas temporales seguras: copia 0600 en el work_root y borrado en finally."""
    query = _make_query_image(tmp_path / "query.png")
    with QueryMediaContext.from_file(query, work_root=tmp_path) as media:
        assert media.secure_copy is not None
        assert media.secure_copy.exists()
        assert media.secure_copy != query  # copia, nunca la ruta original
        assert media.secure_copy.stat().st_mode & 0o777 == 0o600  # solo propietario
        assert media.secure_copy.parent == tmp_path
    assert not query.exists()  # borrado inmediato al salir (FR-018)
    assert media.secure_copy is None or not media.secure_copy.exists()


def test_copy_query_to_secure_temp_named_in_work_root(tmp_path: Path) -> None:
    """La copia temporal se crea con el prefijo del módulo (localizable, ADR-0006)."""
    query = _make_query_image(tmp_path / "query.png")
    copy = copy_query_to_secure_temp(query, tmp_path)
    try:
        assert copy.name.startswith("xtrace-query-")
        assert copy.parent == tmp_path
        assert copy.read_bytes() == query.read_bytes()
    finally:
        copy.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# FR-014: `exclude` oculta el vídeo del search
# ---------------------------------------------------------------------------


def test_exclude_hides_video_from_search(tmp_path: Path) -> None:
    """FR-014 vía CLI: tras `exclude --video`, el vídeo deja de aparecer."""
    query_a = _make_query_image(tmp_path / "a.png")
    video_id, _ = _seed_index(query_a)

    before = CliRunner().invoke(app, ["search", "--image", str(query_a)])
    assert before.exit_code == 0, before.stderr
    assert json.loads(before.stdout)["results"][0]["video_id"] == video_id

    excluded = CliRunner().invoke(app, ["exclude", "--video", video_id])
    assert excluded.exit_code == 0, excluded.stderr
    assert json.loads(excluded.stdout) == {
        "video_id": video_id,
        "excluded": True,
        "changed": True,
    }

    # Nueva imagen de consulta (la anterior se borró al buscar, FR-018).
    query_b = _make_query_image(tmp_path / "b.png")
    after = CliRunner().invoke(app, ["search", "--image", str(query_b)])
    assert after.exit_code == 0, after.stderr
    assert json.loads(after.stdout)["results"] == []


def test_exclude_unknown_video_is_not_an_error() -> None:
    """`exclude` con un vídeo no indexado: JSON estable sin error (idempotente)."""
    result = CliRunner().invoke(app, ["exclude", "--video", "no-such-video"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["excluded"] is True


# ---------------------------------------------------------------------------
# Cadena completa vía CLI: index → search (fixtures ffmpeg, PR-008)
# ---------------------------------------------------------------------------


def test_search_end_to_end_after_cli_index(tmp_path: Path) -> None:
    """Cadena real por la CLI: index → search encuentra el vídeo y borra la media."""
    _require_ffmpeg()
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    make_test_video(dataset / "clip_a.mp4", duration_s=2.0)
    make_test_video(dataset / "clip_b.mp4", duration_s=1.5, size="160x120", rate=30)

    index_result = CliRunner().invoke(app, ["index", "--dataset", str(dataset)])
    assert index_result.exit_code == 0, index_result.stderr
    indexed = json.loads(index_result.stdout)
    assert indexed["videos_indexed"] == 2

    target = scan_dataset(dataset)[0]
    target_id = video_id_for(target.local_ref)
    # Captura exacta: frame 0 re-extraído con los mismos parámetros que el index.
    with extract_frames(
        target,
        work_root=tmp_path / "work",
        frames_per_video=DEFAULT_FRAMES_PER_VIDEO,
        scale_width=DEFAULT_SCALE_WIDTH,
    ) as extraction:
        query = tmp_path / "query.png"
        shutil.copyfile(extraction.frames[0].path, query)

    # top_k=1: solo el frame exacto (distancia 0) entra en el ANN -> score 1.0.
    search_result = CliRunner().invoke(app, ["search", "--image", str(query), "--top-k", "1"])
    assert search_result.exit_code == 0, search_result.stderr
    payload = json.loads(search_result.stdout)
    assert len(payload["results"]) == 1
    assert payload["results"][0]["video_id"] == target_id
    assert payload["results"][0]["match_score"] == 1.0
    assert payload["results"][0]["evidence"]["phash"] == 1.0
    assert not query.exists()  # borrado inmediato tras la búsqueda (FR-018)


def test_search_and_exclude_help_exit_zero() -> None:
    """`search --help` y `exclude --help` salen con 0 (FR-017)."""
    for command in (["search"], ["exclude"]):
        result = CliRunner().invoke(app, [*command, "--help"])
        assert result.exit_code == 0, result.stderr
        assert "Usage:" in result.stdout
    search_help = CliRunner().invoke(app, ["search", "--help"])
    assert "--image" in search_help.stdout
    assert "--top-k" in search_help.stdout
