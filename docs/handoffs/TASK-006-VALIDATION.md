# TASK-006-T028 — Validación constitucional

**Fecha:** 2026-08-19
**Rama:** `feature/006-temporal-refinement`
**Agente:** `/root/luna_t028_validation` (GPT-5.6 Luna)
**Spec:** `specs/006-temporal-refinement/spec.md` (`APPROVED`)
**Contrato:** `specs/006-temporal-refinement/contracts/TASK-006-T028.md`

## Alcance y preflight

- `AGENTS.md`, la constitución, `spec.md`, `plan.md`, `tasks.md` y el contrato T028
  fueron leídos antes de validar.
- La rama activa es la rama de feature esperada.
- No se modificó código, configuración ni tests durante esta tarea. El único archivo
  permitido y creado por T028 es este handoff.
- La documentación T027 quedó integrada dentro de sus `allowed_paths` y dispone de
  handoff propio; la validación se ejecutó sobre el árbol integrado final.
- No se reinició Electroelite ni se detuvo Supabase. Para WDIO se usó únicamente un
  servidor Next local temporal y el stub HTTP del repositorio.

## Gates frontend y base de datos — orden constitucional

| Gate | Comando | Resultado |
| --- | --- | --- |
| Formato | `pnpm format:check` | **PASS** — todos los archivos usan Prettier |
| Lint | `pnpm lint` | **PASS** |
| TypeScript | `pnpm typecheck` | **PASS** |
| Unit/componentes | `pnpm test` | **PASS** — 5 archivos, 35 tests |
| Supabase/pgTAP | `pnpm test:db` | **PASS** — 4 archivos, 173 tests; incluye `temporal_refinement_schema.test.sql` |
| E2E WebdriverIO | `pnpm test:e2e` con Next temporal en 3000 | **PASS** — 2 spec files, 8 tests; home y búsqueda base/refinada/limited/unavailable |
| Build | `pnpm build` | **PASS** — Next 16.3.0, rutas `/`, `/_not-found` y `/buscar` |

La ejecución constitucional completa también se repitió con:

```text
pnpm verify
```

Resultado: **PASS**. Para que el gate fuera reproducible, se levantó temporalmente
`pnpm start` en `http://localhost:3000`; `wdio` levantó y cerró su stub en
`127.0.0.1:8000`. El servidor temporal fue detenido al finalizar.

### Incidencia de infraestructura E2E registrada

Se ejecutó primero `pnpm test:e2e` con la API real ocupando `127.0.0.1:8000`. El
runner actual tiene el puerto del stub fijado a 8000, por lo que se obtuvo
`EADDRINUSE`; la spec home pasó y los 7 casos de búsqueda fallaron con `fetch failed`.
Después se dejó el puerto libre y se ejecutó la suite con el servidor Next temporal:
los 8 casos pasaron. No se oculta el primer fallo: es un conflicto de entorno, no un
fallo observado con el stub en un entorno aislado.

## Backend API

Ejecutados desde `services/api` cuando la suite requería ese root de pytest:

| Comprobación | Resultado |
| --- | --- |
| `uv run --project services/api ruff check services/api/xtrace_api services/api/tests` | **PASS** |
| `uv run --project services/api mypy services/api/xtrace_api` | **PASS** — 23 archivos |
| `uv run pytest` | **162 PASS, 2 FAIL** |
| Suite dirigida de refinamiento/búsqueda/esquema | **PASS** — 123 tests |

Los dos fallos de la suite API global son:

1. `tests/integration/test_stats.py::test_stats_coherent_with_cli_stats`: el
   fixture no crea el directorio temporal `work` antes de guardar `query.png`.
2. `tests/integration/test_videos.py::test_video_card_full_record_pg`: el fixture
   existente inserta `tags` como texto no JSON (`{buttfucking...}`), rechazado por la
   columna JSON.

Ambos fallos están en fixtures/integraciones existentes y no aparecieron en la suite
dirigida de T028; no se modificaron porque el alcance de esta tarea es observación y
registro.

El mypy API incluyendo tests se ejecutó además para no ocultar errores:

```text
uv run --project services/api mypy services/api/xtrace_api services/api/tests
```

Resultado: **FAIL — 23 errores en 10 archivos de tests**. El código de producción
`services/api/xtrace_api` pasa sin errores; los errores restantes son anotaciones de
fixtures/mocks, imports sin stubs y llamadas de tests existentes/nuevos. Deben
resolverse o justificarse antes de considerar verde un gate estricto que incluya
mypy de tests.

## Crawler

| Comprobación | Resultado |
| --- | --- |
| `uv run --project services/crawler ruff check services/crawler/xtrace_crawler services/crawler/tests` | **PASS** |
| `uv run --project services/crawler mypy services/crawler/xtrace_crawler` | **PASS** — 23 archivos |
| `uv run pytest` desde `services/crawler` | **PASS** — 368 tests, 48 skips |
| `uv run pytest tests/unit/test_xvideos_adapter.py tests/unit/test_refinement_asset_contract.py` | **PASS** — 64 tests |

El mypy crawler incluyendo tests también se ejecutó:

```text
uv run --project services/crawler mypy services/crawler/xtrace_crawler services/crawler/tests
```

Resultado: **FAIL — 88 errores en 14 archivos de tests**. El código de producción del
crawler pasa; los errores son principalmente mocks/adapters tipados de forma más
estrecha, imports sin stubs y fixtures heredados. No se cambiaron en T028.

## Otros comandos de control

- `pnpm exec vitest run tests/unit/temporal-refinement-benchmark.test.ts
  tests/unit/api-contract.test.ts tests/unit/buscar-page.test.tsx`: **PASS** — 3
  archivos, 32 tests.
- `git diff --check`: **PASS**.
- La ejecución accidental de `uv run --project services/api pytest` desde la raíz no
  se considera resultado de suite: mezcló los tres paquetes y produjo colisiones de
  módulos `tests.*`. Las suites válidas se repitieron desde sus respectivos roots.

## Temporales y artefactos de validación

Medición posterior a las ejecuciones, sin borrar artefactos generados:

| Ubicación | Tamaño observado | Nota |
| --- | ---: | --- |
| `.next/` | 124 MB | artefacto/cache de `next build` |
| `tests/e2e/screenshots/` | 760 KB | capturas de los intentos E2E; ubicación ignorada por Git |
| `tests/e2e/.reports/` | 16 KB | reportes JUnit de WDIO |
| `/tmp/xtrace-video-probe.*` | 9.4 MB | temporal existente del entorno; no creado por T028 |
| `/tmp/xtrace-web-gallery/` | 22 MB | corpus/temporal existente; no creado por T028 |

No se descargaron vídeos completos ni se escribieron frames durante la validación.
Los puertos 3000 y 8000 quedaron libres al finalizar.

## Resultado de T028

Los gates constitucionales del frontend, Supabase, E2E aislado y build están verdes.
Las suites Python dirigidas y el crawler global están verdes. Permanecen dos fallos
de integración API global y errores de mypy al incluir tests; ambos quedan
documentados y no fueron ocultados ni corregidos fuera del alcance de T028.

### Instrucciones para el revisor/orquestador

1. Revisar los dos fallos API globales y decidir si se corrigen en una tarea separada
   o se documentan como deuda preexistente.
2. Decidir si el gate oficial exige mypy de tests; si es así, crear tareas acotadas
   para los 23 errores API y 88 del crawler, sin relajar el chequeo.
3. Mantener la ejecución E2E aislada (frontend temporal + stub libre en 8000) en CI o
   parametrizar el puerto del stub antes de fusionar.
