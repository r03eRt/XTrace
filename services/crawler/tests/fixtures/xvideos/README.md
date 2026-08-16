# Fixtures sintéticos de xvideos (PR-031 · SEC-004)

Fixtures **sintéticos** que replican la **estructura** del HTML real de
xvideos.com con títulos **anonimizados** y URLs de ejemplo en el dominio
reservado `xvideos.invalid` (RFC 2606). **Prohibido** incluir media real o
títulos reales en el repositorio (SEC-004): estos archivos no contienen ningún
`xvideos.com` real, ninguna imagen ni vídeo, y ningún título de contenido real.

Los tests de `tests/unit/test_xvideos_adapter.py` parsean estos archivos (sin
red, NFR-003) y validan metadatos, assets y paginación; si la estructura real
cambia, los tests fallan con mensajes claros (regresión de estructura).

## Estructura asumida (documentada para PR-033)

> La captura real del HTML la hará el operador en PR-033; **esta es la
> estructura que hoy asume el adapter** (`adapters/xvideos.py`), basada en
> conocimiento público del layout de xvideos y en las convenciones de
> extractores públicos (p. ej. youtube-dl `XvideosIE`). Si la captura real
> difiere, se ajustan fixtures + selectores en PR-033 (el fallo queda aislado
> en el adapter, SC-008, y los tests de regresión lo señalan).

### Página de listado / discover (`listing_page_*.html`)

- Ítems: `div.thumb a[href^="/video"]` — el `href` contiene el ID externo:
  `/video<ID>/<slug>` (regex `^/video(?P<id>\d+)`). Un mismo thumb puede
  contener varios enlaces al mismo vídeo (imagen + overlay): el parser
  deduplica preservando el orden.
- Paginación: `div.pagination a.next-page[href]` — el cursor es el **path**
  del enlace siguiente (p. ej. `/best/2`); si no hay enlace, no hay más
  páginas (`next_cursor=None`). Los hrefs absolutos se normalizan a path.

### Página de vídeo (`video_page_*.html`)

- ID externo: `link[rel="canonical"]` `href` (patrón `/video<ID>/`); si falta,
  se deduce de la `page_url` de la petición. Sin patrón → `XvideosParseError`
  (mensaje claro, regresión de estructura).
- Título: `h2.page-title`.
- Duración / thumbnail / sprite / preview / fecha: bloque `flashvars` JSON
  embebido en un `<script>` (`var flashvars = { ... };`), con las claves
  `duration` (segundos, string), `timestamp` (unix, string), `thumb_url`,
  `thumb_sprite` (sprite/storyboard, 1 URL), `thumb_sprite_num`,
  `preview_video`. `flashvars` es el canal estable de datos del reproductor
  (mismo patrón que usa youtube-dl); si falta o no es JSON válido, los campos
  opcionales quedan `None` (spec: metadatos incompletos no bloquean el vídeo).
- Tags: `div.video-tags-list a` (texto de cada enlace).
- `page_url` del `VideoSource`: el canonical de la página; fallback
  `https://www.xvideos.com/video<ID>/`.

## Archivos

| Archivo | Contenido |
| --- | --- |
| `listing_page_1.html` | 3 thumbs (uno con enlace duplicado para probar dedup) + `next-page` → `/best/2` |
| `listing_page_2.html` | 1 thumb, sin `next-page` → fin de paginación |
| `video_page_full.html` | Metadatos completos (canonical, título, flashvars con duración/timestamp/thumb/sprite/preview, tags) |
| `video_page_minimal.html` | Solo canonical + título: campos opcionales `None` (edge case de la spec) |
