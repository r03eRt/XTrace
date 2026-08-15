"""Tests de ingesta del dataset local y extracción de frames (PR-008).

Criterios verificables (tasks.md PR-008 · spec 001):
- Extracción de frames sobre un fixture pequeño (FR-002).
- Nº de frames coherente con la configuración (FR-002).
- Ficheros corruptos / sin stream de vídeo → error controlado (FR-001,
  acceptance scenario 3 de US1).
- Temporales listados para cleanup y cleanup garantizado (FR-009 · ADR-0006 ·
  SC-006).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from tests.fixtures import make_audio_video, make_corrupt_video, make_test_video
from xtrace_spike.ingest import IngestError
from xtrace_spike.ingest.dataset import DatasetError, DatasetVideo, scan_dataset
from xtrace_spike.ingest.frames import ProbeError, extract_frames, probe_video


def _require_ffmpeg() -> None:
    """Skip cuando ffmpeg/ffprobe no están disponibles (p. ej. CI sin FFmpeg)."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe no están disponibles en este entorno")


def _dataset_video(path: Path) -> DatasetVideo:
    return DatasetVideo(local_ref=path.name, path=path, size_bytes=path.stat().st_size)


# ---------------------------------------------------------------------------
# FR-001 · dataset loader (no requiere FFmpeg)
# ---------------------------------------------------------------------------


def test_scan_dataset_lists_only_supported_extensions(tmp_path: Path) -> None:
    """FR-001: solo se listan vídeos con extensión soportada, recursivo y sin ocultos."""
    clips = tmp_path / "clips"
    (clips / "sub").mkdir(parents=True)
    (clips / ".hidden").mkdir()
    (clips / "a.mp4").write_bytes(b"x")
    (clips / "b.MOV").write_bytes(b"x")
    (clips / "sub" / "c.mkv").write_bytes(b"x")
    (clips / "notes.txt").write_bytes(b"x")
    (clips / "cover.jpg").write_bytes(b"x")
    (clips / ".hidden.mp4").write_bytes(b"x")
    (clips / ".hidden" / "d.mp4").write_bytes(b"x")

    videos = scan_dataset(clips)

    assert [v.local_ref for v in videos] == ["a.mp4", "b.MOV", "sub/c.mkv"]
    assert all(v.path.is_absolute() for v in videos)
    assert all(v.size_bytes == 1 for v in videos)


def test_scan_dataset_local_ref_is_stable_regardless_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-001: local_ref es relativo al root y no depende del cwd del proceso."""
    media = tmp_path / "media"
    (media / "sub").mkdir(parents=True)
    (media / "sub" / "v1.mp4").write_bytes(b"x")

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    first = scan_dataset(media)
    monkeypatch.chdir(tmp_path)
    second = scan_dataset(media)

    assert first == second
    assert [v.local_ref for v in first] == ["sub/v1.mp4"]


def test_scan_dataset_missing_or_file_root_raises(tmp_path: Path) -> None:
    """FR-001: root inexistente o no-directorio → DatasetError (error controlado)."""
    with pytest.raises(DatasetError):
        scan_dataset(tmp_path / "missing")

    blob = tmp_path / "blob.mp4"
    blob.write_bytes(b"x")
    with pytest.raises(DatasetError):
        scan_dataset(blob)


def test_scan_dataset_empty_or_without_videos_returns_empty(tmp_path: Path) -> None:
    """FR-001: dataset vacío o sin vídeos → tupla vacía (no es un error)."""
    assert scan_dataset(tmp_path) == ()
    (tmp_path / "readme.txt").write_text("hola")
    assert scan_dataset(tmp_path) == ()


def test_ingest_errors_are_controlled() -> None:
    """FR-001/FR-002: los errores de ingesta son controlados bajo IngestError."""
    assert issubclass(DatasetError, IngestError)
    assert issubclass(ProbeError, IngestError)


# ---------------------------------------------------------------------------
# FR-002 · FFprobe (requiere ffmpeg/ffprobe)
# ---------------------------------------------------------------------------


def test_probe_video_returns_metadata(tmp_path: Path) -> None:
    """FR-002: FFprobe devuelve duración, dimensiones y fps del fixture."""
    _require_ffmpeg()
    video = make_test_video(tmp_path / "clip.mp4")

    probe = probe_video(video)

    assert 1_900 <= probe.duration_ms <= 2_100
    assert probe.width == 320
    assert probe.height == 240
    assert probe.avg_fps is not None
    assert probe.avg_fps == pytest.approx(25.0, abs=2.0)
    assert probe.codec in {"h264", "mpeg4"}


def test_probe_video_corrupt_raises_controlled_error(tmp_path: Path) -> None:
    """FR-001 (US1 esc. 3): fichero corrupto → ProbeError, no crash."""
    _require_ffmpeg()
    corrupt = make_corrupt_video(tmp_path / "corrupt.mp4")

    with pytest.raises(ProbeError, match="corrupt"):
        probe_video(corrupt)


def test_probe_video_without_video_stream_raises(tmp_path: Path) -> None:
    """FR-002: contenedor sin stream de vídeo → ProbeError (error controlado)."""
    _require_ffmpeg()
    audio = make_audio_video(tmp_path / "audio.mp4")

    with pytest.raises(ProbeError, match="stream de vídeo"):
        probe_video(audio)


# ---------------------------------------------------------------------------
# FR-002 · Extracción de frames (requiere ffmpeg/ffprobe)
# ---------------------------------------------------------------------------


def test_extract_frames_count_is_coherent(tmp_path: Path) -> None:
    """FR-002: nº de frames extraído == frames_per_video sobre fixture pequeño."""
    _require_ffmpeg()
    video = _dataset_video(make_test_video(tmp_path / "clip.mp4"))

    with extract_frames(video, work_root=tmp_path / "work", frames_per_video=10) as result:
        assert len(result.frames) == 10
        assert result.out_dir.is_dir()
        assert result.temporary_paths == tuple(f.path for f in result.frames)
        assert all(p.exists() for p in result.temporary_paths)

        timestamps = [f.timestamp_ms for f in result.frames]
        assert timestamps == sorted(timestamps)
        assert len(set(timestamps)) == 10
        assert all(0 <= ts < result.probe.duration_ms for ts in timestamps)
        intervals = [b - a for a, b in zip(timestamps[:-1], timestamps[1:], strict=True)]
        assert all(i == pytest.approx(200, abs=5) for i in intervals)


def test_extract_frames_scales_to_configured_width(tmp_path: Path) -> None:
    """FR-002: escala configurable; los PNG escalados tienen la anchura pedida."""
    _require_ffmpeg()
    video = _dataset_video(make_test_video(tmp_path / "clip.mp4"))

    with extract_frames(
        video, work_root=tmp_path / "work", frames_per_video=4, scale_width=128
    ) as result:
        for frame in result.frames:
            assert frame.width == 128
            assert frame.height == 96
            with Image.open(frame.path) as img:
                assert img.size == (128, 96)


def test_extract_frames_cleans_temporaries_after_use(tmp_path: Path) -> None:
    """FR-009/ADR-0006 (SC-006): temporales listados y eliminados al salir del with."""
    _require_ffmpeg()
    video = _dataset_video(make_test_video(tmp_path / "clip.mp4"))
    work_root = tmp_path / "work"

    with extract_frames(video, work_root=work_root, frames_per_video=5) as result:
        out_dir = result.out_dir
        assert out_dir.exists()
        assert list(work_root.iterdir()) == [out_dir]

    assert not out_dir.exists()
    assert list(work_root.iterdir()) == []


def test_extract_frames_cleans_temporaries_on_error(tmp_path: Path) -> None:
    """FR-009/ADR-0006 (SC-006): fallo → error controlado y sin temporales residuales."""
    _require_ffmpeg()
    corrupt = _dataset_video(make_corrupt_video(tmp_path / "corrupt.mp4"))
    work_root = tmp_path / "work"

    with pytest.raises(ProbeError):
        with extract_frames(corrupt, work_root=work_root, frames_per_video=5):
            pass

    assert list(work_root.iterdir()) == []


def test_extract_frames_reproducible(tmp_path: Path) -> None:
    """FR-002 (SC-007 mindset): dos extracciones → mismos timestamps y bytes de frame."""
    _require_ffmpeg()
    video = _dataset_video(make_test_video(tmp_path / "clip.mp4"))

    runs: list[tuple[list[int], list[bytes]]] = []
    for i in range(2):
        with extract_frames(video, work_root=tmp_path / f"work{i}", frames_per_video=6) as result:
            runs.append(
                (
                    [f.timestamp_ms for f in result.frames],
                    [f.path.read_bytes() for f in result.frames],
                )
            )

    assert runs[0] == runs[1]


def test_extract_frames_invalid_config_raises_value_error(tmp_path: Path) -> None:
    """FR-002: configuración inválida → ValueError antes de tocar FFmpeg."""
    video = DatasetVideo(local_ref="x.mp4", path=tmp_path / "x.mp4", size_bytes=0)
    work_root = tmp_path / "work"

    with pytest.raises(ValueError, match="frames_per_video"):
        with extract_frames(video, work_root=work_root, frames_per_video=0):
            pass

    with pytest.raises(ValueError, match="scale_width"):
        with extract_frames(video, work_root=work_root, frames_per_video=5, scale_width=0):
            pass
