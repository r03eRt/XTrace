# Fixtures sintéticos de xhamster (PR-062 · SEC-004 · ADR-0015)

Fixtures **sintéticos** que replican la **estructura observada** del HTML real
de xhamster.com (prospección 2026-08-19, recursos públicos, sin bypass) con
títulos **anonimizados** ("Titulo de ejemplo N"), IDs sintéticos (`3000001`,
`xhT0001`) y URLs de ejemplo en los dominios reservados `xhamster.invalid` y
`xhcdn.invalid` (RFC 2606). **Prohibido** incluir media real o títulos reales
en el repositorio (SEC-004): estos archivos no contienen ningún `xhamster.com`
real, ninguna imagen ni vídeo, y ningún título de contenido real.

Los tests de `tests/unit/test_xhamster_adapter.py` parsean estos archivos (sin
red, NFR-003) y validan metadatos, assets y paginación; si la estructura real
cambia, los tests fallan con mensajes claros (regresión de estructura).

## Estructura observada (captura real del orquestador, 2026-08-19)

> Las capturas reales viven fuera del repo (`/tmp/xh-amateur.html`,
> `/tmp/xh-video.html`, `/tmp/xh-robots-full.txt`); los fixtures de aquí son
> **reconstrucciones anonimizadas** de esa estructura (SEC-004). robots.txt
> permite `/categories/` (salvo filtros best/daily/monthly/weekly/most-*) y
> `/videos/`; disallow `/premium/` (SEC-001) — el adapter solo construye URLs
> de sección/paginación/vídeo.

### Página de listado / discover (`category_page_*.html`)

- Ítems: `div.video-thumb[data-video-id]` con enlaces
  `a.video-thumb__image-container[data-role="thumb-link"]` a
  `/videos/<slug>-<id>`. En la captura real el href es **absoluto**
  (`https://es.xhamster.com/videos/amateur-11-2533587`); los fixtures usan
  paths relativos (el parser acepta ambos: el filtro es el patrón del path,
  no el prefijo del href).
- `external_id` = **último segmento tras el guion final** del path:
  `/videos/amateur-11-2533587` → `2533587` (numérica) y
  `/videos/...-xhTRpbl` → `xhTRpbl` (alfanumérica); charset `[A-Za-z0-9]+`.
  El `data-video-id` interno del listado NO se usa como id (no está en la URL
  canónica, ADR-0015 §2). Dedup por id preservando el orden del listado;
  `DiscoverPage.page_urls` guarda el href **completo** (paridad PR-045).
- Los ítems llevan `data-previewvideo`/`data-previewvideo-fallback` (mp4 de
  preview) y `aria-label` (título): **no se usan** (D3: previews no expuestos
  en v1; el título sale de la página de vídeo).
- Paginación: `ol.page-list > li.page-button > a.page-button-link` — el activo
  lleva `page-button-link--active`; hay separadores `...` y un
  `a.page-button-link` duplicado del enlace de la ÚLTIMA página dentro de
  `div.page-limit-button` (también es `a.page-button-link`). **Cursor = path
  del enlace siguiente al activo** en la misma lista; sin siguiente (activo al
  final) → `None`. **La numeración salta** de páginas pequeñas a numeración
  alta (p. ej. `/2` … `/6` → `/16828` → `/33654`): el cursor avanza por el
  href siguiente y el anti-bucle (candidato == path actual, 0 IDs nuevos)
  termina la cadena.

### Página de vídeo (`video_page_*.html`)

- ID externo: `og:url` (patrón `/videos/<slug>-<id>`); si falta, se deduce de
  la `page_url` de la petición. Sin patrón → `XhamsterParseError` (mensaje
  claro, regresión de estructura). Sin señales (og:title/og:url/videoModel) →
  `XhamsterParseError` (SEC-001: p. ej. captcha/anti-bot).
- Título: `og:title`; fallback `window.initials.videoModel.title`.
- Duración: `videoModel.duration` en **segundos** → `duration_ms`. **No hay
  `og:duration`** (estructura real).
- Thumbnail: `og:image` (host `ic-vt-nss.xhcdn.com` real;
  `ic-vt-nss.xhcdn.invalid` en los fixtures).
- Fecha y tags: `window.initials.videoModel` — `created` (epoch **segundos** →
  `published_at` tz-aware UTC), `tags` (array de `{name}`), fallback
  `keywords` (string separada por comas); máx. 20.
- **Sprite del vídeo principal**: `window.initials.spriteLoader.template`
  (p. ej. `https://thumb-v7.xhcdn.com/a/<token>/002/533/587/160x160.50.s.jpg`
  → fichero real 8000×131 → 50 tiles de 160×131, `spriteCount=50`) →
  `storyboard_urls=[template]`; sin template → `[]`. **Los `data-sprite` del
  HTML de la página de vídeo pertenecen a vídeos RELACIONADOS** (sus paths
  `/NNN/NNN/NNN/` difieren del vídeo principal) **y NO se usan**. Nota de
  robustez: la captura real sirve el template anidado en
  `window.initials.xplayerPluginSettings.spriteLoader.template` (y reflejado
  en `videoModel.spriteURL`); el parser acepta ambas formas, siempre desde
  `window.initials`.
- **SC-004 / D3**: los mp4 existen en la página (`data-previewvideo`,
  `trailerURL`) pero están **PROHIBIDOS** en v1 → `preview_url` siempre
  `None`. El manifest declara `["storyboard", "thumbnail"]`.
- `page_url` del `VideoSource`: el `og:url` de la página; fallback
  `https://xhamster.com/videos/x-<external_id>`.

## Archivos

| Archivo | Contenido |
| --- | --- |
| `category_page_1.html` | Página 1 del listado: 3 ítems (ids `3000001` numérico, `xhT0001` alfanumérico, `3000003`; el primero con href duplicado para probar dedup) + paginación con activo=1, separadores y salto a `/16828`/`/33654` → cursor `/categories/amateur/2` |
| `category_page_2.html` | Página 2 (`/categories/amateur/2`): 2 ítems (numérico `3000004` + alfanumérico `xhT0005`) + activo=2 con el siguiente SALTO directo a `/categories/amateur/16828` → cursor `/categories/amateur/16828` (paginación con salto) |
| `video_page_full.html` | Estructura real completa: og:title/url/image + `window.initials` con `videoModel` completo (duration 234 s, created epoch, tags `{name}`) y `spriteLoader.template` (50 tiles) + `data-sprite` de un vídeo RELACIONADO (debe ignorarse) + mp4 de preview/trailer (no expuestos) |
| `video_page_minimal.html` | Solo og:title + og:url + `videoModel` con id/title (id alfanumérico `xhT0001`): campos opcionales `None`/`[]` (edge case de la spec) |
| `video_page_sin_sprite.html` | `videoModel` completo + og:image pero SIN `spriteLoader.template` ni `spriteURL` → `storyboard_urls=[]`, degradación a thumbnail único (FR-005); tags por `keywords` (fallback) |
