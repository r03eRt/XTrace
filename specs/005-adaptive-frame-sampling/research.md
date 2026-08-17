# Research: Muestreo adaptativo de frames

## D1 — Densidad temporal

**Decision**: Un frame cada 120 segundos, limitado a `[1, 8]`.

**Rationale**: Limita el error geométrico ideal a unos 60 segundos y evita que vídeos
cortos paguen 8 embeddings. Un vídeo de 12:38 recibe 7 puntos centrados, reduciendo el
peor salto de varios minutos a cerca de un minuto.

**Alternatives considered**: Umbrales discretos; intervalos de 60–90 segundos; 8 fijos.

## D2 — Posición de los puntos

**Decision**: Centros de intervalos uniformes, no los extremos.

**Rationale**: Reparte el error máximo y evita cabeceras/créditos frecuentes en extremos.

**Alternatives considered**: Incluir extremos; muestreo aleatorio no reproducible.

## D3 — Assets web discretos

**Decision**: Expandir assets permitidos, deduplicar y seleccionar los frames con
timestamp más cercanos a los puntos ideales sin reutilizarlos.

**Rationale**: Conserva posiciones reales sin fabricar imágenes ni timestamps.

**Alternatives considered**: Seleccionar antes de validar assets; interpolar evidencia.

## D4 — Reindexación con menos frames

**Decision**: Frontera coordinada `VideoIndexWriter` que reemplaza frames y finaliza
estado/conteo dentro de la misma unidad atómica.

**Rationale**: El upsert deja frames antiguos, `delete_video` excluye el vídeo y actualizar
el conteo después dejaría inconsistencias ante un fallo.

**Alternatives considered**: Borrar e insertar en llamadas separadas; conservar upsert.

## D5 — Activación

**Decision**: Modo adaptativo explícito; conservar 30 como default histórico hasta superar
el benchmark.

**Rationale**: ADR-0013 exige medir antes de cambiar defaults.

**Alternatives considered**: Cambiar inmediatamente el default global.

## D6 — Persistencia

**Decision**: Sin migración; reutilizar `videos.frame_count`, `frames` y `JobType.REINDEX`.

**Rationale**: El modelo ya contiene estados y claves; falta la operación.

**Alternatives considered**: Nuevas tablas de perfiles, innecesarias para la validación.

## D7 — Seguimiento de lotes

**Decision**: `run_id` en payloads existentes y consulta agregada por ese identificador.

**Rationale**: Distingue encolado de resultado final sin migración.

**Alternatives considered**: Bloquear el CLI; crear una tabla de ejecuciones.

## D8 — Dedupe web

**Decision**: Normalizar posición/timestamp, deduplicar por posición y luego por pHash
exacto, conservando el primer frame estable.

**Rationale**: Evita inflar cobertura sin añadir un umbral aproximado difícil de auditar.

**Alternatives considered**: Solo timestamp; Hamming configurable en esta fase.
