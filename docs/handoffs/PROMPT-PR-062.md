# Prompt para el agente implementador — PR-062 (adapter xhamster)

> Copia-pega este bloque completo al agente implementador (modelo `deepseek-v4-flash`).
> Repositorio: XTrace, working dir `/Users/robertomorgadoluengo/work/code/XTrace`.

---

Implementa **PR-062** de la feature `007-xhamster-adapter`: el adapter real
`xhamster` (HTML) de XTrace, sobre el SDK de la spec 002. **Tienes que implementar
xhamster, no xvideos** (xvideos ya existe como referencia).

## 1. Preflight (obligatorio antes de tocar archivos)

Lee en este orden:

1. `AGENTS.md` y `.specify/memory/constitution.md`
2. `specs/007-xhamster-adapter/spec.md` (**APPROVED**; decisiones D1–D5 del humano)
3. `specs/007-xhamster-adapter/plan.md` y `specs/007-xhamster-adapter/tasks.md`
   (tu contrato es la tarea **PR-062**)
4. `docs/adr/0015-xhamster-adapter-html-sprite.md`
5. `docs/handoffs/FEATURE-007-PLANNING.md` (contexto del orquestador + captures reales)
6. Referencia de código: `services/crawler/xtrace_crawler/adapters/{base,models,xvideos}.py`,
   `services/crawler/xtrace_crawler/crawling/http.py`,
   `services/crawler/tests/unit/test_xvideos_adapter.py`,
   `services/crawler/tests/fixtures/xvideos/` (README incluido)

## 2. Ramas

Crea y trabaja en `feature/007-xhamster-adapter-PR-062-xhamster-adapter` (desde
`main` o la rama de fase). No hagas push directo a `main`.

## 3. Allowed paths (NO toques NADA más)

- `services/crawler/xtrace_crawler/adapters/xhamster.py` (nuevo)
- `services/crawler/tests/fixtures/xhamster/` (nuevo: `README.md`,
  `category_page_1.html`, `category_page_2.html`, `video_page_full.html`,
  `video_page_minimal.html`, `video_page_sin_sprite.html`)
- `services/crawler/tests/unit/test_xhamster_adapter.py` (nuevo)

**Prohibido**: modificar `adapters/base.py`, `models.py`, `registry.py`, `pipeline.py`,
`crawling/*`, `jobs/*`, `assets/*`, `cli.py`, `repo.py`, `supabase/*` o el shell
Next.js. Si crees que algo exige tocar el core → **STOP**, marca BLOCKED y explícalo
en el handoff (constitución §1).

## 4. Qué implementar (detalle)

### 4.1 `adapters/xhamster.py`

- **Parsers puros** (sin red, testables):
  - `parse_listing_page(html, *, current_path=None) -> DiscoverPage`:
    ítems `div.video-thumb` con enlaces
    `a.video-thumb__image-container[data-role="thumb-link"]` href a
    `/videos/<slug>-<id>` (absolutos o relativos); `external_id` = **último segmento
    tras el guion final** del path (formas numérica `2533587` y alfanumérica
    `xhTRpbl`, charset `[A-Za-z0-9]+`); dedup preservando orden; `page_urls` con el
    href completo (paridad PR-045 de xvideos). Paginación: `a.page-button-link` —
    cursor = path del enlace **siguiente al activo** (`page-button-link--active`) en
    la misma lista de paginación; sin siguiente → `None`; anti-bucle (cursor ==
    path actual → `None`). Página sin ítems → vacía sin crash.
  - `parse_video_page(html, *, page_url) -> VideoSource`:
    `og:title`/`og:url`/`og:image` + `window.initials` JSON:
    `videoModel.id`, `.duration` (segundos → `duration_ms`), `.title`,
    `.created` (epoch s → `published_at` tz-aware UTC), `.tags[].name` (máx. 20,
    fallback `.keywords` string separada por comas). **Sprite del vídeo principal**:
    `window.initials.spriteLoader.template` (p. ej.
    `https://thumb-v7.xhcdn.com/a/<token>/002/533/587/160x160.50.s.jpg`) →
    `storyboard_urls=[template]`; sin template → `[]`. **Los `data-sprite` del HTML
    son de vídeos relacionados: NO usarlos.** `external_id` desde `og:url` (fallback
    `page_url`); sin señales de vídeo ni patrón → `XhamsterParseError` tipado.
    `preview_url=None` SIEMPRE (D3/SC-004: los mp4 de previews no se exponen).
  - `storyboard_grid(asset) -> tuple[int,int] | None`: `(N, 1)` si la URL lleva
    `.<N>.s.` (p. ej. `160x160.50.s.jpg` → 50); `(20, 1)` para `*.s.webp` sin N;
    `None` en otro caso. (Los sprites xhamster son tiras de UNA fila.)
- **`XhamsterAdapter`** (protocolo `SourceAdapter`, paridad con `XvideosAdapter`):
  - `manifest`: source=`xhamster`, access_method=`html`,
    assets_accessed=["storyboard","thumbnail"], `robots_reviewed=true`,
    `terms_reviewed=true`, `review_date="2026-08-19"` (Decisión D5 — revisión humana
    OK en modo prueba), `rate_limit=RateLimitSpec(min_interval_ms=2000, max_rps=0.5)`.
  - `asset_hosts` **PROVISIONAL** (fail-closed): hosts `thumb-v0.xhcdn.com` …
    `thumb-v9.xhcdn.com` + `ic-vt-nss.xhcdn.com` (documentar como provisional en
    docstring, validar en PR-065).
  - `SafeHTTPClient` con allowlist de página `{"xhamster.com", "www.xhamster.com",
    "es.xhamster.com"}` (D1 + corrección A1: `es.*` como objetivo de redirect/URL
    canónica, no como base) + resolver mock para MockTransport (paridad xvideos).
  - `discover(*, cursor, limit, section=None)`: **D2 — `section` obligatorio** y debe
    empezar por `/` (si no → `ValueError` tipado; sin sección NO se explora la home).
    URL inicial = `https://xhamster.com<section>`; con cursor = `https://xhamster.com
    <cursor>`. Truncación no soportada (más IDs que `limit` → error tipado).
    Anti-bucle: 0 IDs nuevos → `next_cursor=None`.
  - `get_video(external_id, *, page_url=None)`: usa `page_url` si viene y es de un
    host permitido (fallback: plantilla `https://xhamster.com/videos/x-{id}`); 404 →
    `None` (vídeo retirado); resto de errores HTTP → propagación.
  - `get_visual_assets(video)`: UN `VisualAsset(kind="storyboard",
    url=video.storyboard_urls[0], position=None, timestamp_ms=None)` si hay sprite +
    UN `VisualAsset(kind="thumbnail", url=video.thumbnail_url)` si hay thumbnail;
    sin sprite → solo thumbnail; sin ambos → `[]`. Nunca mp4 (SC-004).
  - `check_availability(video)`: 404 → `REMOVED`; página parseable → `AVAILABLE`;
    resto → `UNAVAILABLE` (paridad xvideos).
  - `aclose()`.

### 4.2 Fixtures sintéticos (`tests/fixtures/xhamster/`)

Reconstrucciones **anonimizadas** de la estructura real (SEC-004): dominio
`xhamster.invalid` (RFC 2606), títulos genéricos ("Titulo de ejemplo N"), IDs
sintéticos, **sin media real ni títulos reales**. Captures reales de referencia en
`/tmp/xh-amateur.html` y `/tmp/xh-video.html` (NUNCA al repo). Cubrir:
listado con ambas formas de id + paginación con salto (`/2`, `/16828`), página de
vídeo completa (og:* + initials con `spriteLoader.template` + `videoModel` con tags
y `created` + `data-sprite` de relacionados para probar que NO se usan), página
mínima (sin tags/fecha/duración), página sin sprite (sin `spriteLoader.template`).
`README.md` documentando la estructura observada (estilo
`tests/fixtures/xvideos/README.md`).

### 4.3 Tests (`tests/unit/test_xhamster_adapter.py`)

Test-first sobre los parsers (constitution §6). Cubrir al menos: IDs dedup en ambas
formas; `page_urls`; cursor/paginación (incluido salto de página y anti-bucle);
truncación; `XhamsterParseError`; metadata completa y opcional-nula; sprite del
player (template) elegido y `data-sprite` de relacionados ignorados; `preview_url
None`; `storyboard_grid` (50,1)/(20,1)/None; adapter con `httpx.MockTransport`
(discover con/sin section, get_video 404/error, check_availability, allowlist);
test AST anti-acoplamiento (ningún módulo del core importa `adapters.xhamster`,
paridad con `test_core_no_importa_el_adapter_xvideos`).

## 5. Calidad (obligatorio, en este orden)

```bash
cd services/crawler
uv run ruff check . && uv run ruff format --check . && uv run mypy . && uv run pytest tests/unit -q
```

Todo en verde antes de terminar. (Los tests de integración contra Supabase local son
de PR-064, no tuyos.)

## 6. Entregables

- Código + fixtures + tests en los allowed_paths.
- Handoff `docs/handoffs/PR-062.md` según `docs/handoffs/TEMPLATE.md` (resumen,
  requisitos, archivos, decisiones, tests, comandos, resultados, limitaciones,
  riesgos, trabajo pendiente, instrucciones para el revisor).
- Commit(s) trazables a `specs/007-xhamster-adapter/spec.md` FR-001…FR-010.
- NO marques la spec como IMPLEMENTED ni toques `tasks.md`/`STATUS.md` (eso es del
  orquestador/revisor).

## 7. Criterios de aceptación (de la spec 007)

- Flujo completo con fixtures **sin red**, determinista (SC-001).
- El core no importa nada específico de xhamster (SC-006/SC-007).
- 0 descargas de vídeo completo: solo sprite + thumbnail (SC-004); rate limit
  declarado (SC-005).
- Manifest conforme a D5; gate SEC-002 intacto (no lo cambias).
