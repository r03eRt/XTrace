"""Datos de catálogo sintético del MockAdapter (PR-021 · FR-003 · SC-001).

Fixtures **sin red** (NFR-003): valores canónicos de ejemplo para construir
casos de test contra el `MockAdapter` sin depender de su implementación.

- `FIXTURE_SEED` / `FIXTURE_CATALOG_SIZE`: seed y tamaño canónicos usados por
  el harness y los tests (deterministas entre ejecuciones, SC-001).
- `SAMPLE_VIDEOS`: datos de ejemplo **literales** (títulos anonimizados, URLs
  sintéticas `http://mock.local/...`, contenido 100 % sintético — SEC-004).
  Son ejemplos canónicos de la forma de `VideoSource`, no instantáneas del
  catálogo generado con `FIXTURE_SEED` (para valores esperados del catálogo
  generado, usar `MockHarness.catalog()`).
- `SAMPLE_ASSETS`: assets esperados para `SAMPLE_VIDEOS`, calculados a mano
  con las reglas documentadas del mock (jerarquía storyboard → thumbnails →
  preview, nunca vídeo completo — SC-006): oráculo independiente de forma.
"""

from __future__ import annotations

from xtrace_crawler.adapters.models import VideoSource, VisualAsset

#: Seed canónico de los fixtures (determinismo entre ejecuciones, NFR-003).
FIXTURE_SEED: int = 42
#: Tamaño de catálogo canónico de los fixtures.
FIXTURE_CATALOG_SIZE: int = 5

#: Número de tiles de storyboard en los samples (paridad con `_STORYBOARD_TILES` del mock).
_SAMPLE_STORYBOARD_TILES: int = 6
#: Número de thumbnails en los samples (paridad con `_THUMBNAILS` del mock).
_SAMPLE_THUMBNAILS: int = 3


def _sample_assets(video: VideoSource) -> list[VisualAsset]:
    """Assets esperados del sample: storyboard con timestamps dentro de la duración."""
    base = f"http://mock.local/assets/{video.external_id}"
    assets: list[VisualAsset] = []
    for position in range(_SAMPLE_STORYBOARD_TILES):
        assets.append(
            VisualAsset(
                kind="storyboard",
                url=f"{base}/storyboard.jpg",
                position=position,
                timestamp_ms=(
                    position * video.duration_ms // _SAMPLE_STORYBOARD_TILES
                    if video.duration_ms is not None
                    else None
                ),
            )
        )
    for position in range(_SAMPLE_THUMBNAILS):
        assets.append(
            VisualAsset(
                kind="thumbnail",
                url=f"{base}/thumb-{position}.jpg",
                position=position,
                timestamp_ms=None,
            )
        )
    assets.append(
        VisualAsset(kind="preview", url=f"{base}/preview.mp4", position=0, timestamp_ms=None)
    )
    return assets


def _sample_videos() -> dict[str, VideoSource]:
    """Catálogo de ejemplo literal (títulos anonimizados, URLs sintéticas)."""
    entries: list[tuple[str, str, int]] = [
        ("mock-vid-0000", "Sundown Ride", 125_000),
        ("mock-vid-0001", "City Lights Walk", 84_000),
        ("mock-vid-0002", "Quiet Forest Stream", 240_000),
    ]
    videos: dict[str, VideoSource] = {}
    for external_id, title, duration_ms in entries:
        videos[external_id] = VideoSource(
            source="mock",
            external_id=external_id,
            title=title,
            page_url=f"http://mock.local/videos/{external_id}",
            duration_ms=duration_ms,
            thumbnail_url=f"http://mock.local/assets/{external_id}/thumb-0.jpg",
            preview_url=f"http://mock.local/assets/{external_id}/preview.mp4",
            storyboard_urls=[f"http://mock.local/assets/{external_id}/storyboard.jpg"],
            tags=["nature", "city"],
            published_at=None,
        )
    return videos


#: Catálogo de ejemplo canónico (dict external_id → VideoSource).
SAMPLE_VIDEOS: dict[str, VideoSource] = _sample_videos()

#: Assets esperados para cada sample (dict external_id → list[VisualAsset]).
SAMPLE_ASSETS: dict[str, list[VisualAsset]] = {
    external_id: _sample_assets(video) for external_id, video in SAMPLE_VIDEOS.items()
}
