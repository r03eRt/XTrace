# Feature Specification: Visual Search Spike (XTrace)

**Feature Branch**: `feature/001-visual-search-spike`

**Created**: 2026-08-14

**Status**: APPROVED

**Input**: User description: "Spike de búsqueda visual inversa para XTrace. Demostrar de
la forma más barata posible que `captura → embedding → índice visual → vídeo correcto`
funciona suficientemente bien sobre un dataset local propio, sin crawler real, antes de
invertir en crawling masivo. VALIDATE SEARCH FIRST, SCALE CRAWLING SECOND."

> Origen de requisitos: `docs/PRODUCT_IDEA.md` (descubrimiento cerrado, `READY_FOR_SPEC`).
> ✅ **Aprobada por el humano responsable el 2026-08-14** (aprobación explícita:
> "Especificacion aprobada"). Estado: `APPROVED`.
>
> Esta spec describe **qué** y **por qué**. Las decisiones técnicas concretas
> (librerías, modelo exacto, esquema físico, proveedor de cómputo) se fijan en
> `technical-planning` tras la **aprobación humana**.

## Objetivo

Validar, con el menor coste posible, la hipótesis central de XTrace: que a partir de una
**imagen de consulta** el sistema puede localizar el **vídeo de origen**
dentro de un **índice visual** construido sobre un **dataset local controlado**,
devolviéndolo entre los primeros resultados junto a un **timestamp aproximado**.

Si esta hipótesis no se cumple con 100–3.000 vídeos, no tiene sentido invertir en
crawling masivo. El objetivo del spike es esa decisión de continuar/parar, no construir
producto.

## Alcance

- Ingesta de un **dataset local propio** de vídeos (no hay crawling ni fuentes externas).
- Extracción de **frames representativos** por vídeo.
- **Deduplicación** de frames mediante perceptual hashing.
- Generación de **firma perceptual (pHash)** y **embedding visual** por frame
  representativo.
- Persistencia de los frames indexados (metadatos, timestamp, hash, embedding) en un
  **índice consultable por similitud vectorial**.
- **Búsqueda por imagen**: dada una imagen de consulta, recuperar frames candidatos,
  agruparlos por vídeo, rankear y devolver el/los vídeos más probables con timestamp.
- **Dataset de benchmark** con variantes de consulta (exacta, comprimida, recortada, con
  watermark, redimensionada, color alterado) y muestras negativas, y medición de métricas
  de calidad y latencia.
- Interfaz mínima de validación (**CLI interna**) suficiente para ejecutar indexación,
  búsquedas y benchmarks. **No** frontend de producto.

## Fuera de alcance

- Crawlers, `SourceAdapter` de fuentes reales y descubrimiento de vídeos externos.
- Fuentes candidatas (erome, xvideos, xhamster, redgifs, pornhub) — fase post-spike.
- Frontend de producto (home, upload público, ficha de vídeo, admin).
- Autenticación, cuentas de usuario y panel de administración.
- **Búsqueda por clip / vídeo con consistencia temporal** (US3/SC-004): diferida a la
  feature siguiente (`Decisión D1`).
- Búsqueda por URL, trending/recent, vídeos relacionados como feature de producto.
- Almacenamiento permanente de vídeos originales.
- Exposición pública del sistema (bloqueada hasta cerrar compliance).
- Escalado a >3.000 vídeos, migración a vector DB dedicada u optimización de GPU.
- Reports/takedowns (más allá de la posibilidad de excluir un vídeo del índice).

## Actores

- **Operador del spike** (persona técnica): carga el dataset local, dispara la
  indexación, ejecuta búsquedas y benchmarks, interpreta métricas.
- **Sistema de indexación** (proceso interno): convierte vídeos locales en frames
  representativos indexados (extracción → dedupe → hash → embedding → persistencia).
- **Sistema de búsqueda** (proceso interno): resuelve una consulta visual contra el
  índice y produce resultados rankeados.

## Historias de usuario

### User Story 1 - Indexar un dataset local (Priority: P1)

El operador dispone de una carpeta de vídeos locales y quiere convertirlos en un índice
visual consultable.

**Why this priority**: Sin índice no hay búsqueda; es el cimiento del spike.

**Independent Test**: Ejecutar la indexación sobre un fixture pequeño (p. ej. 10 vídeos)
y verificar que se generan frames representativos, hashes y embeddings persistidos, y que
las estadísticas (nº de vídeos, frames, vectores) son coherentes.

**Acceptance Scenarios**:

1. **Given** una carpeta con N vídeos locales válidos, **When** el operador ejecuta la
   indexación, **Then** cada vídeo queda con un conjunto de frames representativos
   deduplicados, cada frame con timestamp, pHash y embedding persistidos, y el vídeo
   marcado como `indexed`.
2. **Given** un vídeo ya indexado, **When** se reindexa, **Then** no se generan
   duplicados permanentes de frames (operación idempotente).
3. **Given** un fichero corrupto o no soportado, **When** se intenta indexar, **Then** el
   vídeo se marca como `failed` con el error registrado y el resto del dataset continúa.

### User Story 2 - Buscar por imagen y encontrar el vídeo origen (Priority: P1)

El operador aporta una captura perteneciente a un vídeo del dataset y espera recuperar ese
vídeo entre los primeros resultados.

**Why this priority**: Es la hipótesis central que el spike debe validar.

**Independent Test**: Con el índice cargado, lanzar cada consulta del benchmark y comprobar
la posición del vídeo correcto (Top-1/Top-5/Top-10) y la latencia.

**Acceptance Scenarios**:

1. **Given** un índice con el dataset cargado, **When** el operador busca con una captura
   exacta de un vídeo indexado, **Then** ese vídeo aparece entre los resultados con un
   match score y, cuando el frame coincidente tiene timestamp, un timestamp aproximado.
2. **Given** una captura recortada / recomprimida / con watermark / redimensionada / con
   color alterado de un vídeo indexado, **When** se busca, **Then** el vídeo correcto
   aparece dentro del Top-5 en al menos el 80% de los casos del benchmark.
3. **Given** una imagen que no corresponde a ningún vídeo del dataset (muestra negativa),
   **When** se busca, **Then** el sistema no devuelve un falso positivo de alta confianza
   (los resultados quedan por debajo del umbral de match configurado).

### User Story 3 - Buscar por clip corto con consistencia temporal (Priority: P2) — DIFERIDA

El operador aporta un clip corto y espera que el sistema aproveche que varios frames del
clip mapean al mismo vídeo en instantes consecutivos para elevar la confianza.

**Why this priority**: Refuerza la validación y demuestra el diferencial del video search,
pero la imagen sola ya constituye MVP del spike.

> **DIFERIDA (Decisión D1, 2026-08-14)**: fuera del alcance de este spike. Se implementa
> en la feature siguiente. FR-011 y SC-004 quedan marcados como diferidos.

**Independent Test**: Con clips del benchmark, comprobar que el vídeo correcto sube de
ranking respecto a usar un único frame, y que hay evidencia de coincidencia temporal.

**Acceptance Scenarios**:

1. **Given** un clip corto de un vídeo indexado, **When** se buscan sus frames muestreados,
   **Then** varias coincidencias apuntan al mismo `video_id` en timestamps crecientes y
   próximos, y el vídeo correcto queda como más probable.
2. **Given** un clip de un vídeo indexado, **When** se compara el resultado con la búsqueda
   por un único frame, **Then** la confianza del vídeo correcto es igual o mayor con
   consistencia temporal.

### User Story 4 - Medir calidad y coste con un benchmark (Priority: P2)

El operador quiere una medición reproducible que soporte la decisión continuar/parar.

**Why this priority**: El resultado del spike ES esta medición; sin ella la validación no
es concluyente.

**Independent Test**: Ejecutar el benchmark completo y obtener un informe con Top-1/Top-5/
Top-10, latencia, nº de frames/vídeo, tamaño del índice y throughput de embedding.

**Acceptance Scenarios**:

1. **Given** el dataset de benchmark, **When** se ejecuta la evaluación, **Then** se
   produce un informe reproducible con Top-1, Top-5, Top-10, latencia p50/p95, frames por
   vídeo, tamaño del índice y throughput de embeddings.
2. **Given** varias configuraciones de frames por vídeo (p. ej. 10/30/60/scene-based),
   **When** se evalúan, **Then** el informe permite comparar precisión frente a coste.

### Edge Cases

- Vídeo muy corto o sin escenas distinguibles (pocos frames representativos tras dedupe).
- Vídeo donde todos los frames son casi idénticos (dedupe agresivo deja muy pocos frames).
- Consulta con imagen minúscula, muy comprimida o casi monocroma.
- Consulta con imagen que coincide parcialmente con varios vídeos (ambigüedad de ranking).
- Frame indexado sin timestamp fiable (resultado sin timestamp, sin fallar la búsqueda).
- Fallo a mitad de indexación de un vídeo: no deben quedar temporales ni frames a medias.
- Dataset con dos vídeos casi idénticos (near-duplicate a nivel de vídeo).

## Requirements _(mandatory)_

### Functional Requirements

- **FR-001**: El sistema MUST ingerir un **dataset local de vídeos** desde una ubicación
  configurable, sin depender de fuentes externas ni de red en tiempo de indexación.
- **FR-002**: El sistema MUST extraer **frames representativos** de cada vídeo con una
  estrategia de muestreo configurable (nº o densidad de frames por vídeo).
- **FR-003**: El sistema MUST **deduplicar** frames casi idénticos mediante perceptual
  hashing con un umbral **configurable**, conservando un subconjunto representativo.
- **FR-004**: El sistema MUST calcular una **firma perceptual (pHash)** por frame
  representativo para matching near-exact.
- **FR-005**: El sistema MUST calcular un **embedding visual** por frame representativo
  para matching semántico, procesando en **batches** cuando sea posible.
- **FR-006**: El sistema MUST persistir por frame: `video_id`, `timestamp` (cuando exista),
  `pHash`, `embedding` y metadatos mínimos, en un **índice consultable por similitud
  vectorial (ANN)**.
- **FR-007**: El sistema MUST asociar cada frame a su **vídeo** y mantener el estado del
  vídeo (`discovered`/`pending`/`indexing`/`indexed`/`failed`).
- **FR-008**: La indexación MUST ser **idempotente**: reindexar un vídeo no genera
  duplicados permanentes de frames (clave estable por vídeo + posición/timestamp).
- **FR-009**: El sistema MUST **eliminar todos los artefactos temporales** (frames en disco,
  descargas parciales) incluso cuando un job falle.
- **FR-010**: El sistema MUST ofrecer **búsqueda por imagen**: normaliza la imagen, calcula
  pHash y embedding, ejecuta ANN, recupera frames candidatos, los **agrupa por vídeo** y
  devuelve vídeos rankeados.
- **FR-011** _(DIFERIDO — Decisión D1)_: El sistema ofrecerá **búsqueda por clip**
  (muestreo de frames + **consistencia temporal**) en la feature siguiente. Fuera del
  alcance de este spike.
- **FR-012**: Cada resultado MUST incluir un **match score** y, cuando el frame coincidente
  tenga timestamp, un **timestamp aproximado** de coincidencia.
- **FR-013**: El ranking MUST combinar, como punto de partida configurable, **similitud
  visual**, **nº de frames coincidentes** y **evidencia de pHash**. Los pesos MUST ser
  configurables (no requisitos fijos). La **consistencia temporal** se añadirá con la
  búsqueda por clip (diferida, Decisión D1).
- **FR-014**: El sistema MUST permitir **excluir un vídeo** del índice de forma que deje de
  aparecer en resultados sin borrar necesariamente de forma irreversible sus registros.
- **FR-015**: El sistema MUST proporcionar un **dataset de benchmark** de **~210 casos**:
  **~30 casos por cada una de las 6 variantes positivas** (exacta, comprimida, recortada,
  watermark, redimensionada, color alterado) = ~180 positivos, más **~30 muestras
  negativas** (Decisión D3). La variante *clip corto* queda fuera (Decisión D1).
- **FR-016**: El sistema MUST producir un **informe de benchmark reproducible** con Top-1,
  Top-5, Top-10, latencia (p50/p95), frames por vídeo, tamaño del índice y throughput de
  embeddings.
- **FR-017**: El sistema MUST exponer una **CLI interna** para lanzar indexación, búsquedas
  y benchmarks (Decisión D2). No requiere frontend ni endpoint HTTP en el spike.
- **FR-018**: El sistema MUST **no** almacenar de forma permanente los vídeos originales del
  dataset como parte del índice (se guardan metadatos, hash, embedding y timestamp).

### Key Entities

- **Video**: unidad indexable del dataset local. Atributos: identificador local estable,
  referencia al fichero de origen, duración, estado, timestamps de indexación. Restricción
  de unicidad por identificador local.
- **Frame**: frame representativo de un vídeo. Atributos: `video_id`, `timestamp` (opcional),
  `pHash`, `embedding`, dimensiones, tipo de origen. Es la unidad de búsqueda.
- **Search**: registro de una consulta ejecutada. Atributos: tipo (imagen/clip), tiempo de
  proceso, nº de resultados. No persiste el contenido multimedia de la consulta.
- **SearchResult**: vídeo candidato para una búsqueda. Atributos: `video_id`, match score,
  nº de frames coincidentes, timestamp aproximado, evidencia (pHash/temporal).
- **BenchmarkCase**: caso de evaluación. Atributos: imagen/clip de consulta, variante,
  vídeo esperado (o "ninguno" para negativas).

## Success Criteria _(mandatory)_

### Measurable Outcomes

- **SC-001**: En el dataset de benchmark, el **vídeo correcto aparece en el Top-5 en ≥ 80%**
  de los casos positivos (incluyendo variantes comprimida, recortada, watermark,
  redimensionada y color alterado). **Puerta de decisión del spike.**
- **SC-002**: Las **muestras negativas** no producen falsos positivos de alta confianza: el
  mejor resultado queda por debajo del umbral de match configurado en ≥ 90% de las
  negativas.
- **SC-003**: La **búsqueda responde en < 3 s** (objetivo, medido; p95 reportado) sobre el
  dataset del spike.
- **SC-004** _(DIFERIDO — Decisión D1)_: La **búsqueda por clip** con consistencia temporal
  iguala o mejora el Top-5 respecto a un único frame. Se evaluará en la feature siguiente.
- **SC-005**: La indexación de un vídeo es **idempotente**: reindexar no incrementa el nº de
  frames del vídeo.
- **SC-006**: Tras cualquier job (con o sin error) **no quedan artefactos temporales** en
  disco.
- **SC-007**: El **informe de benchmark es reproducible**: dos ejecuciones sobre el mismo
  dataset y configuración producen las mismas métricas de calidad.

## Assumptions

- El spike opera sobre un **dataset local propio**; el operador es responsable de la
  legalidad del material usado para validar.
- El sistema del spike **no se expone públicamente**; no requiere 18+, cuentas ni compliance
  para funcionar (esas puertas aplican antes de cualquier lanzamiento público).
- La media de consulta se **borra inmediatamente** tras procesar cada búsqueda
  (`ASSUMPTION-6` confirmada en descubrimiento).
- Límites iniciales de consulta (configurables, a validar por UX/benchmark): imagen ≤ 10 MB,
  clip ≤ 50 MB y ≤ 30 s.
- El enfoque técnico confirmado en descubrimiento (perceptual hash + embeddings visuales +
  índice ANN sobre Postgres/pgvector, cómputo local/serverless) se toma como **dirección**;
  la **elección exacta** de modelo, librerías y esquema físico se decide en
  `technical-planning`, no en esta spec.

## Dependencies

- `specs/000-platform-foundation` (esqueleto técnico, `IMPLEMENTED`).
- Disponibilidad de un dataset local de vídeos aportado por el operador.
- Herramientas de proceso de vídeo/imagen y de índice vectorial disponibles en local
  (detalle a fijar en planning).

## Risks

- **Calidad del dataset**: un dataset poco representativo puede invalidar la medición.
- **Falsos positivos**: escenas visualmente genéricas pueden confundir el ranking.
- **Suficiencia de frames**: la hipótesis de 20–50 frames/vídeo puede no bastar; se mide con
  varias configuraciones.
- **Coste/tiempo de embeddings**: throughput insuficiente en local podría requerir cómputo
  serverless antes de lo previsto.
- **Rendimiento del índice vectorial** con ~90k vectores: a medir; si insuficiente, se
  documenta como hallazgo (no se resuelve escalado en el spike).

## Open Questions

_Todas las preguntas críticas resueltas en la ronda de clarificación del 2026-08-14 (ver
`## Historial de decisiones`)._ Sin ambigüedades pendientes capaces de cambiar la
implementación.

## Approval

**Estado**: `APPROVED` (aprobada por el humano el 2026-08-14). Habilitado el paso a
`technical-planning`.

## Historial de decisiones

- **2026-08-14 · D1 (Q3)**: El spike se limita a **búsqueda por imagen**. La **búsqueda por
  clip con consistencia temporal** (US3, FR-011, SC-004) se **difiere** a la feature
  siguiente. *Decisión del humano responsable.*
- **2026-08-14 · D2 (Q2)**: La interfaz de validación del spike es una **CLI interna**
  (sin endpoint HTTP ni frontend). *Decisión del humano responsable.*
- **2026-08-14 · D3 (Q1)**: El dataset de benchmark tendrá **~210 casos**: ~30 por cada una
  de las 6 variantes positivas (~180) + ~30 negativas. *Decisión del humano responsable.*
