# 0014. Refinamiento temporal bajo demanda sobre candidatos principales

- **Estado**: Aceptada para implementación
- **Fecha**: 2026-08-18
- **Spec/Requisitos relacionados**: `006-temporal-refinement` · FR-001..014 ·
  NFR-001..004 · SEC-001..005 · DATA-001..003 · UX-001..003
- **Decisión solicitada por**: operador del producto

## Contexto

El índice global adaptativo mantiene un objetivo de ocho frames por vídeo, pero un
vídeo largo puede devolver un timestamp demasiado alejado de la captura. Aumentar la
densidad de todos los vídeos contradice ADR-0013: multiplica almacenamiento, embeddings
y coste para millones de vídeos. Los adapters ya conocen qué thumbnails/storyboards son
públicos y qué restricciones de cada fuente deben respetarse.

## Decisión

XTrace ejecutará un segundo pase síncrono después de la búsqueda base, limitado a los
tres candidatos principales (cinco como máximo) y a treinta assets adicionales por
candidato. El pase:

1. resuelve la fuente mediante `AdapterRegistry` y exige `sources.enabled` más el
   manifest de compliance;
2. obtiene únicamente `thumbnail`/`storyboard` públicos del adapter, nunca `preview` ni
   vídeo completo;
3. materializa cada imagen en memoria/temporales efímeros con allowlist, límites de
   bytes/píxeles y timeout;
4. calcula similitud contra la consulta, acepta solo timestamps respaldados por el
   asset y conserva el primer resultado si la evidencia no mejora;
5. devuelve la procedencia del timestamp y métricas resumidas, sin modificar `frames`
   ni ampliar el índice global.

Las métricas se guardan en tablas server-only relacionadas con `searches`; el borrado
por TTL de la búsqueda elimina también la telemetría. El operador puede apagar o acotar
la política por entorno/fuente.

## Alternativas consideradas

- **Reindexar con 30/60 frames globales**: mejora la densidad, pero no respeta la
  política de coste de ADR-0013 y no resuelve proveedores con pocos assets.
- **Descargar el vídeo y muestrear localmente**: fuera de alcance legal y técnico; no
  se permite vídeo completo, DRM ni controles de acceso.
- **Job asíncrono separado**: no entrega el timestamp refinado en la respuesta actual y
  añade complejidad operacional antes de validar el valor.
- **Parser propio dentro de la API**: duplicaría HTML, allowlists y revisión legal del
  crawler; se rechaza por ADR-0009.

## Consecuencias

### Positivas

- El vídeo sigue identificándose con el ranking base y el timestamp puede acercarse sin
  inflar el índice global.
- La respuesta hace visible si el timestamp es base, refinado o fallback.
- Los fallos de una fuente degradan solo el refinamiento y son medibles.

### Costes y límites

- La API incorpora la dependencia editable del crawler y su ciclo de tests.
- Cada búsqueda de candidatos web puede generar peticiones adicionales; los límites de
  adapter, rate limit, bytes y tiempo son obligatorios.
- El timestamp sigue siendo aproximado: no se presenta una precisión que el asset no
  respalde.
- La adopción como política operativa requiere el benchmark pareado de SC-001..SC-008;
  este ADR no cambia el default histórico ni dispara una reindexación.

## Seguridad y cumplimiento

La conexión a Supabase es exclusivamente server-side con RLS deny-by-default. No se
persisten imágenes de consulta, bytes de assets ni vídeos; no hay reconocimiento facial,
bypass de CAPTCHA/paywall/DRM/auth/anti-bot ni acceso a `preview`. Las URLs de evidencia
son públicas, allowlisted y sanitizadas antes de persistirlas.
