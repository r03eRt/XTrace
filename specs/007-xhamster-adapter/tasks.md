# Tasks: Adapter xhamster.com (segunda fuente real)

**Input**: `spec.md` (APPROVED 2026-08-19), `plan.md`, ADR-0015. Sin `research.md` ni
`data-model.md` propios: la arquitectura y el modelo de datos son los de la spec 002
(heredados, sin cambios — ver `specs/002-source-sdk-crawler/plan.md`,
`data-model.md` y `contracts/`).

**Handoff de planificación** (para el agente que recoja la feature):
`docs/handoffs/FEATURE-007-PLANNING.md` — decisiones D1–D5, correcciones A1/A2/A3,
captures reales en `/tmp` y próximo paso (asignar PR-062).

**Feature branch base**: `feature/007-xhamster-adapter` (cada PR usa su propia rama
plana `feature/007-xhamster-adapter-PR-0NN-slug` — mismo esquema que la fase 2 — y
termina en un PR aislado).

**Convención de estado por tarea**: `READY` cuando cumple la Definición de Ready
(AGENTS.md §11) y sus dependencias están `DONE`. Solo el orquestador cambia estos
estados.

> **Nota para el orquestador (DeepSeek V4 Pro)**: asigna una tarea a la vez por
> implementador (`deepseek-v4-flash`). Respeta `allowed_paths` (dos agentes nunca
> editan los mismos archivos). Tras cada tarea: revisión por un agente **distinto** +
> handoff en `docs/handoffs/PR-0NN.md`. No merge a `main` sin aprobación humana y CI
> verde.
>
> **Puerta legal (SEC-002 · Decisión D5)**: el humano dio su OK de revisión
> legal/ToS/robots **en modo prueba** (2026-08-19) → el manifest del adapter se
> declara revisado, pero la fuente se registra en BD con `enabled=false`: **ningún
> PR depende de ejecutar contra xhamster real**; todo se valida con fixtures
> sintéticos. La habilitación efectiva (backfill real, PR-065) es una acción humana
> explícita en BD.

---

## Leyenda

- **Prioridad**: P0 (imprescindible) · P1 (MVP de la fase) · P2 (importante).
- **Complejidad**: XS / S / M / L (sin XL; si algo sale XL, dividir).
- **[P]**: paralelizable con otras `[P]` que no compartan `allowed_paths` ni
  dependencias.

---

## Fase 1 — US1: adapter xhamster + fixtures + registro (P1) 🎯

### PR-062 · `XhamsterAdapter` + fixtures sintéticos + tests unitarios sin red

**Prioridad**: P1 · **Complejidad**: M · **Rol**: implementador (deepseek-v4-flash) ·
**Riesgo**: medio (estructura asumida pendiente de validación real).

**Objetivo**: implementar el adapter `xhamster` (HTML) sobre el contrato `SourceAdapter`
de la spec 002, con parsers puros testables y fixtures sintéticos anonimizados (SEC-004).
**Sin tocar el core.**

**Spec/requisitos**: FR-001…FR-006, FR-008, FR-010, SEC-001, SEC-003, SEC-004, NFR-003,
NFR-004, SC-001, SC-004, SC-005. ADR-0015. Decisiones D1/D2/D3/D5 de la spec.

**Allowed paths** (nadie más edita estos archivos):
- `services/crawler/xtrace_crawler/adapters/xhamster.py` (nuevo)
- `services/crawler/tests/fixtures/xhamster/` (nuevo: `README.md`,
  `category_page_1.html`, `category_page_2.html`, `video_page_full.html`,
  `video_page_minimal.html`, `video_page_sin_sprite.html` — sintéticos, dominio
  `.invalid`, sin media real)
- `services/crawler/tests/unit/test_xhamster_adapter.py` (nuevo)

**Contenido**:
1. **Parsers puros** (`parse_listing_page`, `parse_video_page`, helpers):
   - Listado: ítems `div.video-thumb[data-video-id]` → enlaces
     `a.video-thumb__image-container[data-role="thumb-link"][href^="/videos/"]`;
     `external_id` = sufijo de la URL canónica `/videos/<slug>-<id>` (formas numérica y
     alfanumérica, regex con anclas); `page_urls` con href completo (paridad PR-045);
     dedup por id; paginación `a.page-button-link` (cursor = path de la página
     siguiente; la página activa marca el fin) + anti-bucle.
   - Página de vídeo: `og:title`/`og:url`/`og:image` + `window.initials.videoModel`
     (`id`, `duration` s→ms, `title`, `created` epoch→`published_at`, `tags`/`keywords`
     máx. 20); sprite del vídeo principal desde
     `window.initials.spriteLoader.template` → `storyboard_urls=[template]` (los
     `data-sprite` del HTML son de vídeos relacionados y NO se usan; sin template →
     `storyboard_urls=[]`); sin señales de vídeo → `XhamsterParseError` (paridad
     `XvideosParseError`); `preview_url=None` SIEMPRE (D3, SC-004).
2. **`XhamsterAdapter`**: manifest (source=`xhamster`, access_method=`html`,
   assets_accessed=["storyboard","thumbnail"], `robots_reviewed=true`,
   `terms_reviewed=true`, `review_date="2026-08-19"` — D5, rate_limit 2000 ms / 0.5 rps
   conservador), `asset_hosts` **PROVISIONAL** con hosts observados
   (`thumb-v0..9.xhcdn.com`, `ic-vt-nss.xhcdn.com` — fail-closed), `SafeHTTPClient`
   (allowlist `xhamster.com`/`www.xhamster.com`/`es.xhamster.com` — corrección A1:
   `es.*` como objetivo de redirect/URL canónica, no como base), `discover` (D2: `section`
   obligatorio con '/' inicial, sin sección → error tipado; paginación + anti-bucle;
   truncación no soportada), `get_video` (404 → `None`; `page_url` reenviado),
   `get_visual_assets` (UN asset `storyboard` sin position/timestamp con URL del
   `data-sprite` + asset `thumbnail` de og:image; sin sprite → thumbnail solo),
   `check_availability`, `aclose`.
3. **`storyboard_grid(asset)`** exportada (ADR-0015 §3): `(N, 1)` si la URL lleva
   `.<N>.s.` (p. ej. `160x160.50.s.jpg` → 50); `(20, 1)` para `*.s.webp` sin N
   (hover sprite 526×298 → fichero real 5260×298); `None` en otro caso.
4. **Fixtures sintéticos**: reconstrucciones anonimizadas de la estructura observada
   (prospección 2026-08-19 en `/tmp/xh-*.html`, fuera del repo): ítems con
   `data-video-id` sintéticos, URLs `videos/…-<id>` en `xhamster.invalid`, sin media
   real ni títulos reales (SEC-004). README del fixture documenta la estructura
   observada, igual que `tests/fixtures/xvideos/README.md`.

**Tests requeridos** (en `test_xhamster_adapter.py`, test-first sobre parsers):
- listado: IDs dedup (ambas formas de id), page_urls, cursor/paginación, anti-bucle,
  página sin ítems → vacía sin crash, más IDs que limit → error tipado.
- página de vídeo: metadata completa (title/duration/tags/fecha/page_url), campos
  opcionales nulos, sin señales → `XhamsterParseError`, `preview_url=None`.
- assets: sprite único + thumbnail; sin sprite → thumbnail; grid resolver (20,1) y
  `None` para URL ajena.
- adapter con `httpx.MockTransport`: discover con/sin section (fail-fast), get_video
  404→None y HTTP error→propagación, check_availability available/removed/unavailable,
  allowlist (host ajeno → error).
- AST anti-acoplamiento (paridad `test_core_no_importa_el_adapter_xvideos`): ningún
  módulo del core importa `xtrace_crawler.adapters.xhamster`.

**Criterios de finalización**: `uv run ruff check . && uv run ruff format --check . &&
uv run mypy . && uv run pytest tests/unit/test_xhamster_adapter.py -q` en verde;
handoff `docs/handoffs/PR-062.md`.

---

### PR-063 · Registro en el CLI (import dinámico) + seed `xhamster` (enabled=false) + gate

**Prioridad**: P1 · **Complejidad**: S · **Rol**: implementador · **Riesgo**: bajo.

**Objetivo**: conectar el adapter al sistema exactamente como xvideos (SC-006):
registro dinámico en `_default_registry()`, wire de `storyboard_grid` en la composición
del pipeline, fila de seed con `enabled=false`, y pruebas del gate SEC-002.

**Spec/requisitos**: FR-007, SEC-002, DATA-001, SC-006, SC-007. Decisiones D5.

**Dependencias**: PR-062 `DONE`.

**Allowed paths**:
- `services/crawler/xtrace_crawler/cli.py` (solo el bloque de composición raíz)
- `services/crawler/tests/unit/test_registry.py`
- `supabase/seed.sql`

**Contenido**:
1. `cli._default_registry()`: registrar `XhamsterAdapter()` (import dinámico, `real=True`)
   junto a mock/xvideos; docstring actualizado (D5: manifest revisado en modo prueba,
   habilitación efectiva por `sources.enabled`).
2. Composición del pipeline: conectar `storyboard_grid` con import dinámico de
   `xtrace_crawler.adapters.xhamster.storyboard_grid` (ADR-0015 §3) — el core
   (`pipeline.py`) no cambia.
3. `supabase/seed.sql`: fila `xhamster` (manifest D5 con `review_date="2026-08-19"`,
   rate_limit 2000/0.5, `enabled=false`), `on conflict (name) do nothing`, comentario
   de la puerta legal (idempotente, mismo patrón que mock/xvideos).
4. `test_registry.py`: caso xhamster — manifest revisado + `enabled_in_db=false` →
   `AdapterNotEnabledError` con razón `sources.enabled=false`; con `enabled_in_db=true`
   → devuelve el adapter (D5).

**Tests requeridos**: `uv run pytest tests/unit/test_registry.py -q` en verde +
verificación de que `xtrace-crawler sources --json` lista `xhamster` (Supabase local).

**Criterios de finalización**: ruff/mypy/pytest en verde; handoff `docs/handoffs/PR-063.md`.

---

## Fase 2 — US2/US3: flujo completo offline + validación real (P1/P2)

### PR-064 · Integración end-to-end con fixtures (pipeline + Supabase local) + quickstart

**Prioridad**: P1 · **Complejidad**: M · **Rol**: implementador · **Riesgo**: medio
(integración con BD local).

**Objetivo**: demostrar el flujo completo (discover → metadata → assets → frames del
sprite con timestamp → embeddings fake → índice) con el adapter xhamster **sin red**
(fixtures + `MockTransport`), INCREMENTAL sin duplicados, y dejar el quickstart del
operador.

**Spec/requisitos**: FR-009, FR-011 (heredada), NFR-001, NFR-002, SC-002 (parcial
offline), SC-003, SC-007, US3.

**Dependencias**: PR-063 `DONE`.

**Allowed paths**:
- `services/crawler/tests/integration/test_pipeline.py` (caso xhamster nuevo; no
  reescribir casos existentes)
- `specs/007-xhamster-adapter/quickstart.md` (nuevo)
- `services/crawler/README.md` (sección xhamster)

**Contenido**:
1. Caso de integración xhamster: `XhamsterAdapter` con `MockTransport` sobre fixtures
   → pipeline con `FakeEmbeddingProvider` → vídeos únicos `(source_id, external_id)`,
   frames `source_kind=storyboard` con `timestamp_ms` derivado del grid 20×1 (clamp
   `[0, duration_ms)`), thumbnail frame sin timestamp; re-ejecución INCREMENTAL sin
   duplicados. Sprite de prueba sintético 20×1 generado en el test (paridad
   `sprite_factory.py`).
2. `quickstart.md`: prerequisitos (uv, Docker, env), `uv sync --locked`, gates de
   calidad, `supabase db reset` (seed), habilitación explícita en BD (SQL del
   operador), `backfill --source xhamster --section /categories/amateur --limit 64
   --max-videos 50`, `--incremental`, `stats`, y el deshabilitado de la fuente.
3. README del servicio: sección xhamster (estado, gate legal, rate limits).

**Tests requeridos**: `uv run pytest tests/integration/test_pipeline.py -q` en verde con
Supabase local (y los unit de PR-062/063 siguen verdes).

**Criterios de finalización**: quickstart validado end-to-end offline; handoff
`docs/handoffs/PR-064.md`.

---

### PR-065 · Validación real acotada del operador (fuera de CI) + ajustes de estructura

**Prioridad**: P1 · **Complejidad**: M · **Rol**: operador + implementador ·
**Riesgo**: medio (estructura real puede diferir de la asumida).

**Objetivo**: ejecutar el backfill real acotado tras la habilitación humana en BD y
corregir el adapter a la estructura real observada (paridad PR-042…PR-053 de xvideos).

**Spec/requisitos**: SC-002…SC-005, NFR-004, SEC-001. Decisión D4 (max-videos 50).

**Dependencias**: PR-064 `DONE` + **habilitación humana en BD**
(`update public.sources set enabled=true where name='xhamster';` — solo el operador).

**Allowed paths** (solo si la validación exige ajustes):
- `services/crawler/xtrace_crawler/adapters/xhamster.py`
- `services/crawler/tests/fixtures/xhamster/` + `tests/unit/test_xhamster_adapter.py`
- `docs/handoffs/PR-065.md` (nuevo)

**Contenido**:
1. Habilitación en BD (operador) y backfill real:
   `xtrace-crawler backfill --source xhamster --section /categories/amateur --limit 32
   --max-videos 50` (A2: `--limit 64` ≥ página real de 46–51 ítems) → verificar ≤ 50 vídeos `indexed`, frames con timestamp y
   embeddings consultables (SC-002), INCREMENTAL sin duplicados (SC-003), rate limits
   respetados (SC-005), 0 descargas de vídeo completo (SC-004), capturas reales en
   `/tmp` (nunca en el repo, SEC-004).
2. Ajustes del adapter a la estructura real observada (selectores, grid del sprite,
   hosts de CDN, paginación) **con sus tests de regresión** y re-validación.
3. Handoff de validación con evidencias (comandos, counts, logs) y nota de que la
   fuente queda deshabilitada en BD salvo que el operador decida mantenerla activa.

**Criterios de finalización**: SC-002…SC-005 evidenciadas; handoff
`docs/handoffs/PR-065.md`; spec 007 → `IMPLEMENTED` (previa revisión independiente).

---

## Grafo de dependencias

```text
PR-062 (adapter + fixtures + unit) ──► PR-063 (registro + seed + gate)
                                          │
                                          ▼
                                    PR-064 (integración offline + quickstart)
                                          │
                                          ▼
                                    PR-065 (validación real, requiere habilitación humana)
```

Nada en esta feature toca el core: `adapters/base.py`, `models.py`, `registry.py`,
`pipeline.py`, `jobs/*`, `assets/*`, migraciones y el shell Next.js quedan **fuera de
allowed_paths** (SC-006). Si un hallazgo exigiera tocarlos → **BLOCKED** y enmienda
explícita de la spec 002 (constitución §1).

## Trazabilidad (cobertura)

- PR-062: FR-001…FR-006, FR-008, FR-010, SEC-001/003/004, NFR-003/004, SC-001/004/005.
- PR-063: FR-007, SEC-002, DATA-001, SC-006/007.
- PR-064: FR-009, DATA-002 (unicidad `(source_id, external_id)`), DATA-003 (sin tipos
  de job nuevos), NFR-001/002, SC-002 (offline)/003/007, US3.
- PR-065: SC-002…SC-005, NFR-004 (reales).
