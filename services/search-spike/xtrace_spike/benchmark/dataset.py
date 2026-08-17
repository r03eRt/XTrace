"""Generador del dataset de benchmark de búsqueda visual (PR-015 · FR-015 · D3).

Produce ~210 casos de evaluación (Decisión D3): ~30 por cada una de las 6
variantes positivas (exacta, comprimida, recortada, watermark, redimensionada,
color alterada) = ~180 positivos, más ~30 muestras negativas. Cada caso es un
BenchmarkCase: imagen de consulta + variante + vídeo esperado (None para
negativas). La variante "clip corto" queda fuera (Decisión D1, FR-011).

El generador trabaja SOLO sobre frames/archivos (sin DB ni torch): recibe los
frames del dataset indexado (PR-008/PR-011) como SourceFrame y escribe las
imágenes de consulta en un directorio de salida, junto con un manifest.json
reproducible (SC-007 mindset) que el runner (PR-016) consumirá.

Reproducibilidad: toda la aleatoriedad deriva de un único seed (sampling de
frames + síntesis de negativas). Mismo seed + mismos frames -> mismos casos y
mismos bytes de imagen.

Variantes (Pillow + numpy, deterministas):
  exact      — copia bit a bit del frame indexado (píxeles idénticos).
  compressed — re-encode JPEG con calidad baja (artefactos de compresión).
  cropped    — recorte central (70% x 70%) + resize al tamaño original.
  watermark  — texto semitransparente "XTRACE" sobre banda oscura.
  resized    — resize menor (50%) con LANCZOS.
  color      — ganancias por canal (R x1.15, G x0.85, B x0.75) + giro de hue.
  negative   — imágenes sintéticas (ruido/gradiente/formas) NO pertenecientes
               al dataset; expected_video_ref = None.
"""

from __future__ import annotations

import json
import math
import random
import re
import shutil
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np
from PIL import Image, ImageDraw, ImageFont

DEFAULT_SEED: int = 20260814
DEFAULT_CASES_PER_VARIANT: int = 30
DEFAULT_NEGATIVE_CASES: int = 30

POSITIVE_VARIANTS: tuple[str, ...] = (
    "exact",
    "compressed",
    "cropped",
    "watermark",
    "resized",
    "color",
)
NEGATIVE_VARIANT: str = "negative"

# Parámetros fijos de las variantes (documentados; cambiarlos cambia el dataset).
_COMPRESSED_JPEG_QUALITY: int = 25
_CROP_RATIO: float = 0.7
_RESIZE_RATIO: float = 0.5
_WATERMARK_TEXT: str = "XTRACE"
_WATERMARK_ALPHA: int = 160
_COLOR_GAINS: tuple[float, float, float] = (1.15, 0.85, 0.75)
_HUE_SHIFT_DEG: int = 12
_NEGATIVE_SIZE: tuple[int, int] = (320, 240)
_NEGATIVE_KINDS: int = 3

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slug(value: str) -> str:
    """Convierte un local_ref/video_ref en un prefijo de fichero seguro."""
    slug = _SLUG_RE.sub("_", value).strip("._-")
    return slug or "frame"


class BenchmarkError(ValueError):
    """Error de configuración/uso del generador de benchmark (FR-015)."""


@dataclass(frozen=True)
class SourceFrame:
    """Un frame del dataset indexado disponible como fuente de consultas.

    Atributos:
        video_ref: identificador estable del vídeo (local_ref del dataset,
            FR-001) al que pertenece el frame; es la etiqueta esperada de las
            consultas positivas derivadas de él.
        path: ruta al fichero de imagen del frame.
    """

    video_ref: str
    path: Path
    source: str = "local"
    duration_ms: int | None = None
    timestamp_ms: int | None = None


@dataclass(frozen=True)
class BenchmarkCase:
    """Un caso de evaluación del benchmark (FR-015 · spec "Key Entities").

    Atributos:
        query_image_path: ruta a la imagen de consulta generada (existe en
            disco tras generate_benchmark_dataset).
        variant: variante del caso — una de POSITIVE_VARIANTS o
            NEGATIVE_VARIANT.
        expected_video_ref: video_ref del vídeo esperado (None en negativas).
        source_frame_path: frame de origen del que se derivó la consulta
            (None en negativas, que son sintéticas).
    """

    query_image_path: Path
    variant: str
    expected_video_ref: str | None = None
    source_frame_path: Path | None = None
    source: str = "local"
    duration_ms: int | None = None
    timestamp_ms: int | None = None
    truth_timestamp_ms: int | None = None
    case_id: str | None = None

    def __post_init__(self) -> None:
        """Normaliza los dos nombres públicos del timestamp de verdad.

        ``timestamp_ms`` es el nombre histórico del spike; ``truth_timestamp_ms``
        hace explícito en sidecars que no es un timestamp interpolado. Aceptar
        ambos mantiene compatibilidad con manifests antiguos y evita perder
        trazabilidad al cargar datasets nuevos.
        """
        timestamps = [
            value for value in (self.timestamp_ms, self.truth_timestamp_ms) if value is not None
        ]
        if len(set(timestamps)) > 1:
            raise BenchmarkError("timestamp_ms y truth_timestamp_ms deben coincidir")
        canonical = timestamps[0] if timestamps else None
        object.__setattr__(self, "timestamp_ms", canonical)
        object.__setattr__(self, "truth_timestamp_ms", canonical)


@dataclass(frozen=True)
class BenchmarkDataset:
    """Resultado del generador: casos + configuración + manifest en disco.

    El manifest.json (json) contiene la configuración (seed, recuentos) y un
    caso por entrada con el query_image_path RELATIVO al out_dir, para que el
    dataset sea reubicable. PR-016 (runner) lo consumirá vía load_manifest.
    """

    out_dir: Path
    cases: tuple[BenchmarkCase, ...]
    seed: int
    cases_per_variant: int
    negative_cases: int

    @property
    def manifest_path(self) -> Path:
        return self.out_dir / "manifest.json"

    def counts_by_variant(self) -> dict[str, int]:
        """Recuento de casos por variante (orden estable por variante)."""
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.variant] = counts.get(case.variant, 0) + 1
        return counts

    def to_manifest(self) -> dict[str, object]:
        """Manifest JSON estable: configuración + casos (paths relativos)."""
        return {
            "seed": self.seed,
            "cases_per_variant": self.cases_per_variant,
            "negative_cases": self.negative_cases,
            "total_cases": len(self.cases),
            "cases": [
                {
                    "query_image_path": _rel_to(case.query_image_path, self.out_dir),
                    "variant": case.variant,
                    "expected_video_ref": case.expected_video_ref,
                    "source_frame_path": (
                        str(case.source_frame_path) if case.source_frame_path is not None else None
                    ),
                    "source": case.source,
                    "duration_ms": case.duration_ms,
                    "timestamp_ms": case.timestamp_ms,
                    "case_id": case.case_id,
                }
                for case in self.cases
            ],
        }

    def write_manifest(self) -> Path:
        """Escribe manifest.json en out_dir y devuelve su ruta."""
        payload = json.dumps(self.to_manifest(), indent=2, ensure_ascii=False) + "\n"
        self.manifest_path.write_text(payload, encoding="utf-8")
        return self.manifest_path


def scan_frames_root(
    frames_root: str | Path,
    *,
    pattern: str = "*.png",
    sidecar: str | Path | None = None,
) -> tuple[SourceFrame, ...]:
    """Escanea un directorio de frames con layout <root>/<video_ref>/<frame>.png.

    Cada subdirectorio inmediato se interpreta como un vídeo (video_ref = nombre
    del directorio) y cada fichero que casa con el patrón como un frame. Orden
    determinista (por video_ref y nombre de fichero).

    Raises:
        BenchmarkError: si frames_root no existe o no es un directorio.
    """
    root = Path(frames_root)
    if not root.is_dir():
        raise BenchmarkError(
            f"el directorio de frames '{frames_root}' no existe o no es un directorio"
        )
    sidecar_frames: dict[Path, SourceFrame] = {}
    if sidecar is not None:
        sidecar_frames = {frame.path: frame for frame in load_frame_sidecar(sidecar, root)}
    frames: list[SourceFrame] = []
    for path in sorted(root.rglob(pattern)):
        absolute = path.absolute()
        metadata = sidecar_frames.get(absolute)
        if metadata is None:
            frames.append(SourceFrame(video_ref=path.parent.name, path=absolute))
        else:
            frames.append(metadata)
    return tuple(frames)


def load_frame_sidecar(
    sidecar_path: str | Path,
    frames_root: str | Path | None = None,
) -> tuple[SourceFrame, ...]:
    """Carga metadatos de frames permitidos desde un sidecar JSON.

    Se admiten dos formas equivalentes y ambas son deliberadamente sencillas
    de generar desde los crawlers:

    ``{"frames": [{"path": ..., "video_ref": ..., ...}]}`` o
    ``{"videos": [{"video_ref": ..., "frames": [{"path": ..., ...}]}]}``.
    Las rutas relativas se resuelven respecto al sidecar, salvo que se indique
    ``frames_root``. No se descargan ni abren assets en esta fase.
    """
    path = Path(sidecar_path)
    data = _load_json_object(path)
    base = Path(frames_root).absolute() if frames_root is not None else path.parent.absolute()
    entries: list[tuple[dict[str, object], str | None, int | None]] = []
    raw_frames = data.get("frames")
    if isinstance(raw_frames, list):
        for raw in raw_frames:
            if isinstance(raw, dict):
                entries.append((cast(dict[str, object], raw), None, None))
    raw_videos = data.get("videos")
    if isinstance(raw_videos, list):
        for raw_video in raw_videos:
            if not isinstance(raw_video, dict):
                continue
            video = cast(dict[str, object], raw_video)
            video_ref = _optional_string(video.get("video_ref"))
            duration = _optional_int(video.get("duration_ms"))
            raw_video_frames = video.get("frames")
            if not isinstance(raw_video_frames, list):
                continue
            for raw_frame in raw_video_frames:
                if isinstance(raw_frame, str):
                    entries.append(({"path": raw_frame}, video_ref, duration))
                elif isinstance(raw_frame, dict):
                    entries.append((cast(dict[str, object], raw_frame), video_ref, duration))
    if not entries:
        raise BenchmarkError(f"el sidecar '{sidecar_path}' no contiene frames")

    frames: list[SourceFrame] = []
    for entry, inherited_video_ref, inherited_duration in entries:
        raw_path = entry.get("path") or entry.get("frame_path") or entry.get("image_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise BenchmarkError("cada frame del sidecar necesita path")
        frame_path = Path(raw_path)
        if not frame_path.is_absolute():
            frame_path = base / frame_path
        video_ref = _optional_string(entry.get("video_ref")) or inherited_video_ref
        if not video_ref:
            video_ref = frame_path.parent.name
        source = (_optional_string(entry.get("source")) or "local").lower()
        duration = _optional_int(entry.get("duration_ms"))
        if duration is None:
            duration = inherited_duration
        timestamp = _optional_int(
            entry.get("timestamp_ms")
            if entry.get("timestamp_ms") is not None
            else entry.get("truth_timestamp_ms")
        )
        frames.append(
            SourceFrame(
                video_ref=video_ref,
                path=frame_path.absolute(),
                source=source,
                duration_ms=duration,
                timestamp_ms=timestamp,
            )
        )
    return tuple(sorted(frames, key=lambda frame: (frame.video_ref, str(frame.path))))


def generate_benchmark_dataset(
    frames: Collection[SourceFrame],
    out_dir: str | Path,
    *,
    seed: int = DEFAULT_SEED,
    cases_per_variant: int = DEFAULT_CASES_PER_VARIANT,
    negative_cases: int = DEFAULT_NEGATIVE_CASES,
    variants: Collection[str] = POSITIVE_VARIANTS,
) -> BenchmarkDataset:
    """Genera el dataset de benchmark (~210 casos por defecto) en out_dir.

    Para cada variante positiva muestrea cases_per_variant frames del pool
    (random.Random(seed), determinista) y deriva la imagen de consulta; las
    negativas se sintetizan con numpy derivado del mismo seed. Escribe todas
    las imágenes y el manifest.json, y devuelve el BenchmarkDataset.

    Raises:
        BenchmarkError: configuración inválida (recuentos <= 0, variantes
            desconocidas, pool de frames insuficiente o con ficheros ausentes).
    """
    out = Path(out_dir).absolute()
    frame_list = tuple(sorted(frames, key=lambda frame: (frame.video_ref, str(frame.path))))
    _validate_config(frame_list, cases_per_variant, negative_cases, variants)
    # Orden estable por POSITIVE_VARIANTS (determinismo del manifest aunque
    # el llamador pase una Collection sin orden, p. ej. un set).
    variant_order = tuple(variant for variant in POSITIVE_VARIANTS if variant in variants)

    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    np_rng = np.random.default_rng(rng.getrandbits(64))

    cases: list[BenchmarkCase] = []
    for variant in variant_order:
        chosen = rng.sample(frame_list, cases_per_variant)
        for index, frame in enumerate(chosen):
            query_path = out / variant / _query_filename(frame, variant, index)
            _render_variant(frame, query_path, variant)
            cases.append(
                BenchmarkCase(
                    query_image_path=query_path,
                    variant=variant,
                    expected_video_ref=frame.video_ref,
                    source_frame_path=frame.path,
                    source=frame.source,
                    duration_ms=frame.duration_ms,
                    timestamp_ms=frame.timestamp_ms,
                    case_id=f"{frame.video_ref}:{frame.path.stem}:{variant}:{index:04d}",
                )
            )

    for index in range(negative_cases):
        query_path = out / NEGATIVE_VARIANT / f"{NEGATIVE_VARIANT}__{index:04d}.png"
        _render_negative(query_path, np_rng, index)
        cases.append(
            BenchmarkCase(
                query_image_path=query_path,
                variant=NEGATIVE_VARIANT,
                expected_video_ref=None,
            )
        )

    dataset = BenchmarkDataset(
        out_dir=out,
        cases=tuple(cases),
        seed=seed,
        cases_per_variant=cases_per_variant,
        negative_cases=negative_cases,
    )
    dataset.write_manifest()
    return dataset


def load_manifest(manifest_path: str | Path) -> tuple[BenchmarkCase, ...]:
    """Carga los casos de un manifest.json generado (paths re-absolutizados).

    Los query_image_path del manifest son relativos al directorio del manifest
    (dataset reubicable); aquí se resuelven contra él. PR-016 (runner) usará
    esta función para consumir un dataset ya generado.
    """
    manifest = Path(manifest_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    base = manifest.parent
    cases: list[BenchmarkCase] = []
    for entry in data["cases"]:
        cases.append(
            BenchmarkCase(
                query_image_path=(base / entry["query_image_path"]).absolute(),
                variant=entry["variant"],
                expected_video_ref=entry["expected_video_ref"],
                source_frame_path=(
                    Path(entry["source_frame_path"])
                    if entry["source_frame_path"] is not None
                    else None
                ),
                source=str(entry.get("source", "local")).lower(),
                duration_ms=_optional_int(entry.get("duration_ms")),
                timestamp_ms=_optional_int(
                    entry.get("timestamp_ms")
                    if entry.get("timestamp_ms") is not None
                    else entry.get("truth_timestamp_ms")
                ),
                case_id=_optional_string(entry.get("case_id")),
            )
        )
    return tuple(cases)


def load_benchmark_sidecar(sidecar_path: str | Path) -> tuple[BenchmarkCase, ...]:
    """Carga casos de benchmark con verdad conocida desde un sidecar JSON.

    El sidecar es la frontera de datos para la comparación local/web. Cada
    entrada debe conservar ``source``, ``duration_ms`` y ``timestamp_ms`` (o
    ``truth_timestamp_ms``); se aceptan casos negativos con vídeo/timestamp
    nulos, aunque no contribuyen a las métricas temporales.
    """
    path = Path(sidecar_path)
    data = _load_json_object(path)
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list):
        raise BenchmarkError(f"el sidecar '{sidecar_path}' no contiene cases")
    cases: list[BenchmarkCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict):
            raise BenchmarkError(f"el caso {index} del sidecar no es un objeto")
        entry = cast(dict[str, object], raw_case)
        case_id = _optional_string(entry.get("case_id")) or f"case-{index:06d}"
        raw_query = entry.get("query_image_path") or entry.get("image_path")
        query_path = (
            Path(str(raw_query))
            if isinstance(raw_query, str) and raw_query
            else path.parent / "queries" / f"{case_id}.png"
        )
        if not query_path.is_absolute():
            query_path = path.parent / query_path
        expected = entry.get("expected_video_ref")
        expected_video_ref = str(expected) if expected is not None else None
        source = (_optional_string(entry.get("source")) or "local").lower()
        duration_ms = _optional_int(entry.get("duration_ms"))
        timestamp_ms = _optional_int(
            entry.get("timestamp_ms")
            if entry.get("timestamp_ms") is not None
            else entry.get("truth_timestamp_ms")
        )
        raw_source_frame = entry.get("source_frame_path")
        source_frame_path = (
            Path(str(raw_source_frame))
            if isinstance(raw_source_frame, str) and raw_source_frame
            else None
        )
        if source_frame_path is not None and not source_frame_path.is_absolute():
            source_frame_path = path.parent / source_frame_path
        cases.append(
            BenchmarkCase(
                query_image_path=query_path.absolute(),
                variant=str(entry.get("variant", "exact")),
                expected_video_ref=expected_video_ref,
                source_frame_path=source_frame_path,
                source=source,
                duration_ms=duration_ms,
                timestamp_ms=timestamp_ms,
                case_id=case_id,
            )
        )
    if not cases:
        raise BenchmarkError(f"el sidecar '{sidecar_path}' no contiene casos")
    return tuple(cases)


# Alias explícitos para consumidores que prefieren el término genérico sidecar.
load_case_sidecar = load_benchmark_sidecar
load_sidecar = load_benchmark_sidecar


# ---------------------------------------------------------------------------
# Validación y utilidades
# ---------------------------------------------------------------------------


def _load_json_object(path: Path) -> dict[str, object]:
    """Carga un objeto JSON y traduce errores de entrada al error de dominio."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"no se puede leer el sidecar '{path}': {exc}") from exc
    if not isinstance(raw, dict):
        raise BenchmarkError(f"el sidecar '{path}' debe contener un objeto JSON")
    return cast(dict[str, object], raw)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise BenchmarkError(f"valor entero inválido en sidecar: {value!r}")
    try:
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError
            return int(value)
        if isinstance(value, str):
            return int(value.strip())
        raise ValueError
    except (TypeError, ValueError) as exc:
        raise BenchmarkError(f"valor entero inválido en sidecar: {value!r}") from exc


def _validate_config(
    frame_list: tuple[SourceFrame, ...],
    cases_per_variant: int,
    negative_cases: int,
    variants: Collection[str],
) -> None:
    if not variants:
        raise BenchmarkError("variants no puede estar vacío")
    unknown = sorted(set(variants) - set(POSITIVE_VARIANTS))
    if unknown:
        raise BenchmarkError(
            "variantes desconocidas: "
            f"{', '.join(unknown)} (válidas: {', '.join(POSITIVE_VARIANTS)})"
        )
    if cases_per_variant <= 0:
        raise BenchmarkError(f"cases_per_variant debe ser > 0 (recibido {cases_per_variant})")
    if negative_cases < 0:
        raise BenchmarkError(f"negative_cases no puede ser negativo (recibido {negative_cases})")
    if not frame_list:
        raise BenchmarkError("no hay frames de origen: el pool está vacío")
    if len(frame_list) < cases_per_variant:
        raise BenchmarkError(
            f"se necesitan al menos {cases_per_variant} frames de origen "
            f"(hay {len(frame_list)}); usa más vídeos/frames o baja cases_per_variant"
        )
    missing = [str(frame.path) for frame in frame_list if not frame.path.is_file()]
    if missing:
        raise BenchmarkError(f"faltan ficheros de frame: {', '.join(missing[:5])}")


def _rel_to(path: Path, base: Path) -> str:
    """Path POSIX relativo a base (para el manifest reubicable)."""
    try:
        return path.absolute().relative_to(base).as_posix()
    except ValueError:
        return str(path.absolute())


def _query_filename(frame: SourceFrame, variant: str, index: int) -> str:
    """Nombre de fichero determinista de una consulta positiva."""
    suffix = ".jpg" if variant == "compressed" else ".png"
    return f"{_slug(frame.video_ref)}__{_slug(frame.path.stem)}__{index:04d}{suffix}"


# ---------------------------------------------------------------------------
# Renderizado de variantes (Pillow + numpy, deterministas)
# ---------------------------------------------------------------------------


def _open_rgb(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB")


def _render_variant(frame: SourceFrame, out_path: Path, variant: str) -> None:
    """Deriva la imagen de consulta del frame de origen (variantes positivas)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if variant == "exact":
        shutil.copy2(frame.path, out_path)
        return
    source = _open_rgb(frame.path)
    if variant == "compressed":
        source.save(out_path, "JPEG", quality=_COMPRESSED_JPEG_QUALITY, optimize=True)
    elif variant == "cropped":
        _cropped(source).save(out_path, "PNG")
    elif variant == "watermark":
        _watermarked(source).save(out_path, "PNG")
    elif variant == "resized":
        _resized(source).save(out_path, "PNG")
    elif variant == "color":
        _color_altered(source).save(out_path, "PNG")
    else:  # inalcanzable: variants validadas contra POSITIVE_VARIANTS
        raise BenchmarkError(f"variante desconocida: {variant}")


def _cropped(image: Image.Image) -> Image.Image:
    """Recorte central al CROP_RATIO de cada dimensión + resize al original."""
    width, height = image.size
    crop_w = max(1, math.floor(width * _CROP_RATIO))
    crop_h = max(1, math.floor(height * _CROP_RATIO))
    left = (width - crop_w) // 2
    top = (height - crop_h) // 2
    return image.crop((left, top, left + crop_w, top + crop_h)).resize(
        (width, height), Image.Resampling.LANCZOS
    )


def _resized(image: Image.Image) -> Image.Image:
    """Resize menor al RESIZE_RATIO del tamaño original (LANCZOS)."""
    width, height = image.size
    new_w = max(1, round(width * _RESIZE_RATIO))
    new_h = max(1, round(height * _RESIZE_RATIO))
    return image.resize((new_w, new_h), Image.Resampling.LANCZOS)


def _watermarked(image: Image.Image) -> Image.Image:
    """Sello semitransparente: texto XTRACE sobre banda oscura, abajo-derecha.

    Fuente por defecto de Pillow escalada (ImageFont.load_default(size=...)):
    determinista y sin dependencia de fuentes del sistema.
    """
    base = image.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_size = max(14, base.size[0] // 8)
    font = ImageFont.load_default(size=font_size)
    left, top, right, bottom = draw.textbbox((0, 0), _WATERMARK_TEXT, font=font)
    text_w, text_h = right - left, bottom - top
    pad = max(4, font_size // 4)
    margin = max(8, font_size // 2)
    x = base.size[0] - text_w - margin
    y = base.size[1] - text_h - margin
    draw.rectangle(
        [x - pad, y - pad, x + text_w + pad, y + text_h + pad],
        fill=(0, 0, 0, 96),
    )
    draw.text((x, y), _WATERMARK_TEXT, font=font, fill=(255, 255, 255, _WATERMARK_ALPHA))
    return Image.alpha_composite(base, overlay).convert("RGB")


def _color_altered(image: Image.Image) -> Image.Image:
    """Ganancias por canal (R x1.15, G x0.85, B x0.75) + giro de hue +12 deg."""
    pixels = np.asarray(image, dtype=np.float32)
    for channel, gain in enumerate(_COLOR_GAINS):
        pixels[..., channel] *= gain
    boosted = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), mode="RGB")

    hue, sat, val = boosted.convert("HSV").split()
    shift = round(255 * _HUE_SHIFT_DEG / 360)
    hue = hue.point(lambda value: (value + shift) % 256)
    return Image.merge("HSV", (hue, sat, val)).convert("RGB")


def _render_negative(out_path: Path, np_rng: np.random.Generator, index: int) -> None:
    """Sintetiza una imagen que NO pertenece al dataset (etiqueta None).

    Tres familias deterministas (rotando por índice): ruido uniforme,
    gradiente de color y formas geométricas. Tamaño fijo _NEGATIVE_SIZE.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = _NEGATIVE_SIZE
    kind = index % _NEGATIVE_KINDS
    if kind == 0:
        pixels = np_rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
    elif kind == 1:
        top = np_rng.integers(0, 256, size=3, dtype=np.uint8)
        bottom = np_rng.integers(0, 256, size=3, dtype=np.uint8)
        t = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
        base = top.astype(np.float32)[None, None, :] * (1.0 - t) + (
            bottom.astype(np.float32)[None, None, :] * t
        )
        noise = np_rng.integers(-12, 13, size=(height, width, 3), dtype=np.int16)
        pixels = np.clip(base + noise, 0, 255).astype(np.uint8)
    else:
        image = Image.new("RGB", (width, height), (24, 24, 24))
        draw = ImageDraw.Draw(image)
        for _ in range(8):
            x0 = int(np_rng.integers(0, width))
            y0 = int(np_rng.integers(0, height))
            x1 = int(np_rng.integers(x0, width))
            y1 = int(np_rng.integers(y0, height))
            fill = tuple(int(v) for v in np_rng.integers(40, 256, size=3))
            draw.rectangle([x0, y0, x1, y1], fill=fill)
        image.save(out_path, "PNG")
        return
    Image.fromarray(pixels, mode="RGB").save(out_path, "PNG")
