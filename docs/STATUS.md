# STATUS — XTrace

> Documento vivo para continuar el proyecto con **cualquier** agente. Lo actualiza el
> orquestador tras cada PR. Fuente de verdad de requisitos: `docs/PRODUCT_IDEA.md` y
> `specs/`. Contrato: `AGENTS.md` + `.specify/memory/constitution.md`.

**Última actualización**: 2026-08-16 · por DeepSeek V4 Pro (orquestador).

> ✅ **Fase 2 MERGEADA a `main`** (PR #3, 2026-08-16, aprobación humana; CI 6/6 verde:
> python-crawler-quality, quality, e2e, security, spec-compliance, GitGuardian).

> 🧪 **Pruebas reales del operador (2026-08-16)**: habilitadores PR-049 (`backfill
> --section`), PR-050 (`XTRACE_CRAWLER_EMBEDDINGS=fake|siglip`) y PR-051 (extra `siglip`
> + allowlist `thumbs-gcore`) mergeados a `main`. Prueba del tag `/tags/buttfucking`
> (max-videos=20): **20/20 vídeos `indexed`, 46 frames con embeddings SigLIP reales**;
> búsqueda con un thumbnail real → Top-1 `video.uuadpbta957` (0.857) y Top-2
> `video.kmcedoka08e` con visual=1.0/phash=1.0 (coincidencia exacta). Bucle completo
> captura→crawl→índice→búsqueda validado con datos reales.

## Setup de agentes (esta ejecución)

- **Orquestador**: DeepSeek V4 Pro (`deepseek-v4-pro`) → asigna tareas, resuelve dependencias, actualiza este
  archivo y `tasks.md`. **No** implementa. Único que edita `tasks.md`.
- **Implementador**: DeepSeek V4 Flash (`deepseek-v4-flash`) → ejecuta **una** tarea a la vez respetando su
  `allowed_paths`. Carga skill `task-execution` antes de tocar archivos.
- **Revisor**: un agente **distinto** al implementador (idealmente otro proveedor). Carga
  `task-review`. No implementa. Con PR-016 (puerta de decisión) revisa un modelo distinto.
- Handoff obligatorio por PR en `docs/handoffs/PR-0NN.md` (plantilla en `docs/handoffs/`).

### Política de coste/tokens (modelos)

- **`deepseek-v4-pro`** (orquestador): SOLO para orquestación y puertas de decisión (p. ej. PR-016). Coste ≈ $0.435 / $0.87 por 1M tokens (in/out).
- **`deepseek-v4-flash`** (implementador y revisor DeepSeek): modelo por defecto de ejecución, para ahorrar tokens. Coste ≈ $0.14 / $0.28 por 1M tokens.
- **Enforcement**: el orquestador fija el modelo del implementador vía `workflow.agent({ provider: "deepseek-official", model: "deepseek-v4-flash" })`, porque la tool `subagent` no expone selector de modelo (hereda el default `deepseek-v4-pro`). El revisor, si es DeepSeek, usa también flash; idealmente otro proveedor (constitución §5).

#### Qué puede hacer PRO (lista cerrada)

1. `tasks.md` (único editor), asignación y dependencias, merges y resolución de conflictos.
2. Puertas de decisión y validaciones de datos reales (p. ej. PR-016/PR-017 del spike).
3. Aprobar/rechazar la salida de un agente flash en las puertas (no rehacer su trabajo).

#### Qué debe ir a FLASH (todo lo demás)

- **Planificación**: borradores de `spec.md` (spec-authoring), `plan.md`/ADRs/contratos
  (technical-planning) y `tasks.md` inicial los produce un agente flash con un contrato
  detallado; el orquestador solo revisa y consolida. *(Corrección 2026-08-16: la fase 2
  se planificó en pro; no repetir.)*
- **Implementación y revisión**: todos los PRs, incluidos fixes pequeños de config,
  lint/format ignores, seed SQL y docs de handoff (los handoffs los escribe el implementador
  de cada PR). Si el orquestador detecta un fix trivial, crea una tarea flash (p. ej.
  PR-035), no lo aplica él mismo.
- **Verificación de gates**: re-ejecutar pytest/ruff/mypy/pgTAP/JS en worktrees es tarea de
  los agentes flash (implementador y revisor); el orquestador solo relee los informes.

#### Nota operativa

- El token de `gh` del operador está caducado (2026-08-16): los PRs a `main` los abre el
  humano (web o `gh auth login`). El push de ramas vía SSH funciona.

## Fase actual

**Fase 2 — Source SDK + Primer Crawler** (`specs/002-source-sdk-crawler`).
Estado spec: **IMPLEMENTED** (2026-08-16; aprobación humana 2026-08-15; validación real
con xvideos completada). Documentación de diseño **completa**: `plan.md`, `data-model.md`,
`contracts/`, `quickstart.md`, `tasks.md`, ADR-0009..0011.

**Validación real (2026-08-16)**: operador liberó SEC-002 ("estamos probando al menos") →
manifest revisado (PR-042) → backfill acotado real contra xvideos.com (**5/5 vídeos
`indexed`**, 10 frames con timestamp, embeddings en el índice; INCREMENTAL sin duplicados;
rate limits respetados; 0 descargas de vídeo completo). 5 hallazgos reales corregidos en
PR-043…PR-047 (selectores home, slugs, page_urls, anti-bucle). Limitación documentada: la
validación usó el proveedor de embeddings **fake** (default local); SigLIP real se activa
por config para el índice de producción (validado ya en el spike).

**Fase 1 (anterior) — Visual Search Spike**: **COMPLETADA.** PR-001…PR-018 (18 PRs) +
FIX-phash implementados, revisados (APPROVED) y **mergeados a `main`**. **US1/US2/US4
funcionales** (CLI: index/stats/search/exclude/benchmark). **Puerta SC-001/SC-002 SUPERADA
con el dataset real del operador (43 vídeos).**

## Roadmap de la fase

15 PRs (PR-019 … PR-033), ninguno XL. Ver `specs/002-source-sdk-crawler/tasks.md` para
objetivos, dependencias, `allowed_paths`, tests y criterios. Grafo de dependencias y plan
de paralelización incluidos allí.

- **PRs completados**: PR-019…PR-047 — **29/29 DONE** (revisados APPROVED y mergeados a la
  rama de fase; incluye los 6 PRs de la validación real PR-042…047). **Converge:
  CONVERGED** (FR 15/15 · SEC 4/4 · DATA 3/3 · NFR 4/4 · SC 8/8, SC-002 validado real).
- **PRs abiertos**: — (pendiente de abrir el PR de la rama de fase a `main`, con CI verde
  y aprobación humana)
- **Siguiente**: PR de `feature/002-source-sdk-crawler` a `main` → CI verde → aprobación
  humana → merge.
- **Puerta legal**: el adapter real de xvideos permanece deshabilitado hasta la revisión
  legal/ToS/robots del humano (SEC-002); el desarrollo no depende de ello (mock/fixtures).

## Primer PR recomendado y por qué

**PR-019**. Bootstrap del servicio `services/crawler/` (paquete `xtrace_crawler`, toolchain
uv/ruff/mypy/pytest, dependencia editable al spike — ADR-0011) + job de CI dedicado, sin
romper la pipeline JS ni el job del spike. Desbloquea la Ola A (PR-020/022/023/024/025 en
paralelo). Es pequeño, de bajo riesgo y necesario antes de cualquier lógica de dominio.

## Puerta de decisión del spike

**✅ MEDIDA Y SUPERADA (2026-08-15, dataset real del operador: 43 vídeos, SigLIP v1, pgvector/HNSW):**

- **SC-001: Top-5 ≥ 80% → CUMPLE: 95,6%** (Top-1 = 93,9%) con umbral de match 0.8.
- **SC-002: FPR ≤ 10% → CUMPLE: 0%** con umbral de match 0.8 (a 0.5-0.7 las negativas pasan; a 0.9 se pierde Top-5: 71,7%). **Umbral recomendado: 0.8.**
- **SC-003: latencia < 3 s → OK** (p50/p95 reportados ≈ 0-2 ms de la consulta; el coste real está en el embedding ~0.25-0.4 s/imagen en CPU, throughput medido ~2.4-3.9 fps).
- Conclusión: **VALIDATE SEARCH FIRST ✔ — se puede escalar el crawling** (decidir 30 vs 60 frames/vídeo en PR-017).
- **Validación manual del operador (2026-08-15):** un frame real subido por el operador fue buscado y el sistema devolvió el vídeo correcto (`4920517166559660298.mp4`) con timestamp acertado (~1,69 s) y score 0.872.
- **Validación manual del operador (2026-08-15, 2ª ronda):** 3 capturas reales del operador
  (`capturas-test/`, gitignored — no commitear) → **3/3 Top-1 correctos** (confirmado por el
  operador): `MAYO 2026 (386).mp4` score 0.938 @ ~51 s · `MAYO 2026 (389).mp4` score 0.912
  @ ~39,5 s · `010+AMWF+Petite+Teen+Deepthroats...480p.mp4` score 0.945 @ ~30:32. Capturas
  no idénticas (pHash 0.72-0.84), resueltas por el embedding visual. Latencia de consulta
  en CPU local 7-11 s (embeddings en CPU; no afecta a la puerta de corrección).

## Blockers conocidos

- ~~Dataset local~~ → **RESUELTO**: el operador aportó 43 vídeos en `dataset/` (gitignored,
  nunca commitear).

## Decisiones pendientes

- ~~Dimensión `D`~~ → **FIJADA: D = 768** (SigLIP v1 ViT-B-16-SigLIP, torch 2.2.2 CPU Intel-Mac, PR-005; anexado a ADR-0005). La usará la migración PR-006.
- Uso de `halfvec` vs `vector`: decidir con benchmark (PR-016), documentar en ADR-0004.

## Deuda técnica / diferido

- **Plan de despliegue público** — al llegar a la fase de exposición pública/MVP, preparar
  propuesta de despliegue en **VPS propio** (~5–10 €/mes: Postgres+pgvector, FastAPI,
  crawler, Next.js; la BD en el VPS, no en portátil), preferencia del operador frente a
  Supabase Pro/Vercel gestionados (decisión 2026-08-15).
- **Búsqueda por clip + consistencia temporal** (FR-011, SC-004) — diferida (Decisión D1),
  próxima feature.
- Crawler, `SourceAdapter` de fuentes reales (erome, xvideos, xhamster, redgifs, pornhub),
  FastAPI, frontend Next.js, admin, compliance pública — features posteriores del MVP
  (ver `docs/PRODUCT_IDEA.md`).
- ~~Persistir pHash en frames~~ → **RESUELTO (FIX-phash)**: `FrameRecord.phash` añadido;
  InMemory+Pg persisten el pHash real con codec bigint↔uint64. Pendiente para PR-013:
  exponer lectura del phash desde PgVectorStore (`get_frame` solo existe en InMemory).

## Plan de coste (objetivo)

| Escenario | Infra | Coste mensual | Coste puntual (indexación) |
| --- | --- | --- | --- |
| **Desarrollo / spike** | Local + Docker + Supabase Free + embeddings CPU local | **~0 €** | 0 € (dataset local pequeño) |
| **MVP** (~3k vídeos / 90k emb.) | Supabase Free/Pro, R2 free, crawler local/VPS ~5 €, GPU serverless por uso | **~0–25 €** | GPU serverless puntual (créditos Modal / bajo) |
| **~100k vídeos** (~3M emb.) | Supabase Pro, pgvector (medir), VPS crawler, GPU por lotes | **~25–50 €+** | Indexación por lotes en GPU serverless |
| **~1M vídeos** (~30M emb.) | Evaluar Qdrant / infra vectorial dedicada (ADR futuro) | según uso | mayor; planificar por lotes |

Principio: **cheap first, scale when proven**. No añadir servicios de pago sin ADR con
coste estimado (AGENTS.md / prompt maestro §91).

## Puertas de calidad (recordatorio)

- Python (por PR del spike): `ruff` + `mypy` + `pytest` (+ integration/pgTAP cuando aplique).
- DB: `pnpm test:db` (pgTAP).
- Skeleton JS: `pnpm verify` permanece verde (el spike no toca el app Next.js).
- Sin merge a `main` sin CI verde y **aprobación humana**. El implementador no aprueba su
  propio trabajo.
