# Fixtures sintéticos — adapter redgifs (PR-066 · SEC-004)

Reconstrucciones **anonimizadas** de la estructura real observada en la
prospección de 2026-08-19 (recursos públicos de `api.redgifs.com`, sin
bypass; el JSON real capturado vive fuera del repo, en `/tmp`, nunca aquí).
Todas las URLs de asset usan el dominio `.invalid` (RFC 2606) y ningún fichero
contiene media real. El valor de token es un literal claramente sintético
(`"fixture-token-not-a-secret"`), nunca un secreto real (SEC-005).

## Ficheros

- `auth_temporary.json` — respuesta de `GET /v2/auth/temporary`.
- `niche_gifs_page_1.json` / `niche_gifs_page_2.json` — dos páginas
  consecutivas de `GET /v2/niches/<id>/gifs?order=new&count=100&page=N`
  (envelope `{gifs, page, pages, total}`); la página 2 es la última
  (`page == pages`).
- `niche_gifs_empty.json` — página vacía **legítima** de fin de listado
  (`gifs=[]` con `page`/`pages`/`total` presentes) — distinta del caso
  malformado (`gifs` vacío **y** `pages`/`total` ausentes), que los tests
  construyen inline porque no es una respuesta real observada, sino una
  regresión de estructura a detectar.
- `gif_object.json` — respuesta de `GET /v2/gifs/<id>` (wrapper
  `{"gif": {...}}`) para un ítem de vídeo/gif normal (`type=1`).
- `gif_object_image_post.json` — mismo endpoint para un **post de imagen**
  (`type=2`, `duration=null`, `hasAudio=false`, `hls=false`).
- `gif_not_found_404.json` — cuerpo de la respuesta `404` `GifNotFound` de
  `GET /v2/gifs/<id>` para un ítem retirado.

## Estructura observada (resumen, prospección 2026-08-19)

- Envelope de listado: `page`/`pages`/`total` (paginación por `page`, 1-based;
  el campo `cursor` del envelope NO pagina este endpoint); `count=100` es el
  máximo aceptado.
- Objeto gif: `id`, `description` (nullable), `createDate` (epoch s),
  `duration` (s, nullable), `tags` (array de strings), `userName`, `views`,
  `likes`, `hasAudio`, `type` (1=vídeo/gif, 2=imagen), `urls.{thumbnail,
  poster, sd, hd, silent, html}`. Los thumbnails/posters viven en
  `media.redgifs.com` (aquí, `media.redgifs.invalid`); los mp4 (`sd`/`hd`/
  `silent`) se conservan en el fixture para probar que el adapter **nunca**
  los lee ni expone (SC-006 de la spec 002).
