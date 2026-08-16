# Fixtures sintéticos de xvideos (PR-031/PR-043/PR-044 · SEC-004)

Fixtures **sintéticos** que replican la **estructura observada** del HTML real
de xvideos.com (validación real 2026-08-16, hallazgo PR-033) con títulos
**anonimizados** ("Titulo de ejemplo N"), IDs sintéticos
(`video.synth000NN`) y URLs de ejemplo en el dominio reservado
`xvideos.invalid` (RFC 2606). **Prohibido** incluir media real o títulos
reales en el repositorio (SEC-004): estos archivos no contienen ningún
`xvideos.com` real, ninguna imagen ni vídeo, y ningún título de contenido
real.

Los tests de `tests/unit/test_xvideos_adapter.py` parsean estos archivos (sin
red, NFR-003) y validan metadatos, assets y paginación; si la estructura real
cambia, los tests fallan con mensajes claros (regresión de estructura).

## Estructura observada (captura real del operador, 2026-08-16)

> Las capturas reales viven fuera del repo (`/tmp/xvideos-probe/`); los
> fixtures de aquí son **reconstrucciones anonimizadas** de esa estructura
> (SEC-004). La estructura asumida de PR-031 (canonical, `flashvars`,
> `div.pagination a.next-page`, IDs numéricos) quedó **descartada**: el
> backfill real produjo 0 vídeos (selectores antiguos) y jobs DISCOVER en
> bucle — de ahí la protección anti-bucle de `discover()` (PR-043).

### Página de listado / discover (`listing_page_*.html`, `home_page.html`)

- Ítems: `div.thumb a[href^="/video."]` — el `href` es
  `/video.<encoded>/<slug>` y el ID externo es el **primer segmento del
  path** (p. ej. `video.synth00001`; el `<encoded>` NO es numérico en la
  estructura real, p. ej. `video.abc12345`). **PR-044 (2a validación real,
  2026-08-16)**: la HOME NO usa `a.thumb-link` — el enlace de vídeo es hijo
  directo de `div.thumb` **sin clase** (`<div class="thumb"><a href="/video.…">`);
  en `/best/…` el mismo selector cubre los `a.thumb-link` (que también viven
  dentro de `div.thumb`). El enlace del título (`div.thumb-under`, **fuera**
  de `div.thumb`) no se cuenta dos veces y los hrefs repetidos (p. ej.
  overlay + imagen) se deduplican.
- Thumb lazy: `img[data-src]` del CDN de thumbnails
  (`thumb-cdn77.xvideos-cdn.com` real; `thumb-cdn77.xvideos.invalid` en los
  fixtures), ficheros `xv_<N>_t.jpg`.
- Título del thumb: `div.thumb-under div.title a` (hermano de `div.thumb`).
- Paginación: `a.dir.next[href]` — el cursor es el **path** del enlace
  siguiente (p. ej. `/best/2026-07/1`); sin enlace, no hay más páginas
  (`next_cursor=None`). **La HOME no tiene paginación** (`a.dir.next`
  ausente, grid de una sola página ~30 vídeos): `discover()` devuelve los IDs
  y `next_cursor=None` (fin). Los hrefs absolutos se normalizan a path.
  `/best/1` **redirige** a `/best/2026-07`: el cursor se toma de la **URL
  FINAL** de la respuesta (`response.url`).
- **Anti-bucle (PR-043)**: si `a.dir.next` apunta al path actual de la
  respuesta, o si una página devuelve 0 IDs **nuevos** (no vistos en la
  instancia del adapter), `next_cursor=None` (fin).

### Página de vídeo (`video_page_*.html`)

- ID externo: `og:url` (patrón `/video.<encoded>/`); si falta, se deduce de
  la `page_url` de la petición. Sin patrón → `XvideosParseError` (mensaje
  claro, regresión de estructura).
- Título: `og:title`; fallback `h2.page-title` (en la estructura real el h2
  incluye un `span.duration`, p. ej. "14 min", que se descarta).
- Duración: `og:duration` en **segundos** (string) → `duration_ms`.
- Thumbnail: `og:image` (del CDN `thumb-cdn77…`, `xv_<N>_t.jpg`).
- Fecha y tags: bloque JSON-LD (`<script type="application/ld+json">`):
  `uploadDate` ISO con offset → `published_at` (tz-aware; sin offset se
  asume UTC), `keywords` → tags (máx. 20).
- Galería de thumbnails del reproductor: URLs `xv_<N>_t.jpg` del **mismo
  path CDN** que `og:image` (los thumbs de vídeos relacionados —otros
  UUIDs— quedan fuera), a menudo JSON-escapadas (`\/`) en los scripts del
  reproductor. `position=N`, `timestamp_ms` aproximado
  `round(N / (total+1) * duration_ms)`.
- **SC-006**: el mp4 completo existe en la página
  (`html5player.setVideoUrlLow`) pero está **PROHIBIDO** exponerlo o
  descargarlo → `preview_url` siempre `None`. No se detectó sprite real →
  `storyboard_urls` vacío y el manifest declara `["thumbnail"]`.
- `page_url` del `VideoSource`: el `og:url` de la página; fallback
  `https://www.xvideos.com/<external_id>/`.

## Archivos

| Archivo | Contenido |
| --- | --- |
| `home_page.html` | **HOME real (PR-044)**: 3 thumbs con enlaces de vídeo **sin clase** dentro de `div.thumb` (uno con href duplicado para probar dedup) + título en `div.thumb-under`; **sin paginación** (`a.dir.next` ausente → fin) |
| `listing_page_1.html` | 3 thumbs (uno con `thumb-link` duplicado para probar dedup) + `dir.prev` y `dir.next` → `/best/2026-07/1` |
| `listing_page_2.html` | 1 thumb (synth00004) + `dir.next` → `/best/2026-07/2` |
| `listing_page_3.html` | 1 thumb (synth00005), sin `dir.next` → fin de paginación |
| `video_page_full.html` | Estructura real completa: og:title/url/duration/image, h2 con `span.duration`, galería `xv_1..xv_6_t.jpg` (JSON-escapada), `setVideoUrlLow` (mp4 prohibido), JSON-LD con `uploadDate`/`keywords` |
| `video_page_minimal.html` | Solo og:title + og:url: campos opcionales `None` (edge case de la spec) |
