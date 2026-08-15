# Feature Specification: Source SDK + Primer Crawler (XTrace)

**Feature Branch**: `feature/002-source-sdk-crawler`

**Created**: 2026-08-15

**Status**: APPROVED

**Input**: User description: "Fase 2 — el motor ya está validado (spike 001). Ahora toca cómo
llenar el índice desde webs: contrato `SourceAdapter` + entidad normalizada `VideoSource` +
mock adapter + fixtures + harness de tests, y el **primer crawler** real
(discover → get_video → get_visual_assets → jobs en Postgres `FOR UPDATE SKIP LOCKED`, sin
Redis) con BACKFILL → INCREMENTAL, retries/backoff y rate limits. Prioridad de acceso por
fuente: API/feed oficial → sitemap → JSON → HTML → navegador (último recurso). Prioridad de
frames: storyboard/sprite > galería thumbnails > preview > vídeo (no descargar vídeos
completos)."

> Origen de requisitos: `docs/PRODUCT_IDEA.md` (fase post-spike: `SourceAdapter`, crawler,
> colas = tabla `jobs` en Postgres sin Redis, abstracciones reemplazables) y el resultado del
> spike 001 (pipeline frames → pHash → SigLIP → pgvector validado, SC-001/SC-002 superadas).
>
> Esta spec describe **qué** y **por qué**. Las decisiones técnicas concretas (librerías HTTP,
> esquema físico exacto de `jobs`, layout del paquete Python, mecanismo de worker) se fijan en
> `technical-planning` tras la **aprobación humana**.

## Objetivo

Extender XTrace del **dataset local validado en el spike** a la ingesta desde **fuentes web
reales**, de forma que el núcleo de indexación/búsqueda **nunca** vea el HTML/JSON de cada web:

1. Definir el contrato `SourceAdapter` y la entidad normalizada `VideoSource` como única
   frontera entre fuentes y core.
2. Proporcionar un **mock adapter + fixtures + harness** para desarrollar y testear todo el
   flujo sin red.
3. Construir el **primer crawler real** sobre **una única fuente** (a decidir por el humano),
   orquestado por **jobs en Postgres** (`FOR UPDATE SKIP LOCKED`, sin Redis), que alimente el
   índice visual del spike con la misma calidad validada.

El éxito de la fase es demostrar que **añadir una fuente no toca el core** y que el índice se
llena desde internet de forma permitida, medible y barata.

## Alcance

- Contrato `SourceAdapter` (async): `discover()`, `get_video(external_id)`,
  `get_visual_assets(video)`, `check_availability(video)` + **manifest de compliance**.
- Entidad normalizada **`VideoSource`**: `source`, `external_id`, `title`, `page_url`,
  `duration`, `thumbnail_url`, `preview_url`, `storyboard_urls`, `tags`, `published_at`.
- **Mock adapter + fixtures + harness de tests**: flujo completo sin red, determinista en CI.
- **Primer crawler real con UNA fuente: `xvideos.com`** (Decisión D1 — storyboards/sprite +
  thumbnails, ejercicio completo del contrato sobre HTML real), habilitada solo tras revisión
  legal/ToS/robots documentada en su manifest y aprobación humana.
- Pipeline del crawler: `discover` → `get_video` → `get_visual_assets` → **jobs en Postgres**
  (`FOR UPDATE SKIP LOCKED`) → workers → assets → frames (timestamp) → pHash + embedding →
  índice (reutilizando el pipeline del spike).
- Modos **BACKFILL** e **INCREMENTAL**, retries con backoff exponencial + jitter, rate limits
  configurables por adapter.
- Aislamiento de fallos por fuente y estadísticas básicas de operación.

## Fuera de alcance

- Más de una fuente real (las restantes candidatas llegan como adapters posteriores, cada una
  con su revisión legal).
- API FastAPI / endpoints HTTP públicos y frontend Next.js (fases posteriores).
- Búsqueda por clip con consistencia temporal (sigue diferida, Decisión D1 del spike).
- Almacenamiento o descarga de **vídeos completos** (solo assets permitidos: storyboard/sprite,
  thumbnails, previews).
- Saltarse auth, paywalls, CAPTCHA, DRM, anti-bot o acceder a contenido privado.
- Redis u otro broker externo; escalado horizontal de workers más allá del patrón básico
  `FOR UPDATE SKIP LOCKED`.
- Exposición pública y compliance de lanzamiento (puertas previas al despliegue público).
- Reindexado del dataset local del spike; el crawler solo añade contenido web.

## Actores

- **Operador** (persona técnica): habilita fuentes (tras revisión legal), lanza backfill e
  incremental, consulta el estado de jobs y la salud del crawler.
- **Crawler** (proceso interno): descubre IDs, obtiene metadata y visual assets de una fuente
  habilitada, respetando su manifest y rate limit.
- **Worker de jobs** (proceso interno): toma jobs de la tabla `jobs` en Postgres y los ejecuta
  hasta un estado terminal, con reintentos y cleanup.
- **Sistema de indexación** (proceso interno, reutilizado del spike): convierte visual assets
  en frames representativos indexados.

## Historias de usuario

### User Story 1 - Contrato SourceAdapter + VideoSource con mock, fixtures y harness (Priority: P1)

El operador quiere que el núcleo consuma cualquier fuente web a través de un contrato único y
estable, sin que el HTML/JSON de cada web contamine la indexación ni la búsqueda.

**Why this priority**: Es la base de toda la fase; sin un contrato aislado, cada fuente nueva
implicaría tocar el core (viola el objetivo de producto "añadir una fuente no toca el core").

**Independent Test**: Implementar el mock adapter y ejecutar el flujo completo
(discover → metadata → assets → jobs) con fixtures, sin red y de forma determinista en CI, y
verificar que el core no importa nada específico de una web.

**Acceptance Scenarios**:

1. **Given** el contrato `SourceAdapter` + entidad `VideoSource` + mock adapter + fixtures,
   **When** se ejecuta el flujo completo offline, **Then** se producen `VideoSource`
   normalizados y jobs persistidos correctamente, y ningún módulo del core importa código
   específico de una fuente.
2. **Given** un adapter (mock) que simula un fallo de red o HTML cambiado, **When** falla,
   **Then** el error queda contenido en el adapter y no corrompe ni bloquea el resto del
   flujo.
3. **Given** un adapter, **When** se consulta su manifest de compliance, **Then** documenta
   `source`, `access method`, `assets accessed`, `robots reviewed`, `terms reviewed`,
   `rate limit` y `review date`; y un adapter real **sin** manifest revisado no puede
   habilitarse.

### User Story 2 - Primer crawler real: de una fuente web al índice visual (Priority: P1)

El operador quiere llenar el índice desde **una** fuente web real (elegida por él y revisada
legalmente), con backfill inicial e incremental posterior, sin Redis y sin descargar vídeos
completos.

**Why this priority**: Es la validación real del SDK y el primer paso de "scale crawling
second" tras el spike. Sin crawler real, la fase no produce índice desde internet.

**Independent Test**: Con la fuente habilitada y un límite de prueba acotado, ejecutar
BACKFILL y verificar vídeos, visual assets, frames y embeddings en la BD; ejecutar de nuevo
en INCREMENTAL y verificar que solo se añade contenido nuevo (sin duplicados).

**Acceptance Scenarios**:

1. **Given** una fuente habilitada (manifest revisado), **When** se ejecuta `discover` en modo
   BACKFILL, **Then** se crean vídeos con unicidad `(source_id, external_id)` y jobs de
   metadata/visual assets en Postgres.
2. **Given** vídeos ya descubiertos de esa fuente, **When** se ejecuta INCREMENTAL, **Then**
   solo se procesan IDs nuevos o cambiados; no se duplican vídeos ni frames.
3. **Given** un job que falla por un error transitorio (p. ej. 429/timeout), **When** se
   reintenta con backoff exponencial + jitter, **Then** termina en un estado terminal
   (completado o `failed` con error registrado) y no deja artefactos temporales.
4. **Given** el rate limit declarado por el adapter, **When** el worker lo alcanza, **Then**
   espera sin violar el límite y continúa después, sin reintentos agresivos.
5. **Given** los visual assets de un vídeo (storyboard/thumbnails/preview, nunca el vídeo
   completo), **When** se procesan, **Then** se extraen frames con timestamp y se indexan con
   el pipeline del spike (pHash + embedding + ANN), reutilizando su idempotencia.
6. **Given** un vídeo de la fuente retirado o no disponible, **When** se comprueba su
   disponibilidad, **Then** queda marcado `unavailable`/`removed` y deja de consumir
   reintentos.

### User Story 3 - Aislamiento de fuentes y observabilidad básica (Priority: P2)

El operador quiere que una fuente caída o bloqueada no tumbe el crawler, y poder ver el estado
de la operación.

**Why this priority**: Es la garantía de robustez de la estrategia multi-fuente; sin
aislamiento ni métricas, operar el crawler es a ciegas.

**Independent Test**: Provocar errores persistentes en una fuente (mock) y verificar que los
jobs de otras fuentes siguen procesándose; consultar estadísticas de jobs por estado y fuente.

**Acceptance Scenarios**:

1. **Given** dos fuentes registradas, **When** una falla de forma persistente, **Then** los
   jobs de la otra continúan procesándose con normalidad.
2. **Given** jobs en curso o terminados, **When** el operador consulta el estado del crawler,
   **Then** obtiene conteos por estado y fuente, vídeos descubiertos/indexados y errores
   recientes con causa.

### Edge Cases

- Página de vídeo eliminada (404/`removed`) → estado terminal `unavailable`/`removed`, sin
  reintentos infinitos.
- HTML/JSON de la fuente cambia sin aviso → el adapter falla de forma aislada; fixtures
  versionados detectan la regresión; el vídeo queda `failed` con error.
- Respuestas 429 / bloqueo temporal / anti-bot legítimo → backoff con jitter; **nunca** se
  intenta saltar la protección.
- Vídeo sin storyboard (solo thumbnails o preview) → degradación según la jerarquía de assets,
  sin fallar todo el vídeo.
- Vídeo ya descubierto que reaparece en `discover` → upsert por `(source_id, external_id)`,
  sin duplicados.
- Crash del worker a mitad de job → el job vuelve a ser elegible (`SKIP LOCKED` + reintento)
  y el cleanup garantiza que no quedan temporales.
- Metadatos incompletos (sin `published_at`, sin tags, sin duración) → campos opcionales
  nulos; el vídeo sigue procesándose.
- Frame sin timestamp fiable (thumbnail sin referencia temporal) → frame indexado sin
  timestamp, sin fallar (paridad con FR-012 del spike).
- Contenido retirado / takedown → registro + exclusión del índice (reutiliza el mecanismo de
  exclusión del spike, FR-014).

## Requirements _(mandatory)_

### Functional Requirements

- **FR-001**: El sistema MUST definir el contrato **`SourceAdapter`** (async) con
  `discover()`, `get_video(external_id)`, `get_visual_assets(video)` y
  `check_availability(video)`, más un **manifest de compliance** por adapter.
- **FR-002**: El sistema MUST definir la entidad normalizada **`VideoSource`** con
  `source`, `external_id`, `title`, `page_url`, `duration`, `thumbnail_url`, `preview_url`,
  `storyboard_urls`, `tags` y `published_at`, como única forma de datos entre adapters y core.
- **FR-003**: El sistema MUST incluir un **mock adapter + fixtures + harness de tests** que
  permitan ejecutar el flujo completo **sin red**, de forma determinista, en CI.
- **FR-004**: El sistema MUST elegir el método de acceso de cada fuente según la prioridad
  **API/feed oficial → sitemap → JSON → HTML → navegador** y documentar la elección en el
  manifest.
- **FR-005**: El sistema MUST obtener los visual assets según la prioridad
  **storyboard/sprite → galería thumbnails → preview → vídeo**, y **MUST NOT** descargar
  vídeos completos en ningún caso.
- **FR-006**: El sistema MUST persistir los jobs en una tabla **`jobs`** en Postgres y
  despacharlos con **`FOR UPDATE SKIP LOCKED`**, sin Redis ni broker externo.
- **FR-007**: El crawler MUST soportar los modos **BACKFILL** e **INCREMENTAL** por fuente.
- **FR-008**: Los jobs MUST reintentarse con **backoff exponencial + jitter**, con un máximo
  de intentos configurable y estados terminales (`done`/`failed`/`unavailable`), sin
  reintentos infinitos.
- **FR-009**: Cada adapter MUST declarar y respetar un **rate limit configurable** (por
  adapter): **defaults en código + override por variable de entorno**, con jitter
  (Decisión D5).
- **FR-010**: El sistema MUST **aislar los fallos por fuente**: el fallo de un adapter no
  impide el procesamiento de jobs de otras fuentes.
- **FR-011**: El sistema MUST convertir los visual assets de una fuente en **frames** (con
  `timestamp` cuando el asset lo proporcione) e indexarlos **reutilizando el pipeline del
  spike** (pHash + embedding + ANN), manteniendo su idempotencia y unicidad.
- **FR-012**: El sistema MUST mantener el estado del vídeo
  (`discovered`/`pending`/`indexing`/`indexed`/`failed`/`unavailable`/`removed`) con unicidad
  por `(source_id, external_id)`.
- **FR-013**: El sistema MUST permitir **excluir un vídeo** del índice (reutilizando el
  mecanismo del spike) para takedowns/contenido retirado.
- **FR-014**: El sistema MUST registrar **estadísticas básicas** del crawler (jobs por
  estado/fuente, vídeos descubiertos/indexados, errores recientes) consultables por el
  operador.
- **FR-015**: El sistema MUST limpiar **todos los artefactos temporales** (assets descargados,
  frames en disco) incluso cuando un job falle.

### Security Requirements

- **SEC-001**: El sistema MUST NOT saltarse auth, paywalls, CAPTCHA, DRM o anti-bot, ni
  acceder a contenido privado; solo se accede a **recursos públicos legalmente accesibles**.
- **SEC-002**: **Ningún adapter de fuente real** podrá habilitarse sin manifest de compliance
  revisado (legal/ToS/robots, con `review date`) y **aprobación humana explícita**.
- **SEC-003**: El acceso a la BD desde el crawler usa credenciales de **servidor**
  (`service_role`/equivalente local), nunca expuestas a clientes; las tablas del crawler
  mantienen RLS deny-by-default (paridad con el spike).
- **SEC-004**: Los fixtures derivados de contenido real se mantienen fuera del repositorio
  (gitignored) salvo que su inclusión sea legal y se apruebe explícitamente.

### Data Requirements

- **DATA-001**: El esquema amplía el del spike con: `sources` (manifest, rate limit, estado),
  `videos` + `source_id`/`external_id` con `UNIQUE(source_id, external_id)`, y `jobs` (tipo,
  estado, intentos, backoff, error, `FOR UPDATE SKIP LOCKED`). La migración es versionada y
  no destructiva.
- **DATA-002**: Los tipos de job cubren al menos: `DISCOVER`, `FETCH_METADATA`,
  `INDEX_VIDEO`/`EXTRACT_FRAMES`, `GENERATE_EMBEDDINGS`, `CHECK_AVAILABILITY` (y `REINDEX` si
  aplica), coherentes con `PRODUCT_IDEA.md`.
- **DATA-003**: Los datos del dataset local del spike (`local_ref`) y los de fuentes web
  (`external_id`) coexisten sin colisiones de unicidad.

### Non-Functional Requirements

- **NFR-001**: El crawler en desarrollo debe ejecutarse **localmente con coste ~0 €** (CPU,
  Supabase local/Docker), sin servicios de pago.
- **NFR-002**: Un adapter caído no degrada el throughput de otros adapters (aislamiento).
- **NFR-003**: El flujo con mock adapter debe completarse **sin acceso a red** y de forma
  determinista (tests repetibles).
- **NFR-004**: El crawler respeta en todo momento los límites declarados por la fuente
  (rate limit, robots) — medible en logs y tests.

## Key Entities

- **Source**: fuente web registrada. Atributos: nombre, manifest de compliance (access
  method, assets accessed, robots reviewed, terms reviewed, rate limit, review date), estado
  (habilitada/deshabilitada).
- **Video** (ampliación del spike): añade `source_id`, `external_id` (unicidad conjunta),
  `page_url`, `title`, `duration`, `tags`, `published_at`, `thumbnail_url`, `preview_url`,
  `storyboard_urls`, y estados `unavailable`/`removed`.
- **Job**: unidad de trabajo de la cola. Atributos: tipo (`DISCOVER`/`FETCH_METADATA`/…),
  estado, `source_id`/`video_id` de referencia, nº de intentos, siguiente ejecución (backoff),
  error. Elegible con `FOR UPDATE SKIP LOCKED`.
- **VisualAsset**: referencia a un asset de la fuente (`storyboard`/`thumbnail`/`preview`)
  con su URL y prioridad/tipo; nunca el vídeo completo.
- **Frame** (reutilizado del spike): frame indexado con `video_id`, `timestamp_ms` (opcional),
  `pHash`, `embedding` y `source_kind` (ya contempla `storyboard`/`thumbnail`).

## Success Criteria _(mandatory)_

### Measurable Outcomes

- **SC-001**: El flujo completo (discover → metadata → assets → jobs) se ejecuta con el mock
  adapter **sin red**, en CI, de forma determinista.
- **SC-002**: Un **BACKFILL acotado** de la primera fuente (límite de prueba definido por el
  operador, p. ej. ≤ N vídeos) produce vídeos, frames con timestamp y embeddings
  **consultables** en el índice del spike.
- **SC-003**: Una segunda ejecución **INCREMENTAL** sobre la misma fuente **no duplica**
  vídeos ni frames (idempotente).
- **SC-004**: Todo job transitorio fallido se reintenta con backoff y termina en un estado
  final; tras cualquier job **no quedan artefactos temporales** (SC-006 del spike extendida).
- **SC-005**: El rate limit declarado por el adapter se **respeta** (0 violaciones medibles
  en logs/tests).
- **SC-006**: **0 descargas de vídeo completo**; solo assets permitidos
  (storyboard/thumbnails/previews).
- **SC-007**: Añadir un segundo adapter (mock de otra fuente) **no requiere modificar el
  core**: solo se añaden ficheros del adapter y su registro (objetivo de producto).
- **SC-008**: Una fuente fallida (mock con errores persistentes) **no impide** que los jobs
  de otra fuente se procesen.

## Assumptions

- La **primera fuente es `xvideos.com`** (Decisión D1). La **revisión legal/ToS/robots es
  responsabilidad del humano**; sin ella, el adapter real permanece **deshabilitado** y el
  desarrollo usa solo mock/fixtures.
- El código vive en **`services/crawler/`** con el SDK dentro (Decisión D3); la cola de jobs
  usa **Supabase local (Docker), la misma BD que el spike** (Decisión D4).
- El crawler **reutiliza** el pipeline y el esquema del spike (frames/pHash/embedding/ANN,
  `VectorStore`/`EmbeddingProvider`) en lugar de reimplementarlos.
- El desarrollo y las pruebas usan **Supabase local** (Docker), no la nube.
- Los fixtures se generan a partir de contenido capturado de forma permitida; el contenido
  audiovisual real **no se commitea**.
- La arquitectura concreta (librería HTTP, layout del paquete, esquema físico de `jobs`,
  mecanismo de worker) se decide en `technical-planning` con un ADR que extiende ADR-0007
  (abstracciones reemplazables).

## Dependencies

- `specs/000-platform-foundation` — esqueleto técnico, `IMPLEMENTED`.
- `specs/001-visual-search-spike` — pipeline de frames/pHash/embedding/ANN y esquema
  `videos`/`frames`, `IMPLEMENTED`.
- Revisión legal/ToS/robots del humano para la primera fuente real (puerta de habilitación).
- Supabase local operativa (Docker) para la tabla `jobs`.

## Risks

- **Cambios de HTML/JSON de la fuente** → adapters aislados + fixtures versionados que
  detectan la regresión.
- **Bloqueo / rate limits / anti-bot** → backoff con jitter, respeto estricto a límites y
  robots, aislamiento por fuente; nunca se intenta saltar protecciones.
- **Riesgo legal** → la habilitación de cada fuente real queda bloqueada hasta revisión
  humana; el desarrollo de SDK/crawler avanza con mock/fixtures sin depender de ella.
- **Alcance del backfill real** → se acota a una muestra de prueba definida por el operador
  para validar sin coste ni riesgo desproporcionados.
- **Acoplamiento accidental al mock** → el harness y los tests se diseñan contra el contrato,
  no contra la implementación mock.

## Open Questions

_Todas las preguntas críticas resueltas en la ronda de clarificación del 2026-08-15 (ver
`## Historial de decisiones`)._ Sin ambigüedades pendientes capaces de cambiar la
implementación.

## Approval

**Estado**: `APPROVED` — aprobada por el humano responsable el 2026-08-15 (aprobación
explícita: **"Especificación aprobada"**). Habilitado el paso a `technical-planning`.

## Historial de decisiones

- **2026-08-15 · Borrador inicial**: spec creada a partir del plan de fase del operador
  (Source SDK + primer crawler). Preguntas abiertas: primera fuente · alcance de la spec ·
  estructura del código · ubicación de la tabla `jobs` · configuración de rate limits.
- **2026-08-15 · D1 (Q1)**: La **primera fuente real es `xvideos.com`** (storyboards/sprite +
  thumbnails; ejercicio completo del contrato sobre HTML). La habilitación del adapter real
  queda **bloqueada** hasta la revisión legal/ToS/robots del humano; el desarrollo avanza con
  mock/fixtures. *Decisión del humano responsable.*
- **2026-08-15 · D2 (Q2)**: Esta spec cubre **SDK+mock y el primer crawler real** (US1 + US2);
  no se separa en dos specs. *Decisión del humano responsable.*
- **2026-08-15 · D3 (Q3)**: El código vive en **`services/crawler/`** con el SDK dentro
  (paquete `xtrace_crawler`, subpaquete `adapters/`); extraerlo a `packages/` después es un
  movimiento barato si hiciera falta. *Decisión del humano responsable.*
- **2026-08-15 · D4 (Q4)**: La tabla **`jobs`** vive en la **misma Supabase local (Docker)**
  que el spike (Postgres + pgvector), con `FOR UPDATE SKIP LOCKED`, sin Redis.
  *Decisión del humano responsable.*
- **2026-08-15 · D5 (Q5)**: Los **rate limits** por adapter se configuran con **defaults en
  código + override por variable de entorno**. *Decisión del humano responsable.*
- **2026-08-15 · Aprobación**: spec **`APPROVED`** por el humano responsable (frase exacta
  "Especificación aprobada"). Se habilita `technical-planning`.
