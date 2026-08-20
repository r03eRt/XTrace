# Tasks: Adapter redgifs.com (tercera fuente real, primer adapter API)

**Input**: `spec.md` (APPROVED 2026-08-19), `plan.md`, ADR-0016. Sin `research.md` ni
`data-model.md` propios: la arquitectura y el modelo de datos son los de la spec 002
(heredados, sin cambios — ver `specs/002-source-sdk-crawler/plan.md`,
`data-model.md` y `contracts/`).

**Feature branch base**: `feature/008-redgifs-adapter` (cada PR usa su propia rama
plana `feature/008-redgifs-adapter-PR-0NN-slug` y termina en un PR aislado).

**Convención de estado por tarea**: `READY` cuando cumple la Definición de Ready
(AGENTS.md §11) y sus dependencias están `DONE`. Solo el orquestador cambia estos
estados.

> **Puerta legal (SEC-002 · Decisión D4)**: el humano dio su OK de revisión
> legal/ToS/robots **en modo prueba** (2026-08-19) y su instrucción explícita de
> reanudar la implementación (2026-08-20) → el manifest del adapter se declara
> revisado, pero la fuente se registra en BD con `enabled=false`: **ningún PR
> depende de ejecutar contra redgifs real**; todo se valida con fixtures
> sintéticos. La habilitación efectiva (backfill real, PR-069) es una acción humana
> explícita en BD.

---

## Leyenda

- **Prioridad**: P0 (imprescindible) · P1 (MVP de la fase) · P2 (importante).
- **Complejidad**: XS / S / M / L (sin XL; si algo sale XL, dividir).
- **[P]**: paralelizable con otras `[P]` que no compartan `allowed_paths` ni
  dependencias.

---

## Fase 1 — US1: adapter redgifs + fixtures + registro (P1) 🎯

### PR-066 · `RedgifsAdapter` + cliente de token + fixtures sintéticos + tests unitarios sin red

**Prioridad**: P1 · **Complejidad**: M · **Rol**: implementador · **Riesgo**: medio
(estructura de API asumida de la prospección, pendiente de validación real).

**Objetivo**: implementar el adapter `redgifs` (API oficial, `access_method="api"`,
primer adapter de este nivel) sobre el contrato `SourceAdapter` de la spec 002, con
parsers JSON puros testables, manejo del token temporal, y fixtures sintéticos
anonimizados (SEC-004). **Sin tocar el core.**

**Spec/requisitos**: FR-001…FR-006, FR-008, FR-010, SEC-001, SEC-003, SEC-004,
SEC-005, NFR-003, NFR-004, SC-001, SC-004, SC-005. ADR-0016. Decisiones D1/D2/D3/D5
de la spec.

**Allowed paths** (nadie más edita estos archivos):
- `services/crawler/xtrace_crawler/adapters/redgifs.py` (nuevo)
- `services/crawler/tests/fixtures/redgifs/` (nuevo: `README.md`,
  `auth_temporary.json`, `niche_gifs_page_1.json`, `niche_gifs_page_2.json`,
  `niche_gifs_empty.json`, `gif_object.json`, `gif_object_image_post.json`,
  `gif_not_found_404.json` — sintéticos, dominio `.invalid`, sin media real)
- `services/crawler/tests/unit/test_redgifs_adapter.py` (nuevo)

**Contenido**:
1. **Cliente de token** (`_TokenManager` o función equivalente): `GET
   /v2/auth/temporary` bajo demanda, cacheado en memoria del adapter (nunca en BD ni
   logs, SEC-005), renovación automática ante `401` con backoff del rate limiter
   existente; fallo persistente → error tipado contenido en la fuente.
2. **Parsers puros** (`parse_niche_gifs_envelope`, `parse_gif_object`, helpers):
   - Envelope de listado: `gifs`/`page`/`pages`/`total`; `count` fijo a 100 (máximo
     verificado); anti-bucle (página repetida / 0 IDs nuevos / `page >= pages` →
     fin); `gifs` ausente o vacío → fin sin error; `pages`/`total` ausentes con
     `count=0` → error tipado (`RedgifsParseError`).
   - Objeto gif: wrapper `{"gif": {...}}` de `GET /v2/gifs/<id>` (ignorando
     `user`/`niches` extra); `id`→`external_id` lowercase, `description`→`title`
     nullable, `createDate` epoch→`published_at`, `duration` (s, nullable,
     redondeo)→`duration_ms`, `tags`→`tags`, `urls.thumbnail`→`thumbnail_url`;
     `urls.poster` conservado para `get_visual_assets`; `urls.sd/hd/silent`
     **nunca** leídos más allá de descartarlos explícitamente; sin señales de gif →
     `RedgifsParseError`.
3. **`RedgifsAdapter`**: manifest (source=`redgifs`, access_method=`api`,
   assets_accessed=["thumbnail"], `robots_reviewed=true`, `terms_reviewed=true`,
   `review_date="2026-08-19"` — D4, rate_limit 2000 ms / 0.5 rps conservador),
   `asset_hosts=["media.redgifs.com"]` (fail-closed), `SafeHTTPClient` con
   allowlist de host de API `api.redgifs.com`, `discover` (D2: `section`
   obligatorio con prefijo `/niches/`, sin sección o prefijo inválido → error
   tipado; paginación por `page` + anti-bucle), `get_video` (404 `GifNotFound` →
   `None`; `page_url` fijo `https://www.redgifs.com/watch/<external_id>`, nunca
   fetcheado — D5), `get_visual_assets` (hasta dos `VisualAsset(kind="thumbnail",
   timestamp_ms=None)`: `urls.thumbnail` y `urls.poster`, alguno puede faltar sin
   fallar; **nunca** expone `urls.sd/hd/silent`), `check_availability` (200→
   `AVAILABLE`, 404→`REMOVED`, otro error→`UNAVAILABLE`), `aclose`.
4. **Fixtures sintéticos**: reconstrucciones anonimizadas de la estructura
   observada en prospección (2026-08-19, JSON real fuera del repo en `/tmp`):
   envelope de listado con 2 páginas + página vacía, objeto gif completo, objeto
   gif de post de imagen (`type=2`, `duration=null`, `hasAudio=false`), respuesta
   404 `GifNotFound`, respuesta de token temporal con valor claramente sintético
   (`"fixture-token-not-a-secret"`). README del fixture documenta la estructura
   observada (mismo patrón que `tests/fixtures/xhamster/README.md`).

**Tests requeridos** (en `test_redgifs_adapter.py`, test-first sobre parsers):
- token: obtención inicial, renovación ante 401 (con backoff), fallo persistente →
  error tipado sin fugar el valor del token en el mensaje.
- envelope de listado: parseo página completa, anti-bucle (página repetida/0 IDs
  nuevos/`page>=pages`), `gifs` vacío/ausente → fin sin error, `pages`/`total`
  ausentes con `count=0` → error tipado.
- objeto gif: campos completos, campos opcionales nulos (`description`/`tags`/
  `createDate` ausentes), post de imagen (`duration=null`→`duration_ms=None`),
  ids con mayúsculas → normalizados a lowercase, sin señales → `RedgifsParseError`.
- adapter con `httpx.MockTransport`: discover sin `section`/con sección que no
  empieza por `/niches/` → error tipado (fail-fast); `get_video` 404→`None`;
  `get_visual_assets` nunca incluye `sd`/`hd`/`silent`; `check_availability`
  available/removed/unavailable; allowlist (host ajeno a `api.redgifs.com`/
  `media.redgifs.com` → error).
- AST anti-acoplamiento (paridad `test_core_no_importa_el_adapter_xvideos`): ningún
  módulo del core importa `xtrace_crawler.adapters.redgifs`.

**Criterios de finalización**: `uv run ruff check . && uv run ruff format --check . &&
uv run mypy . && uv run pytest tests/unit/test_redgifs_adapter.py -q` en verde;
handoff `docs/handoffs/PR-066.md`.

---

### PR-067 · Registro en el CLI (import dinámico) + seed `redgifs` (enabled=false) + gate

**Prioridad**: P1 · **Complejidad**: S · **Rol**: implementador · **Riesgo**: bajo.

**Objetivo**: conectar el adapter al sistema exactamente como xvideos/xhamster/erome
(SC-006): registro dinámico en `_default_registry()`, fila de seed con
`enabled=false`, y pruebas del gate SEC-002.

**Spec/requisitos**: FR-007, SEC-002, DATA-001, SC-006, SC-007. Decisión D4.

**Dependencias**: PR-066 `DONE`.

**Allowed paths**:
- `services/crawler/xtrace_crawler/cli.py` (solo el bloque de composición raíz)
- `services/crawler/tests/unit/test_registry.py`
- `supabase/seed.sql`

**Contenido**:
1. `cli._default_registry()`: registrar `RedgifsAdapter()` (import dinámico,
   `real=True`) junto a mock/xvideos/xhamster/erome; docstring actualizado (D4:
   manifest revisado en modo prueba, habilitación efectiva por `sources.enabled`).
   **Sin wire de `storyboard_grid`**: redgifs no tiene sprite (ADR-0016 §4), a
   diferencia de xhamster/xvideos.
2. `supabase/seed.sql`: fila `redgifs` (manifest D4 con `review_date="2026-08-19"`,
   rate_limit 2000/0.5, `enabled=false`), `on conflict (name) do nothing`,
   comentario de la puerta legal (idempotente, mismo patrón que xhamster/erome).
3. `test_registry.py`: caso redgifs — manifest revisado + `enabled_in_db=false` →
   `AdapterNotEnabledError` con razón `sources.enabled=false`; con
   `enabled_in_db=true` → devuelve el adapter (D4).

**Tests requeridos**: `uv run pytest tests/unit/test_registry.py -q` en verde +
verificación de que `xtrace-crawler sources --json` lista `redgifs` (Supabase local).

**Criterios de finalización**: ruff/mypy/pytest en verde; handoff
`docs/handoffs/PR-067.md`.

---

## Fase 2 — US2/US3: flujo completo offline + validación real (P1/P2)

### PR-068 · Integración end-to-end con fixtures (pipeline + Supabase local) + quickstart

**Prioridad**: P1 · **Complejidad**: M · **Rol**: implementador · **Riesgo**: medio
(integración con BD local).

**Objetivo**: demostrar el flujo completo (token → discover → metadata → assets
→ frames sin timestamp → embeddings fake → índice) con el adapter redgifs **sin
red** (fixtures + `MockTransport`), INCREMENTAL sin duplicados, y dejar el
quickstart del operador.

**Spec/requisitos**: FR-009, FR-011 (heredada), NFR-001, NFR-002, SC-001 (offline),
SC-003, SC-007, US3.

**Dependencias**: PR-067 `DONE`.

**Allowed paths**:
- `services/crawler/tests/integration/test_pipeline.py` (caso redgifs nuevo; no
  reescribir casos existentes)
- `specs/008-redgifs-adapter/quickstart.md` (nuevo)
- `services/crawler/README.md` (sección redgifs)

**Contenido**:
1. Caso de integración redgifs: `RedgifsAdapter` con `MockTransport` sobre
   fixtures → pipeline con `FakeEmbeddingProvider` → vídeos únicos
   `(source_id, external_id)`, frames `source_kind=thumbnail` **sin timestamp**
   (thumbnail + poster, hasta 2 por ítem); re-ejecución INCREMENTAL sin
   duplicados; caso de fallo persistente (token inválido) aislado sin afectar a
   otras fuentes (US3).
2. `quickstart.md`: prerequisitos (uv, Docker, env), `uv sync --locked`, gates de
   calidad, `supabase db reset` (seed), habilitación explícita en BD (SQL del
   operador), `backfill --source redgifs --section /niches/homemade --max-videos
   50`, repetir con `/niches/real-cellphone-clips`, `--incremental`, `stats`, y el
   deshabilitado de la fuente.
3. README del servicio: sección redgifs (estado, gate legal, rate limits, primer
   adapter `access_method="api"`).

**Tests requeridos**: `uv run pytest tests/integration/test_pipeline.py -q` en
verde con Supabase local (y los unit de PR-066/067 siguen verdes).

**Criterios de finalización**: quickstart validado end-to-end offline; handoff
`docs/handoffs/PR-068.md`.

---

### PR-069 · Validación real acotada del operador (fuera de CI) + ajustes de estructura

**Prioridad**: P1 · **Complejidad**: M · **Rol**: operador + implementador ·
**Riesgo**: medio (estructura real de la API puede diferir de la asumida).

**Objetivo**: ejecutar el backfill real acotado tras la habilitación humana en BD
sobre los dos nichos de validación y corregir el adapter a la estructura real
observada (paridad PR-042…PR-053 de xvideos y PR-065 de xhamster).

**Spec/requisitos**: SC-002…SC-005, NFR-004, SEC-001. Decisión D3
(`--max-videos 50` sobre `homemade` y `real-cellphone-clips`).

**Dependencias**: PR-068 `DONE` + **habilitación humana en BD**
(`update public.sources set enabled=true where name='redgifs';` — solo el
operador).

**Allowed paths** (solo si la validación exige ajustes):
- `services/crawler/xtrace_crawler/adapters/redgifs.py`
- `services/crawler/tests/fixtures/redgifs/` + `tests/unit/test_redgifs_adapter.py`
- `docs/handoffs/PR-069.md` (nuevo)

**Contenido**:
1. Habilitación en BD (operador) y backfill real:
   `xtrace-crawler backfill --source redgifs --section /niches/homemade
   --max-videos 50` y `xtrace-crawler backfill --source redgifs --section
   /niches/real-cellphone-clips --max-videos 50` → verificar ≤ 50 vídeos
   `indexed` por nicho, frames sin timestamp y embeddings consultables (SC-002),
   INCREMENTAL sin duplicados (SC-003), rate limits respetados (SC-005), 0
   descargas de mp4 (SC-004), capturas reales en `/tmp` (nunca en el repo,
   SEC-004), token nunca expuesto en logs (SEC-005).
2. Ajustes del adapter a la estructura real observada (endpoints, envelope,
   hosts de CDN, comportamiento del token) **con sus tests de regresión** y
   re-validación.
3. Handoff de validación con evidencias (comandos, counts, logs) y nota de que la
   fuente queda deshabilitada en BD salvo que el operador decida mantenerla activa.

**Criterios de finalización**: SC-002…SC-005 evidenciadas; handoff
`docs/handoffs/PR-069.md`; spec 008 → `IMPLEMENTED` (previa revisión
independiente).

---

## Grafo de dependencias

```text
PR-066 (adapter + token + fixtures + unit) ──► PR-067 (registro + seed + gate)
                                                   │
                                                   ▼
                                             PR-068 (integración offline + quickstart)
                                                   │
                                                   ▼
                                             PR-069 (validación real, requiere habilitación humana)
```

Nada en esta feature toca el core: `adapters/base.py`, `models.py`, `registry.py`,
`pipeline.py`, `jobs/*`, `assets/*`, `crawling/ratelimit.py`, migraciones y el
shell Next.js quedan **fuera de allowed_paths** (SC-006). Si un hallazgo exigiera
tocarlos → **BLOCKED** y enmienda explícita de la spec 002 (constitución §1).

## Trazabilidad (cobertura)

- PR-066: FR-001…FR-006, FR-008, FR-010, SEC-001/003/004/005, NFR-003/004,
  SC-001/004/005.
- PR-067: FR-007, SEC-002, DATA-001, SC-006/007.
- PR-068: FR-009, DATA-002 (unicidad `(source_id, external_id)`), DATA-003 (sin
  tipos de job nuevos), NFR-001/002, SC-001 (offline)/003/007, US3.
- PR-069: SC-002…SC-005, NFR-004 (reales).
