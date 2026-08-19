# Handoff — FEATURE-007-PLANNING (adapter xhamster.com)

- **Proveedor y modelo usados**: `provider: deepseek-official · model: deepseek-v4-pro` (orquestador).
- **Resumen**: Planificación completa de la **feature 007 — adapter xhamster.com**
  (segunda fuente real). Spec `APPROVED` por el humano (frase exacta "Especificación
  aprobada", 2026-08-19), plan + ADR + tasks generados, análisis de consistencia
  ejecutado (0 CRITICAL) y correcciones A1/A2/A3 aplicadas. **Sin código de producción
  tocado**: el humano pidió dejar la feature planteada y anotada para que otro agente
  la implemente.
- **Requisitos implementados**: ninguno todavía (fase de planificación). La spec cubre
  FR-001…FR-010, SEC-001…SEC-004, DATA-001…DATA-003, NFR-001…NFR-004, SC-001…SC-007 —
  todas con tarea mapeada (cobertura 100 %).
- **Archivos modificados**:
  - `specs/007-xhamster-adapter/spec.md` (nuevo · `APPROVED`)
  - `specs/007-xhamster-adapter/plan.md` (nuevo)
  - `specs/007-xhamster-adapter/tasks.md` (nuevo · PR-062…PR-065)
  - `docs/adr/0015-xhamster-adapter-html-sprite.md` (nuevo)
  - `docs/STATUS.md` (bloque feature 007)
  - Sin cambios en `services/`, `supabase/` ni en el shell Next.js.
- **Decisiones tomadas** (todas del humano responsable, 2026-08-19):
  - **D1** host base `xhamster.com`; allowlist de página `xhamster.com`,
    `www.xhamster.com`, `es.xhamster.com` (el `es.*` solo como objetivo de
    redirect/URL canónica — corrección A1, con IP española `og:url` puede servirse en
    `es.*`).
  - **D2** discover **solo por sección** `/categories/amateur` vía `--section`;
    sin sección → error tipado (fail-fast; en v1 no se explora la home).
  - **D3** assets de v1 = **sprite/storyboard webp + thumbnail**; los previews mp4
    (`data-previewvideo`) observados NO se exponen (`preview_url=None`).
  - **D4** backfill real acotado `--limit 64 --max-videos 50` (A2: la página real trae
    46–51 ítems; `--limit` debe ser ≥ tamaño de página para no disparar el error de
    truncación del adapter).
  - **D5** puerta legal SEC-002 **OK en modo prueba** → manifest del adapter con
    `robots_reviewed=true`, `terms_reviewed=true`, `review_date="2026-08-19"`; el seed
    registra la fuente con **`enabled=false`** y la habilitación efectiva en BD sigue
    siendo acción humana.
- **Tests añadidos**: ninguno todavía (se definen en `tasks.md` para cada PR; PR-062
  incluye fixtures sintéticos anonimizados y test AST anti-acoplamiento).
- **Comandos ejecutados**: prospección factual de recursos públicos de xhamster
  (robots.txt, listado `/categories/amateur`, página de vídeo, sprite en CDN) con
  `curl`; sin accesos fuera de robots y sin bypass. Gates de calidad **no ejecutados**
  (sin cambios de código).
- **Resultados**: spec/plan/ADR/tasks coherentes (análisis: 0 CRITICAL, 0 ambigüedades,
  cobertura 100 %, 4 hallazgos menores resueltos/registrados). Feature lista para
  empezar **PR-062**.
- **Limitaciones**: la estructura de xhamster usada en la spec es **observada en
  prospección** (2026-08-19), no validada end-to-end: selectores y sprites deben
  re-validarse con fixtures y, en PR-065, contra el HTML real. **Hallazgo clave
  (2026-08-19)**: el sprite del vídeo principal vive en
  `window.initials.spriteLoader.template` (`…/<W>x<H>.<N>.s.<ext>`, tira de UNA fila;
  p. ej. `160x160.50.s.jpg` → 8000×131 → 50 tiles de 160×131, `spriteCount=50`; el
  hover sprite `526x298.s.webp` → 5260×298 → 20 tiles); los `data-sprite` del HTML de
  la página de vídeo son de vídeos **relacionados** y NO se usan. Grid resolver:
  `(N,1)` con `.<N>.s.` en la URL, `(20,1)` para `.s.webp` sin N, `None` en otro caso.
  Los captures reales del orquestador viven en **`/tmp/xh-amateur.html`,
  `/tmp/xh-video.html`, `/tmp/xh-robots-full.txt`** (nunca en el repo, SEC-004) — si
  esa máquina cambia, re-capturar con curl + UA de navegador.
- **Riesgos**: HTML cambia (→ fixtures versionados + `XhamsterParseError`); grid del
  sprite distinto en la práctica (→ constantes revisables, degradación contenida);
  paginación con saltos (`/16828`, `/33654`) (→ cursor por `a.page-button-link` +
  anti-bucle + cota `--max-videos`); bloqueo/429 (→ backoff heredado, nunca saltar
  protecciones); acoplamiento accidental al core (→ test AST + revisión independiente).
- **Trabajo pendiente** (para el siguiente agente/orquestador):
  1. Asignar **PR-062** a un implementador (`deepseek-v4-flash`): `adapters/xhamster.py`
     + `tests/fixtures/xhamster/` + `tests/unit/test_xhamster_adapter.py` (allowed_paths
     exactos en `tasks.md`).
  2. Después: PR-063 (registro CLI + seed + gate), PR-064 (integración offline +
     quickstart), PR-065 (validación real — **solo tras habilitación humana en BD**).
  3. Al terminar cada PR: revisión por agente distinto + handoff `docs/handoffs/PR-0NN.md`
     + actualizar `docs/STATUS.md`; al cerrar la feature, spec → `IMPLEMENTED`.
- **Instrucciones para el revisor**: verificar que no se toca el core (SC-006: solo
  ficheros del adapter, registro, seed y fixtures), que el manifest cumple D5, que los
  fixtures son sintéticos/anonimizados (SEC-004) y que cada PR pasa
  `uv run ruff check . && uv run mypy . && uv run pytest` en `services/crawler`.
  Fuente de verdad: `specs/007-xhamster-adapter/{spec,plan,tasks}.md` + ADR-0015.
