# 0012. Servicio FastAPI de búsqueda reutilizando xtrace_spike (patrón ADR-0011)

- **Estado**: Aceptada
- **Fecha**: 2026-08-16
- **Spec/Requisitos relacionados**: 003-search-mvp · FR-001, FR-004, FR-005, FR-011,
  NFR-003, SEC-001, DATA-001 · relacionada con ADR-0003, ADR-0007, ADR-0008, ADR-0011

## Contexto

La fase 3 (MVP de búsqueda usable) necesita exponer el pipeline de búsqueda del spike como
**API REST** (`POST /search`, `GET /health`, `GET /stats`, `GET /videos/{id}`, D1) con
**paridad garantizada** con la CLI `search` ya validada (SC-001), y un frontend mínimo que la
consuma. ADR-0008 difirió FastAPI hasta esta fase; ahora hay necesidad de HTTP.

Este es el **tercer consumidor** de `xtrace_spike` (tras la CLI del propio spike y el
crawler): ADR-0011 fijó el criterio de reevaluar la extracción de un paquete compartido
(`packages/xtrace-core`) cuando un tercer servicio necesitara lo mismo.

## Decisión

Crear un **tercer servicio Python** en `services/api/` (paquete `xtrace_api`, **FastAPI** +
uvicorn + python-multipart) que **reutiliza `xtrace_spike` como dependencia de camino
editable** (mismo patrón que el crawler, ADR-0011) y **no modifica el spike**. Reutiliza
directamente: `ImageSearch` (normalizar → pHash → embed → ANN → agrupar), `rank_candidates`
(ranking con pesos y umbral), `security.py` (`validate_query_image` +
`QueryMediaContext` + `open_query_image`), `PgVectorStore`, `PgRepo` y los proveedores de
embeddings (`FakeEmbeddingProvider`/`SiglipLocalProvider`, selección por env). La API añade
solo: transporte HTTP, modelos de contrato (pydantic), gestión de la subida multipart, y la
analítica `searches` con TTL configurable (FR-012).

**Fronteras de la fase**: sin auth y bind `127.0.0.1` (SEC-001, D3); la API **no se
despliega** (D4); sin migraciones ni tablas nuevas (DATA-001 — `searches`/`videos`/`frames`
tal cual; TTL por cleanup de `created_at`); el contrato de `/search` reutiliza el JSON de la
CLI (FR-004). El escalado (`halfvec`, vector DB dedicada) sigue diferido (ADR-0004).

**Reevaluación del trigger de ADR-0011**: se mantiene la dependencia editable en lugar de
extraer `packages/xtrace-core` ahora. Trigger de extracción futuro: un **cuarto** consumidor,
o la necesidad de **modificar** código compartido (el spike hoy solo se lee).

## Alternativas consideradas

- **Extraer ya `packages/xtrace-core`** — más puro a largo plazo, pero: toca el spike
  cerrado (19 ramas de PR preservadas), añade fricción de empaquetado (nuevo paquete + lock +
  CI) sin necesidad inmediata, y el API solo **consume** API pública existente del spike (no
  necesita cambiar nada). Diferida con trigger explícito.
- **Reimplementar la búsqueda en el API** — rompe la paridad API-CLI por construcción
  (SC-001) y duplica mantenimiento. Rechazada.
- **Añadir los endpoints al crawler o al spike** — mezcla responsabilidades (ingesta vs
  búsqueda) y tocaría el spike cerrado. Rechazada.
- **Implementar la API en TypeScript/Node** — ecosistema pobre para embeddings/pgvector;
  contradice ADR-0003. Rechazada.
- **Desplegar la API (Vercel/serverless)** — prohibido por D4/ASSUMPTION-2 (compliance
  pendiente). Rechazada en esta fase.

## Consecuencias

- (+) **Paridad API-CLI por construcción**: la cadena de búsqueda es el mismo código del
  spike (FR-005, SC-001), con los mismos defaults (`top_k=10`, `min_score=0.0`,
  `DEFAULT_WEIGHTS`).
- (+) El spike permanece intocado; validación y borrado seguro de media (SEC-002/003)
  heredados de `security.py` sin reimplementación.
- (+) CI aditiva (workflow `python-api-quality`), sin tocar la pipeline JS ni los jobs
  existentes; E2E del frontend con stub (sin API real en CI).
- (−) Tercer servicio Python en el monorepo (un lockfile y un job de CI más); acoplamiento
  de paquete con el spike ya aceptado en ADR-0011.
- (−) FastAPI introduce dependencias nuevas (`fastapi`, `uvicorn`, `python-multipart`,
  `httpx` en dev para TestClient); contenidas en `services/api/`.
- (−) Los embeddings en CPU (7-11 s medidos) bloquean un worker thread por petición
  (handlers sync en threadpool de FastAPI); aceptable para operador único local, se mide y
  reporta (SC-004) sin garantizarlo.
- (−) Sin migraciones, el TTL de `searches` es un cleanup periódico por `created_at` (no
  expiración de filas); limitación documentada en el plan (DATA-001).
