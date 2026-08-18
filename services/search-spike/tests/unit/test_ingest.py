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

import math
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from tests.fixtures import make_audio_video, make_corrupt_video, make_test_video
from xtrace_spike.ingest import IngestError
from xtrace_spike.ingest.dataset import DatasetError, DatasetVideo, scan_dataset
from xtrace_spike.ingest.frames import (
    ProbeError,
    VideoProbe,
    extract_frames,
    probe_video,
)
from xtrace_spike.sampling import AdaptiveSamplingPolicy


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


def test_extract_frames_adaptive_uses_centered_policy(tmp_path: Path) -> None:
    """FR-001/003: FFmpeg recibe el objetivo adaptativo y timestamps centrados."""
    _require_ffmpeg()
    video = _dataset_video(make_test_video(tmp_path / "clip.mp4", duration_s=2.0))

    with extract_frames(
        video,
        work_root=tmp_path / "work",
        sampling_policy=AdaptiveSamplingPolicy(),
    ) as result:
        assert len(result.frames) == 1
        timestamps = [frame.timestamp_ms for frame in result.frames]
        assert timestamps == [1_000]
        assert all(0 <= timestamp < result.probe.duration_ms for timestamp in timestamps)


def test_extract_frames_adaptive_single_frame_survives_container_duration_mismatch(
    tmp_path: Path,
) -> None:
    """FR-004: un contenedor con audio algo más largo no pierde su único frame."""
    _require_ffmpeg()
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    video_path = tmp_path / "audio-longer.mp4"
    encoder = (
        "libx264"
        if "libx264"
        in subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        else "mpeg4"
    )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=duration=19.166:size=320x240:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=19.202",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            encoder,
            "-g",
            "999",
            "-bf",
            "0",
            "-c:a",
            "aac",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    video = _dataset_video(video_path)
    with extract_frames(
        video,
        work_root=tmp_path / "work",
        sampling_policy=AdaptiveSamplingPolicy(),
    ) as result:
        assert len(result.frames) == 1
        frame = result.frames[0]
        assert frame.path.exists()
        assert frame.timestamp_ms == round(result.probe.duration_ms / 2)
        assert 0 <= frame.timestamp_ms < result.probe.duration_ms


@pytest.mark.parametrize("target_count", range(1, 9))
def test_extract_frames_adaptive_materializes_all_targets_on_long_gop(
    tmp_path: Path, target_count: int
) -> None:
    """FR-003/004: 1..8 puntos reales sobreviven a un contenedor long-GOP."""
    _require_ffmpeg()
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    video_path = tmp_path / f"long-gop-{target_count}.mp4"
    encoder = (
        "libx264"
        if "libx264"
        in subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        else "mpeg4"
    )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=duration=19.166:size=320x240:rate=30",
            "-c:v",
            encoder,
            "-g",
            "999",
            "-bf",
            "0",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    video = _dataset_video(video_path)
    probe = probe_video(video_path)
    interval_ms = math.ceil(probe.duration_ms / target_count)
    policy = AdaptiveSamplingPolicy(target_interval_ms=interval_ms, max_frames=8)
    assert policy.target_count(probe.duration_ms) == target_count

    with extract_frames(
        video,
        work_root=tmp_path / "work",
        scale_width=None,
        sampling_policy=policy,
    ) as result:
        assert len(result.frames) == target_count
        timestamps = [frame.timestamp_ms for frame in result.frames]
        expected = [
            round((index + 0.5) * result.probe.duration_ms / target_count)
            for index in range(target_count)
        ]
        assert timestamps == expected
        assert timestamps == sorted(timestamps)
        assert len(set(timestamps)) == target_count
        assert all(0 <= timestamp < result.probe.duration_ms for timestamp in timestamps)

        for frame in result.frames:
            assert frame.path.exists()
            assert frame.width == 320
            assert frame.height == 240
            with Image.open(frame.path) as image:
                assert image.size == (320, 240)


def test_extract_frames_adaptive_centers_multiple_points_and_legacy_stays_at_30(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-001/003: objetivo y comando FFmpeg son centrados; legacy sigue en 30."""
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"fixture")
    video = _dataset_video(video_path)
    probe = VideoProbe(
        duration_ms=360_000,
        width=320,
        height=240,
        avg_fps=25.0,
        codec="h264",
    )
    commands: list[list[str]] = []

    def fake_probe(path: Path) -> VideoProbe:
        assert path == video_path
        return probe

    def fake_run(
        command: list[str], *, capture_output: bool, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert capture_output and text and not check
        commands.append(command)
        frame_count = int(command[command.index("-frames:v") + 1])
        output_pattern = Path(command[-1])
        output_pattern.parent.mkdir(parents=True, exist_ok=True)
        if "%" in output_pattern.name:
            for index in range(frame_count):
                output_pattern.parent.joinpath(f"frame_{index + 1:06d}.png").touch()
        else:
            output_pattern.touch()
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("xtrace_spike.ingest.frames.probe_video", fake_probe)
    monkeypatch.setattr("xtrace_spike.ingest.frames.subprocess.run", fake_run)

    with extract_frames(
        video,
        work_root=tmp_path / "adaptive-work",
        sampling_policy=AdaptiveSamplingPolicy(),
    ) as adaptive:
        assert len(adaptive.frames) == 3
        assert [frame.timestamp_ms for frame in adaptive.frames] == [60_000, 180_000, 300_000]

    with extract_frames(video, work_root=tmp_path / "legacy-work") as legacy:
        assert len(legacy.frames) == 30
        assert [frame.timestamp_ms for frame in legacy.frames[:3]] == [0, 12_000, 24_000]

    adaptive_commands = commands[:3]
    legacy_command = commands[-1]
    assert len(adaptive_commands) == 3
    assert all(command[command.index("-frames:v") + 1] == "1" for command in adaptive_commands)
    assert all("-ss" in command for command in adaptive_commands)
    assert legacy_command[legacy_command.index("-frames:v") + 1] == "30"
    assert "-ss" not in legacy_command
