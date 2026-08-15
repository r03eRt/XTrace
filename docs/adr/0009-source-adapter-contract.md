# 0009. Contrato SourceAdapter + VideoSource normalizado + manifest de compliance

- **Estado**: Aceptada
- **Fecha**: 2026-08-15
- **Spec/Requisitos relacionados**: 002-source-sdk-crawler · FR-001, FR-002, FR-004,
  SEC-001, SEC-002, SC-007 · extiende ADR-0007

## Contexto

La fase 2 añade fuentes web (xvideos primero, luego más). El HTML/JSON de cada web cambia
sin aviso y su acceso está sujeto a límites legales y técnicos. Acoplar el core (indexación/
búsqueda del spike) a una web concreta haría que cada fuente nueva —o cada cambio de layout—
exigiera tocar el núcleo, violando el objetivo de producto "añadir una fuente no toca el
core" (SC-007) y exponiendo al sistema a riesgos legales por fuente.

## Decisión

Definir el contrato **`SourceAdapter`** (async) con `discover()`, `get_video(external_id)`,
`get_visual_assets(video)` y `check_availability(video)`, y la entidad normalizada
**`VideoSource`** (`source`, `external_id`, `title`, `page_url`, `duration`, `thumbnail_url`,
`preview_url`, `storyboard_urls`, `tags`, `published_at`) como **única frontera de datos**
entre adapters y core. El core nunca ve HTML/JSON de la web.

Cada adapter declara un **`AdapterManifest`** de compliance: `source`, `access_method`
(con la jerarquía API/feed oficial → sitemap → JSON → HTML → navegador documentada),
`assets_accessed`, `robots_reviewed`, `terms_reviewed`, `rate_limit` y `review_date`.
El `registry` **no habilita** un adapter real sin `robots_reviewed`/`terms_reviewed` en
`true` y aprobación humana (SEC-002). Incluye un **mock adapter** determinista para
desarrollar y testear sin red (FR-003).

## Alternativas consideradas

- **Cada fuente integrada directamente en el pipeline** — simple al principio, pero cada
  cambio de HTML toca el core y mezcla riesgo legal con dominio. Rechazada.
- **Protocolo por fuente sin manifest** — sin puerta de compliance, el riesgo legal no queda
  documentado ni bloqueado por diseño. Rechazada.
- **Adapters en proceso separado (microservicios)** — aislamiento total pero infraestructura
  y operación desproporcionadas para una fuente. Rechazada (revisable a futuro).

## Consecuencias

- (+) Añadir una fuente = solo adapter + registro + revisión legal (SC-007 medible).
- (+) Fallos de HTML/red contenidos por adapter (FR-010, SC-008); fixtures sintéticos para
  regresiones.
- (+) Puerta legal explícita y auditable por fuente (manifest con `review_date`).
- (−) Una capa de indirección más (paridad con ADR-0007, ya aceptada en el spike).
