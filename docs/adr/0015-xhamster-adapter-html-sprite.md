# 0015. Adapter xhamster: HTML + sprite webp con grid resolver (segunda fuente real)

- **Estado**: Aceptada
- **Fecha**: 2026-08-19
- **Spec/Requisitos relacionados**: 007-xhamster-adapter · FR-001…FR-010, SEC-001/002,
  SC-006 · extiende ADR-0009 (contrato SourceAdapter) y el patrón de composición de
  PR-031/SC-007

## Contexto

La spec 007 añade la segunda fuente real, `xhamster.com`, al SDK de la spec 002. La
prospección factual (2026-08-19, recursos públicos, sin bypass) mostró una estructura
radicalmente distinta a xvideos: listado con ítems `div.video-thumb[data-video-id]`,
enlaces canónicos `/videos/<slug>-<id>` con **dos formas de id** (numérica, p. ej.
`2533587`, y alfanumérica, p. ej. `xhTRpbl`), paginación numérica de categoría con
saltos, página de vídeo con `og:*` + `window.initials.videoModel` (**sin JSON-LD**), y
un **storyboard/sprite webp** de 5260×298 px (20 tiles de 263×298 en una fila) en el
CDN `thumb-*.xhcdn.com`, además de previews mp4 cortos. No hay API/feed oficial ni
sitemap (404).

Hay que decidir: método de acceso, qué identificador usar, cómo convertir el sprite en
frames con una sola descarga, y cómo mantener el objetivo "añadir una fuente no toca el
core" (SC-006/SC-007).

## Decisión

1. **Acceso `html`** (jerarquía FR-004): sin API/feed ni sitemap, parsing con
   `selectolax` sobre `SafeHTTPClient` (allowlist de página `xhamster.com` +
   `www.xhamster.com`).
2. **`external_id` = sufijo de la URL canónica** `/videos/<slug>-<id>` (la última
   parte tras el guion; ambas formas). El `data-video-id` del listado **no** se usa
   como id: no forma parte de la URL canónica ni de `og:url`, y mezclarlo rompería la
   regla "id derivable de la URL canónica". `DiscoverPage.page_urls` guarda el href
   completo (paridad PR-045).
3. **Sprite → frames con una descarga**: la página de vídeo expone el sprite del
   vídeo principal en el **player config** (`window.initials.spriteLoader.template`,
   p. ej. `https://thumb-v7.xhcdn.com/a/<token>/002/533/587/160x160.50.s.jpg` — el
   path `/NNN/NNN/NNN/` coincide con el de `og:image`). Los `data-sprite` del HTML de
   la página de vídeo pertenecen a vídeos **relacionados** y NO se usan (hallazgo de
   prospección 2026-08-19). Formato de sprite xhamster: **tira de una sola fila**
   `…/<W>x<H>.<N>.s.<ext>` donde N = nº de frames: observado
   `160x160.50.s.jpg` → fichero real 8000×131 → 50 tiles de 160×131 (spriteCount=50
   en el player config), y el hover sprite de listados `526x298.s.webp` → 5260×298 →
   20 tiles de 263×298. El adapter emite **UN**
   `VisualAsset(kind="storyboard", url=<template>, position=None, timestamp_ms=None)`
   por vídeo (desde `video.storyboard_urls[0]` poblado en `get_video`; sin sprite →
   degradación a thumbnail) y exporta la función pura
   `storyboard_grid(asset) -> tuple[int,int] | None`: `(N, 1)` si la URL lleva
   `.<N>.s.`; `(20, 1)` para `*.s.webp` sin N; `None` en otro caso. El CLI conecta esa
   función al hook **`storyboard_grid` del pipeline (PR-029, ya existente)** mediante
   import dinámico — el mismo mecanismo anti-acoplamiento del registro de adapters. El
   pipeline recorta las N tiles y deriva `timestamp_ms = round(position/N *
   duration_ms)` (clamp `[0, duration_ms)`); sin duración → `None`. Así: 1 GET de
   sprite por vídeo y **cero cambios en el core**. El grid es una **asumción
   re-validable** (fixtures + capturas reales del operador; si un sprite real no es
   divisible, el pipeline degrada con `StoryboardError` contenido, paridad PR-053).
4. **Assets de v1 = storyboard + thumbnail** (`og:image`); los previews mp4 observados
   (`data-previewvideo`) no se exponen (Decisión D3 de la spec): `preview_url=None`.
   Sin sprite → degradación a thumbnail único (FR-005).
5. **Discover acotado por sección** (Decisión D2): `section` obligatorio
   (`/categories/amateur`); sin sección → error tipado (fail-fast). Paginación por
   cursor sobre `/categories/amateur/N` con protección anti-bucle (cursor repetido /
   0 IDs nuevos → fin, paridad PR-043); la cota `--max-videos 50` acota la cadena.
6. **Compliance**: manifest revisado en modo prueba (Decisión D5:
   `robots_reviewed=true`, `terms_reviewed=true`, `review_date="2026-08-19"`), seed con
   `enabled=false` y gate SEC-002 del registry sin cambios.

## Alternativas consideradas

- **Un `VisualAsset` por tile con `position`** (patrón del MockAdapter): sin tocar nada
  del pipeline, pero descargaría el sprite completo **una vez por tile** (20 GETs por
  vídeo, ~1000 para la validación de 50 vídeos) — derroche de peticiones al CDN y
  riesgo de rate limiting. Rechazada.
- **Indexar el sprite como un único frame** (sin grid resolver): inútil para la
  búsqueda visual (la imagen completa de 20 escenas no representa ninguna). Rechazada.
- **Añadir campos de grid a `VisualAsset`/contrato**: toca el core/contrato de la spec
  002 sin necesidad (el hook `storyboard_grid` ya existe, PR-029). Rechazada.
- **Usar `data-video-id` como external_id**: numérico y simple, pero ausente de la URL
  canónica/`og:url` (no derivable en `get_video`/`check_availability` sin listado) y
  no unívoco entre las dos formas de URL observadas. Rechazada.
- **Acceso por navegador (último recurso FR-004)**: innecesario; el HTML servido ya
  contiene listado, metadata y sprite. Rechazada.

## Consecuencias

- (+) Segunda fuente con **0 cambios en el core** (solo adapter + registro + seed +
  fixtures): SC-006/SC-007 medibles con test AST.
- (+) 1 petición de sprite por vídeo; timestamps derivados con la semántica uniforme ya
  validada en xvideos (PR-053).
- (+) La estructura distinta (slugs, `window.initials`, sprite webp, paginación con
  saltos) ejercita de verdad el contrato ADR-0009.
- (−) El grid 20×1 es una asumción pendiente de la validación real; hasta entonces los
  tests se apoyan en fixtures sintéticos (riesgo documentado en la spec).
- (−) El wire del `storyboard_grid` vive en el punto de composición del CLI (como el
  registro): una función por fuente con dispatch por URL; si una tercera fuente
  necesitara grid propio, se reevalúa mover el dispatch al adapter (sin tocar el core
  hoy).
