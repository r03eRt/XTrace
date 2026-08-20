# Feature Specification: Adapter redgifs.com (fuente real vía API oficial)

**Feature Branch**: `feature/008-redgifs-adapter`

**Created**: 2026-08-19

**Status**: APPROVED (implementación reanudada 2026-08-20 por instrucción explícita del humano; ver `plan.md`, ADR-0016 y `tasks.md`)

**Input**: User description: "puedes hacerlo ahora el adaptador de redgif,
https://www.redgifs.com/niches/homemade y
https://www.redgifs.com/niches/real-cellphone-clips por ejemplo"

> Origen de requisitos: `docs/PRODUCT_IDEA.md` (fuente candidata nº 4: `redgifs.com`,
> "gifs cortos | Posible API/feed; previews animados") y la spec 002 (SDK + primer
> crawler) — donde "más de una fuente real" quedó explícitamente **fuera de alcance**:
> cada fuente nueva es un adapter posterior con su propia revisión legal/ToS/robots.
> El contrato `SourceAdapter` ya está implementado y probado con `xvideos.com`,
> `xhamster.com` (spec 007) y `erome.com`. Esta spec describe **qué** y **por qué**;
> las decisiones técnicas concretas se fijan en `technical-planning` tras la
> **aprobación humana**.

## Objetivo

Añadir la **fuente real `redgifs`** al SDK de crawling de XTrace — **`redgifs.com`**,
empezando por sus nichos `homemade` y `real-cellphone-clips` (URLs aportadas por el
usuario) — **sin tocar el core** de indexación/búsqueda:

1. Implementar el adapter real `redgifs` que cumple el contrato `SourceAdapter`
   (spec 002 · FR-001): `discover()`, `get_video()`, `get_visual_assets()`,
   `check_availability()` + **manifest de compliance**.
2. Ser el **primer adapter con `access_method="api"`** (nivel 1 de la jerarquía
   FR-004: API/feed oficial → sitemap → JSON → HTML → navegador): RedGIFs expone una
   **API oficial pública** (`api.redgifs.com`) con token temporal sin clave
   (`GET /v2/auth/temporary`, documentado por RedGIFs en github.com/Redgifs/api).
3. Aprovechar los visual assets que la fuente expone de forma pública: **thumbnail**
   y **poster** (imágenes estáticas en `media.redgifs.com`). RedGIFs **no tiene
   storyboard/sprite**: cada ítem (gif/clip corto o imagen) expone a lo sumo estas
   dos imágenes, sin semántica temporal → frames **sin timestamp**
   (`timestamp_ms=None`, paridad FR-012 del spike). Según la jerarquía FR-005
   (storyboard/sprite → thumbnails → preview → vídeo), **nunca** los mp4
   (`sd`/`hd`/`silent`): en RedGIFs el mp4 **es el contenido completo del ítem**
   (prohibido por SC-006 de la spec 002: 0 descargas de vídeo completo).
4. Registrar la fuente en el SDK (registry + seed en BD) **deshabilitada** hasta la
   revisión legal/ToS/robots del humano (gate SEC-002), con fixtures sintéticos para
   desarrollar y testear todo el flujo **sin red**.
5. Tras la aprobación humana, ejecutar un **backfill acotado real** de los nichos de
   validación (`homemade`, `real-cellphone-clips`) que alimente el índice visual del
   spike, y demostrar que **añadir una fuente no toca el core** (SC-007 de la spec
   002, objetivo de producto).

El éxito de la fase es demostrar que el SDK soporta una fuente con **estructura y
mecanismo de acceso radicalmente distintos** a los anteriores (API oficial JSON en
vez de HTML; ítems cortos sin storyboard; token efímero de API), **sin modificar el
core**.

## Alcance

- Adapter real **`redgifs`** (API oficial — `access_method="api"`, jerarquía FR-004
  nivel 1) sobre el contrato existente `SourceAdapter` (spec 002), reutilizando
  `SafeHTTPClient`, rate limiter, pipeline, jobs y repositorios **sin cambios en el
  core**.
- **Token temporal de API**: `GET https://api.redgifs.com/v2/auth/temporary` devuelve
  un JWT público (`scope=read`, sin clave, validez ≈ 24 h; el token responde
  `rate:-1`, sin límite explícito declarado). El adapter obtiene el token al arrancar
  el discover y lo **renueva automáticamente** ante `401` (rotación con backoff);
  **nunca** se loguea ni se persiste.
- **Discover por nicho**: entrada inicial `/niches/<id>` (p. ej. `/niches/homemade`)
  vía el mecanismo `section` ya existente en el contrato (`discover(section=...)` +
  `--section <path>` del CLI, PR-049 de la spec 002). El adapter traduce la sección
  al endpoint `GET /v2/niches/<id>/gifs?order=new&count=100&page=N`, con paginación
  por **`page`** (1-based; verificado 2026-08-19: el envelope trae `page`/`pages`/
  `total`; el campo `cursor` del envelope no pagina este endpoint) y protección
  anti-bucle (página repetida / 0 IDs nuevos / `page >= pages` → fin).
  **Sin `section`, o con una sección que no empiece por `/niches/`, el adapter
  rechaza el discover** (fail-fast, Decisión propuesta D2): en v1 solo se exploran
  nichos, no tags ni búsqueda.
- `get_video()`: metadata normalizada desde el objeto gif de la API (`GET
  /v2/gifs/<id>`; el listado ya trae el objeto completo): `id` → `external_id`
  (lowercase), `description` → `title` (nullable), `createDate` (epoch) →
  `published_at`, `duration` (segundos float, **nullable** — hay posts de imágenes)
  → `duration_ms`, `tags` → `tags`, `urls.thumbnail` → `thumbnail_url`,
  `userName`/`views` conservados solo como contexto (no modelados en v1).
  `page_url` = `https://www.redgifs.com/watch/<external_id>` como **referencia
  canónica que nunca se fetchea** (robots.txt disallows `/watch/` y `/ifr/`;
  Decisión propuesta D5).
- `get_visual_assets()`: **thumbnail** y **poster** (imágenes jpg estáticas de
  `media.redgifs.com`, kind `thumbnail` — el contrato no tiene kind "poster");
  **sin storyboard** → sin timestamps (paridad FR-012: frame indexado sin timestamp,
  sin fallar). **MUST NOT** exponer ni descargar los mp4 (`sd`/`hd`/`silent`, que
  son el contenido completo del ítem; SC-006).
- `check_availability()`: `GET /v2/gifs/<id>` → `200` = available; `404` o error de
  API = `unavailable`/`removed` (terminal, sin reintentos infinitos). **Nunca** se
  accede a `/watch/` ni `/ifr/`.
- **Manifest de compliance** documentando: source, access method (`api`), assets
  accedidos (`thumbnail`), robots revisados, términos revisados, rate limit y review
  date. **Decisión propuesta D4**: OK del humano en modo prueba →
  `robots_reviewed=true`, `terms_reviewed=true`, `review_date="2026-08-19"`; la
  habilitación **efectiva** sigue exigiendo `sources.enabled=true` en BD (aprobación
  humana explícita final, gate existente del registry). El seed registra la fuente
  con `enabled=false`.
- **Fixtures sintéticos anonimizados** (`tests/fixtures/redgifs/`) derivados de la
  estructura JSON real observada (dominios `.invalid` en los fixtures, sin media
  real en el repo — SEC-004) + tests unitarios del adapter y de integración del
  flujo completo **sin red**, deterministas en CI (mismo patrón que xvideos/xhamster
  con `httpx.MockTransport` inyectable).
- **Backfill acotado real** (tras aprobación humana): límite de prueba
  **`--max-videos 50`** (Decisión propuesta D3) sobre `/niches/homemade` y
  `/niches/real-cellphone-clips` (Decisión propuesta D3), BACKFILL + INCREMENTAL,
  frames (1–2 por ítem, sin timestamp) e indexación reutilizando el pipeline del
  spike.
- Aislamiento: un fallo de redgifs no afecta a xvideos/xhamster/erome/mock;
  estadísticas por fuente ya cubiertas por el core (FR-014 de la spec 002) sin
  cambios.

## Fuera de alcance

- Cualquier otra fuente candidata (pornhub, etc.).
- Descarga o exposición de los **mp4 del ítem** (`urls.sd`/`urls.hd`/`urls.silent`):
  son el contenido completo del gif/clip (SC-006 de la spec 002). Si en el futuro el
  humano autoriza explícitamente el acceso a clips cortos, requiere **enmienda
  formal al contrato** (spec 002 · SC-006) con su propia revisión legal — nunca un
  cambio silencioso.
- Tags (`/tag/*`, pese a estar en el sitemap oficial), **búsqueda por texto**
  (`/v2/gifs/search`, cota observada de 10k resultados), **galerías/posts de
  imágenes** como modelo de colección (los posts-imagen individuales sí se procesan
  como vídeos sin duración), **perfiles/colecciones de usuario** (`/v2/users/*`,
  `/v2/gallery/*`) — fases futuras documentadas.
- Acceso a `www.redgifs.com` en general: la página web es un SPA sin SSR (HTML
  inservible para crawling) y las rutas `/watch/` y `/ifr/` están disallowed por
  robots.txt; el adapter v1 **solo** habla con `api.redgifs.com` (y descarga assets
  de `media.redgifs.com`).
- Saltarse auth, paywalls, CAPTCHA, DRM, anti-bot o cualquier protección (el token
  temporal es el mecanismo público documentado, no un bypass; Cloudflare protege la
  API).
- Modificar el core (pipeline/jobs/registry/CLI): si la implementación revelara una
  necesidad real de cambio, se convierte en enmienda explícita del contrato de la
  spec 002 con su propio PR trazado, nunca un cambio silencioso.
- Exposición pública y compliance de lanzamiento.

## Actores

- **Operador** (persona técnica): revisa legalmente la fuente (robots/ToS), habilita
  `sources.enabled`, lanza el backfill acotado e incremental y consulta stats.
- **Crawler** (proceso interno): obtiene el token temporal, descubre IDs de los
  nichos, obtiene metadata y visual assets, respetando manifest, rate limit y robots.
- **Worker de jobs** (proceso interno, existente): ejecuta jobs `FOR UPDATE SKIP
  LOCKED` con retries/backoff (sin cambios).
- **Sistema de indexación** (proceso interno, existente): convierte los visual
  assets en frames (sin timestamp) e indexa con el pipeline del spike (sin cambios).

## Historias de usuario

### User Story 1 - Adapter redgifs sobre el contrato existente vía API oficial, con fixtures y tests sin red (Priority: P1)

El operador quiere que redgifs.com se consuma a través del mismo contrato
`SourceAdapter` que xvideos/xhamster/erome, sin tocar el core, y que el adapter esté
listo y probado antes de cualquier acceso real.

**Why this priority**: Es la base de toda la fase; sin adapter probado con fixtures no
hay nada que habilitar legalmente después, y el mecanismo de acceso (API oficial con
token temporal, JSON en vez de HTML, sin storyboard) es lo suficientemente distinto
como para ejercitar de verdad el contrato.

**Independent Test**: Ejecutar el flujo completo (token → discover → get_video →
get_visual_assets → jobs → frames) con fixtures sintéticos de redgifs, sin red,
determinista en CI, y verificar que ningún módulo del core cambia.

**Acceptance Scenarios**:

1. **Given** fixtures sintéticos del listado `/v2/niches/<id>/gifs` (envelope con
   `gifs`, `page`, `pages`, `total`; ítems con `id`, `description`, `createDate`,
   `duration` nullable, `tags`, `urls.thumbnail`, `urls.poster`, `urls.sd/hd/silent`)
   y del endpoint `GET /v2/gifs/<id>`, **When** se ejecuta el flujo completo offline,
   **Then** se producen `VideoSource` normalizados (`external_id` estable lowercase,
   `title` nullable, `page_url` = `https://www.redgifs.com/watch/<id>`,
   `duration_ms` nullable, `thumbnail_url`, `published_at`) y jobs persistidos, sin
   que el core importe nada específico de redgifs.
2. **Given** un fixture que simula la API cambiada (JSON sin ítems ni señales de
   envelope), **When** el adapter lo parsea, **Then** falla con un error tipado y
   claro del adapter, contenido en la fuente (no corrompe el resto del flujo).
3. **Given** el manifest del adapter, **When** se consulta, **Then** documenta
   `source`, `access method` (`api`), `assets accessed` (`thumbnail`), `robots
   reviewed`, `terms reviewed`, `rate limit` y `review date`; con
   `robots_reviewed=false` o `terms_reviewed=false` el registry **rechaza** habilitar
   la fuente (`AdapterNotEnabledError`).
4. **Given** un token temporal caducado (401 en la API), **When** el adapter hace una
   petición, **Then** renueva el token automáticamente (rotación con backoff) y
   completa la petición; si la renovación falla de forma persistente, el error queda
   contenido en la fuente y el token **nunca** aparece en logs.

### User Story 2 - Backfill acotado real de los nichos de validación al índice visual (Priority: P1)

El operador quiere llenar el índice desde los nichos `homemade` y
`real-cellphone-clips` de redgifs, con un backfill de prueba acotado, y verificar
incremental sin duplicados — igual que con xvideos/xhamster.

**Why this priority**: Es la validación real del adapter (SEC-002 liberado por el
humano) y el primer contenido de redgifs en el índice.

**Independent Test**: Con la fuente habilitada y `--max-videos` acotado, ejecutar
BACKFILL sobre `--section /niches/homemade` y `/niches/real-cellphone-clips` y
verificar vídeos, frames (1–2 por ítem, sin timestamp) y embeddings consultables;
ejecutar INCREMENTAL y verificar que no se duplican vídeos ni frames.

**Acceptance Scenarios**:

1. **Given** la fuente `redgifs` habilitada (manifest revisado + `enabled=true`),
   **When** se ejecuta `backfill --source redgifs --section /niches/<id>
   --max-videos N`, **Then** se crean vídeos con unicidad `(source_id, external_id)`
   y jobs de metadata/visual assets, y el backfill se detiene al alcanzar la cota N.
2. **Given** vídeos ya descubiertos de esa fuente, **When** se ejecuta INCREMENTAL,
   **Then** solo se procesan IDs nuevos o cambiados; no se duplican vídeos ni frames.
3. **Given** los visual assets de un ítem (thumbnail + poster; nunca los mp4),
   **When** se procesan, **Then** se extraen frames indexados **sin timestamp**
   (paridad FR-012 del spike) reutilizando la idempotencia del pipeline.
4. **Given** un ítem retirado (404 en `GET /v2/gifs/<id>`), **When** se comprueba su
   disponibilidad, **Then** queda marcado `unavailable`/`removed` sin reintentos
   infinitos.

### User Story 3 - Aislamiento y observabilidad con tres fuentes reales (Priority: P2)

El operador quiere que un fallo de redgifs no tumbe el crawler ni afecte a las demás
fuentes, y ver el estado por fuente.

**Why this priority**: Con más fuentes reales, el aislamiento por fuente (FR-010 de
la spec 002) se ejercita de verdad.

**Independent Test**: Provocar errores persistentes en redgifs (fixtures con errores,
token inválido) y verificar que los jobs de xvideos/xhamster/erome/mock siguen
procesándose; consultar stats por fuente.

**Acceptance Scenarios**:

1. **Given** xvideos, xhamster, erome y redgifs registrados, **When** redgifs falla
   de forma persistente (401 sin renovación posible), **Then** los jobs de las demás
   fuentes continúan procesándose con normalidad.
2. **Given** jobs en curso o terminados de todas las fuentes, **When** se consultan
   las estadísticas, **Then** se obtienen conteos por estado y fuente sin cambios en
   el core.

### Edge Cases

- Ítem retirado (404 en `GET /v2/gifs/<id>`) → terminal `unavailable`/`removed`, sin
  reintentos infinitos (paridad spec 002).
- Token temporal caducado/revocado (401) → renovación automática con backoff; fallo
  persistente → error contenido en la fuente; **el token nunca se loguea**.
- 429 / rate limiting de la API / bloqueo temporal legítimo → backoff con jitter;
  **nunca** se intenta saltar la protección.
- **Posts de imágenes** (`duration=null`, `hasAudio=false`, `type=2`) → vídeo con
  `duration_ms=None`, solo thumbnail (y poster si existe), sin fallar.
- Ítem sin `description`, sin `tags` o sin `createDate` → campos opcionales nulos; el
  vídeo sigue procesándose (paridad spec 002).
- `duration` en segundos con decimales → `duration_ms = round(duration * 1000)`;
  valores nulos/ausentes → `None`.
- IDs con mezcla de mayúsculas/minúsculas → `external_id` normalizado a lowercase
  (la API responde en minúscula; `GET /v2/gifs/<id>` exige lowercase).
- Página repetida / 0 IDs nuevos / fin de páginas (`page >= pages`) → la cadena
  avanza con anti-bucle y sin truncación silenciosa.
- Página con `pages`/`total` ausentes (respuesta de `count=0`) → pararse con error
  tipado del adapter.
- Envelope con `gifs` ausente o vacío en mitad de la cadena → fin de discover sin
  error (paridad con el anti-bucle de xvideos).
- Token con `rate:-1` (sin límite explícito declarado) → el rate limit del manifest
  sigue siendo el conservador declarado en código (paridad FR-010).

## Requirements _(mandatory)_

### Functional Requirements

- **FR-001**: El sistema MUST implementar el adapter **`redgifs`** cumpliendo el
  contrato `SourceAdapter` de la spec 002 (`discover`, `get_video`,
  `get_visual_assets`, `check_availability` + `manifest`), **sin modificar el core**.
- **FR-002**: El método de acceso MUST ser **`api`** (API oficial pública
  `api.redgifs.com`; token temporal sin clave vía `GET /v2/auth/temporary`,
  documentado por RedGIFs en github.com/Redgifs/api — jerarquía FR-004 nivel 1,
  superior a sitemap/JSON/HTML). Prospección 2026-08-19: el HTML de las páginas es un
  SPA sin contenido SSR (inservible para crawling); la API es la vía canónica.
- **FR-003**: `discover()` MUST usar la sección `/niches/<id>` como entrada (nicho,
  p. ej. `/niches/homemade`) vía el mecanismo `section` del contrato (PR-049), con
  paginación por **`page`** (1-based; envelope con `page`/`pages`/`total`) y
  protección anti-bucle; **sin `section`, o con sección que no empiece por
  `/niches/`, el adapter MUST rechazar el discover** con error claro (fail-fast,
  Decisión propuesta D2: en v1 no se exploran tags ni búsqueda).
- **FR-004**: `get_video()` MUST normalizar a `VideoSource` desde el objeto gif de la
  API (`GET /v2/gifs/<id>` o el objeto del listado): `id` → `external_id` (lowercase,
  estable), `description` → `title` (nullable), `createDate` (epoch) →
  `published_at`, `duration` (segundos float, nullable) → `duration_ms` (redondeo),
  `tags` → `tags`, `urls.thumbnail` → `thumbnail_url`, y `page_url` =
  `https://www.redgifs.com/watch/<external_id>` como **referencia canónica nunca
  fetcheada** (robots disallow `/watch/`; Decisión propuesta D5). Campos opcionales
  nulos cuando falten.
- **FR-005**: `get_visual_assets()` MUST devolver, según la jerarquía FR-005 de la
  spec 002: (a) el **thumbnail** (`urls.thumbnail`) y (b) el **poster**
  (`urls.poster`), ambos como kind `thumbnail` (el contrato no tiene kind "poster"),
  sin timestamps (`timestamp_ms=None`, no hay storyboard en la fuente — paridad
  FR-012 del spike). **MUST NOT** exponer ni descargar los mp4 (`urls.sd`/`hd`/
  `silent`): son el contenido completo del ítem (SC-006).
- **FR-006**: El adapter MUST declarar `asset_hosts` (allowlist SEC-001) con los
  hosts de assets observados (`media.redgifs.com`, y los que la validación real
  confirme), nunca derivada de las URLs parseadas; allowlist de host de API:
  `api.redgifs.com`.
- **FR-007**: El adapter MUST declarar un **manifest de compliance**
  (`access_method="api"`, `assets_accessed=["thumbnail"]`, rate limit conservador
  con overrides por entorno `XTRACE_CRAWLER_RATE_REDGIFS_*`). **Decisión propuesta
  D4**: revisión legal humana OK en modo prueba → `robots_reviewed=true`,
  `terms_reviewed=true`, `review_date="2026-08-19"`; la habilitación efectiva sigue
  exigiendo `sources.enabled=true` en BD, y el seed registra la fuente con
  `enabled=false`.
- **FR-008**: El sistema MUST incluir **fixtures sintéticos anonimizados** de la
  estructura real de la API de redgifs (envelope de listado de nicho, objeto gif,
  respuesta 404; sin media real en el repo — SEC-004) y **tests** que ejecuten el
  flujo completo **sin red** y de forma determinista en CI.
- **FR-009**: El backfill real MUST estar **acotado** (**`--max-videos 50`**,
  Decisión propuesta D3) y restringido a los nichos de validación `/niches/homemade`
  y `/niches/real-cellphone-clips`; BACKFILL e INCREMENTAL reutilizan los
  jobs/pipeline existentes (idempotencia por `(source_id, external_id)`).
- **FR-010**: El adapter MUST respetar el rate limit declarado (defaults
  conservadores en código + override por entorno `XTRACE_CRAWLER_RATE_REDGIFS_*`,
  paridad Decisión D5 de la spec 002), y MUST NOT acceder a rutas desaconsejadas por
  robots.txt (`/watch/`, `/ifr/`) ni a `www.redgifs.com` en v1.

### Security Requirements

- **SEC-001**: El sistema MUST NOT saltarse auth, paywalls, CAPTCHA, DRM o anti-bot,
  ni acceder a contenido privado; solo recursos públicos legalmente accesibles
  (paridad spec 002). Prospección 2026-08-19: robots.txt de `www.redgifs.com`
  permite `/niches/` (y el resto salvo `/watch/` y `/ifr/`), declara sitemap en
  `api.redgifs.com`; `api.redgifs.com` no tiene robots.txt propio (404) y está
  detrás de Cloudflare/nginx con auth para algunos endpoints (el token temporal es
  el mecanismo público documentado).
- **SEC-002**: El adapter real **no podrá habilitarse** sin manifest de compliance
  revisado (legal/ToS/robots con `review date`) y **aprobación humana explícita**
  (`sources.enabled=true`). Reutiliza el gate existente del registry (sin cambios).
  La revisión legal del humano se daría en modo prueba (Decisión propuesta D4).
- **SEC-003**: Todo acceso HTTP del adapter pasa por `SafeHTTPClient` con allowlist
  de host de API (`api.redgifs.com`) y de assets (`media.redgifs.com`, FR-006), con
  validación anti-DNS-rebinding heredada.
- **SEC-004**: Los fixtures derivados de la estructura real se mantienen anonimizados
  (dominios `.invalid`) y sin media real commiteada; el JSON real capturado en
  prospección vive en `/tmp`, nunca en el repo.
- **SEC-005**: El **token temporal de API es un secreto efímero de sesión**: se
  obtiene bajo demanda con backoff, se renueva automáticamente ante `401`, y MUST NOT
  loguearse, persistirse en BD ni exponerse en fixtures/errores (rotación segura).

### Data Requirements

- **DATA-001**: El seed MUST registrar la fuente `redgifs` (name/adapter `redgifs`,
  manifest de compliance con `access_method="api"`, `enabled=false`) sin migración
  nueva (el esquema de la spec 002 ya cubre `sources`).
- **DATA-002**: Los vídeos de redgifs conviven con los de xvideos/xhamster/erome y el
  dataset local sin colisiones (unicidad `(source_id, external_id)`, ya existente).
- **DATA-003**: Los frames derivados de thumbnail/poster usan `source_kind`
  `thumbnail` ya contemplado; los jobs reutilizan los tipos existentes
  (DISCOVER/FETCH_METADATA/INDEX_VIDEO/CHECK_AVAILABILITY) sin tipos nuevos.

### Non-Functional Requirements

- **NFR-001**: Desarrollo y tests con coste ~0 € (Supabase local, fixtures, sin red).
- **NFR-002**: Un fallo de redgifs no degrada el throughput de las demás fuentes
  (aislamiento, heredado).
- **NFR-003**: El flujo completo con fixtures se ejecuta **sin red** y de forma
  determinista en CI.
- **NFR-004**: El adapter respeta en todo momento los límites declarados (rate limit,
  robots) — medible en logs y tests (paridad SC-005 de la spec 002).

## Key Entities

- **Source** `redgifs`: manifest (`access_method="api"`, `assets_accessed=
  ["thumbnail"]`, robots/terms revisados según decisión humana — propuesta 2026-08-19
  en modo prueba, Decisión D4), `enabled=false` en el seed (habilitación efectiva =
  acción humana en BD).
- **Video** (ampliación del spike, ya existente): filas de redgifs con `source_id`/
  `external_id` (id del gif, lowercase), `page_url` = `https://www.redgifs.com/watch/
  <id>` (referencia, nunca accedida), `title` (description), `duration_ms` (nullable:
  posts de imágenes), `tags`, `published_at`, `thumbnail_url`.
- **VisualAsset**: thumbnail y poster (imágenes jpg de `media.redgifs.com`, kind
  `thumbnail`); mp4 (`sd`/`hd`/`silent`) observados pero **prohibidos en v1** (son el
  contenido completo del ítem, SC-006).
- **Frame** (reutilizado): frames **sin timestamp** (`timestamp_ms=None`, no hay
  storyboard), `pHash`/`embedding` del pipeline existente.

## Success Criteria _(mandatory)_

### Measurable Outcomes

- **SC-001**: El flujo completo (token → discover → get_video → get_visual_assets →
  jobs) se ejecuta con fixtures sintéticos de redgifs **sin red**, en CI, de forma
  determinista.
- **SC-002**: Un **backfill acotado real** (**`--max-videos 50`**, Decisión propuesta
  D3) de `/niches/homemade` y `/niches/real-cellphone-clips` produce vídeos, frames
  y embeddings **consultables** en el índice del spike.
- **SC-003**: Una segunda ejecución **INCREMENTAL** sobre la misma fuente **no
  duplica** vídeos ni frames.
- **SC-004**: **0 descargas de mp4**; solo assets permitidos (thumbnail + poster).
- **SC-005**: El rate limit declarado se **respeta** (0 violaciones medibles en
  logs/tests).
- **SC-006**: Añadir redgifs **no requiere modificar el core**: solo se añaden
  ficheros del adapter, su registro, seed y fixtures (objetivo de producto, SC-007 de
  la spec 002).
- **SC-007**: Un fallo persistente de redgifs (fixtures, token inválido) **no
  impide** que los jobs de otras fuentes se procesen.

## Assumptions

- **Estructura real observada en prospección (2026-08-19, recursos públicos, sin
  bypass)**: robots.txt de `www.redgifs.com` con `Disallow: /watch/` y `/ifr/` y
  `Sitemap: https://api.redgifs.com/sitemap.xml` (sitemap de `/tag/*`); las páginas
  `/niches/*` están permitidas. API oficial: `GET /v2/auth/temporary` devuelve JWT
  (`scope=read`, `rate:-1`, validez ≈ 24 h; `rtfm` → github.com/Redgifs/api); `GET
  /v2/niches` (+`?page`, `?query`) → 1795 nichos en 60 páginas; `GET
  /v2/niches/{id}/gifs?order=new|trending|top&count=100&page=N` → envelope
  `{gifs, page, pages, total, ...}` con paginación **por `page`** (verificado: el
  campo `cursor` del envelope no pagina este endpoint; `count=100` devuelve 100
  ítems y `count=200` devuelve 0 → el máximo aceptado es 100; `homemade` 66084 gifs,
  `real-cellphone-clips` 3669 gifs); `GET /v2/gifs/{id}` → `{"gif": {...}}` (wrapper
  con `user`/`niches` extra que se ignoran; 404 con `{"error":{"code":
  "GifNotFound"}}` → ítem retirado; el id se normaliza a lowercase); `GET
  /v2/gifs/search` (cota observada 10k). Objeto gif: `id`, `description`,
  `createDate`, `duration` (s, **nullable**), `tags`, `niches`, `sexuality`,
  `contentType`, `userName`, `views`, `likes`, `hasAudio`, `type` (1 = vídeo/gif, 2 =
  imagen con `duration=null`/`hasAudio=false`/`hls=false`, HTTP 200 normal),
  `urls.{thumbnail, poster, sd, hd, silent, html}`; los thumbnails/posters viven en
  `media.redgifs.com`. La página web es un SPA sin contenido SSR (8 KB de shell, sin
  og: ni ítems) → HTML no usable. **Todo esto se re-valida con fixtures versionados
  durante la implementación** (mismo patrón que los PR-043…PR-053 de xvideos y la
  feature 007).
- El `external_id` es el **id del gif en lowercase** (estable y derivable del objeto
  de la API), no la URL.
- El `page_url` es `https://www.redgifs.com/watch/<id>` como **referencia canónica
  que el crawler nunca fetchea** (robots respetado; la disponibilidad se comprueba
  por API) — Decisión propuesta D5.
- Orden del feed del discover: **`new`** (contenido más reciente primero, el más
  estable para INCREMENTAL); `trending`/`top` disponibles pero no usados en v1
  (override por entorno en el plan técnico si la validación lo exige).
- Tamaño de página del listado: `count=100` (máximo aceptado verificado; `count=200`
  devuelve respuesta vacía), con paginación por `page` y parada en `page >= pages`
  o 0 IDs nuevos (anti-bucle).
- La revisión legal/ToS/robots es responsabilidad **del humano**; sin ella el adapter
  permanece deshabilitado y el desarrollo usa solo fixtures. La revisión de
  robots.txt está hecha (hechos de la prospección); la revisión de **términos de
  servicio** de RedGIFs queda para el humano (Decisión propuesta D4: OK en modo
  prueba).
- El crawler reutiliza pipeline, jobs, repositorios, registry y CLI de la spec 002
  sin cambios; cualquier cambio necesario en el core es una enmienda explícita aparte.
- El contenido audiovisual real capturado en validaciones **no se commitea**
  (SEC-004).
- Limitación documentada: cada ítem de redgifs aporta **1–2 frames sin timestamp**
  al índice (frente a los storyboards de xvideos/xhamster); la calidad de búsqueda
  del corpus redgifs depende de esa densidad, medible con las stats existentes.

## Dependencies

- `specs/000-platform-foundation` — esqueleto técnico, `IMPLEMENTED`.
- `specs/001-visual-search-spike` — pipeline frames/pHash/embedding/ANN, `IMPLEMENTED`.
- `specs/002-source-sdk-crawler` — contrato `SourceAdapter`, registry, jobs, pipeline y
  primer adapter real (xvideos), `IMPLEMENTED`.
- `specs/007-xhamster-adapter` — patrón de adapter + fixtures + gate SEC-002 (en
  implementación; su PR-062/PR-063 sirven de referencia de estructura).
- Revisión legal/ToS/robots del humano para redgifs (puerta SEC-002).
- Supabase local operativa (Docker) para la tabla `jobs`/`sources`.

## Risks

- **Cambios de la API de redgifs** (JSON, endpoints, requisitos de auth) → adapters
  aislados + fixtures versionados que detectan la regresión.
- **Rate limits / 429 / Cloudflare** → backoff con jitter, respeto estricto a límites
  y robots, aislamiento por fuente; nunca se intenta saltar protecciones.
- **Token temporal efímero** (caducidad ≈ 24 h, posible revocación) → renovación
  automática ante 401 con backoff; el token nunca se persiste ni se loguea.
- **Riesgo legal** → habilitación bloqueada hasta revisión humana; el desarrollo
  avanza con fixtures sin depender de ella.
- **Densidad visual baja por ítem** (1–2 frames sin timestamp) → limitación
  documentada y medible (SC-002/SC-003); si la validación real demostrara calidad
  insuficiente, la ampliación (p. ej. autorizar clips cortos) es una enmienda formal
  al contrato, nunca un cambio silencioso.
- **Alcance del backfill real** → acotado a N vídeos de los nichos definidos por el
  operador.

## Open Questions

_Las siguientes decisiones se proponen con una recomendación; el humano responsable
las confirma o corrige en la ronda de clarificación (una sola respuesta basta)._

> **Resueltas 2026-08-19**: la aprobación humana de la spec se dio sin correcciones a
> Q1–Q5, por lo que las recomendaciones quedan adoptadas como **decisiones D1–D5**
> (ver `## Historial de decisiones`). El humano puede revisarlas en cualquier momento
> antes de la implementación.

- **Q1 (assets v1)**: RedGIFs no tiene storyboard: por ítem solo expone **thumbnail +
  poster** (2 imágenes, sin timestamps). Los mp4 cortos (`sd`/`hd`/`silent`) son el
  contenido completo del ítem, prohibidos por SC-006. **Recomendación**: v1 con solo
  imágenes (poster + thumbnail, `timestamp_ms=None`), sin enmienda al contrato.
  Alternativa: autorizar la descarga de clips mp4 cortos → enmienda explícita a
  SC-006 con revisión legal más profunda.
- **Q2 (alcance del discover)**: **Recomendación**: solo por sección
  `--section /niches/<id>` con fail-fast sin sección (paridad D2 de la 007); tags y
  búsqueda por texto fuera de v1. Alternativa: admitir también el endpoint de
  búsqueda (`/v2/gifs/search`, cota 10k).
- **Q3 (validación real)**: **Recomendación**: backfill `--max-videos 50` sobre los
  **dos** nichos aportados (`/niches/homemade` y `/niches/real-cellphone-clips`).
  Alternativa: solo `homemade`, u otra cota.
- **Q4 (puerta legal SEC-002)**: **Recomendación**: OK del humano **en modo prueba**
  (manifest con `robots_reviewed=true`, `terms_reviewed=true`,
  `review_date="2026-08-19"`, paridad D5 de la 007); la habilitación efectiva sigue
  exigiendo `sources.enabled=true` en BD.
- **Q5 (page_url)**: **Recomendación**: `https://www.redgifs.com/watch/<id>` como
  referencia canónica (guardada, nunca accedida; robots respetado). Alternativas:
  URL `/ifr/<id>` (embed) o `page_url=None`.

## Approval

**Estado**: `APPROVED` — aprobada por el humano responsable el 2026-08-19 con la
instrucción explícita *"quiero que se apruebe pero que no hagas nada todavía"* (sin
correcciones a Q1–Q5: las recomendaciones quedan adoptadas como decisiones D1–D5,
revisables por el humano en cualquier momento antes de la implementación).
**Reanudación (2026-08-20)**: el humano da la instrucción explícita de empezar con
redgifs → se habilitan `technical-planning` (`plan.md` + ADR-0016) y
`task-planning` (`tasks.md`, PR-066…PR-069), sin alterar Q1–Q5/D1–D5.

## Historial de decisiones

- **2026-08-19 · Borrador inicial**: spec creada por el agente (DeepSeek) tras
  preflight y prospección factual de recursos públicos de redgifs (robots.txt,
  sitemap, API oficial con token temporal, listados de los nichos `homemade` y
  `real-cellphone-clips`, objeto gif, endpoints de metadata/búsqueda; sin media
  descargada — SEC-004, el JSON real vive en `/tmp`). Decisiones propuestas Q1–Q5
  listadas en `## Open Questions`.
- **2026-08-19 · Verificación técnica (2ª pasada)**: confirmado que la paginación de
  `/v2/niches/{id}/gifs` es por **`page`** (1-based, envelope con `page`/`pages`/
  `total`; el campo `cursor` del envelope no pagina este endpoint), `count` máximo
  aceptado = **100** (`count=200` → respuesta vacía), `GET /v2/gifs/{id}` envuelve en
  `{"gif": {...}}` (con `user`/`niches` extra ignorados) y responde **HTTP 404**
  `GifNotFound` para ítems retirados, y los **posts de imágenes** (`type=2`,
  `duration=null`, `hasAudio=false`, `hls=false`) son respuestas HTTP 200 normales
  que el adapter procesa con `duration_ms=None`. Spec actualizada en consecuencia
  (FR-003, assumptions, edge cases).
- **2026-08-19 · D1 (Q1)**: **Assets v1 = solo imágenes** (thumbnail + poster,
  `timestamp_ms=None`); los mp4 (`sd`/`hd`/`silent`) no se exponen ni descargan (son
  el contenido completo del ítem, SC-006); cualquier acceso futuro a clips cortos
  exige enmienda formal al contrato. *Recomendación adoptada en la aprobación.*
- **2026-08-19 · D2 (Q2)**: **Discover solo por sección** `/niches/<id>` vía
  `--section`; sin sección (o sección que no empiece por `/niches/`) el adapter
  rechaza el discover (fail-fast). Tags y búsqueda por texto fuera de v1.
  *Recomendación adoptada en la aprobación.*
- **2026-08-19 · D3 (Q3)**: **Backfill de validación real con `--max-videos 50`**
  sobre los dos nichos aportados (`/niches/homemade` y
  `/niches/real-cellphone-clips`). *Recomendación adoptada en la aprobación.*
- **2026-08-19 · D4 (Q4)**: **Revisión legal/ToS/robots de redgifs OK en modo
  prueba** (puerta SEC-002 liberada por el humano) → manifest con
  `robots_reviewed=true`, `terms_reviewed=true`, `review_date="2026-08-19"`. La
  habilitación efectiva sigue exigiendo `sources.enabled=true` en BD (aprobación
  humana final). *Recomendación adoptada en la aprobación.*
- **2026-08-19 · D5 (Q5)**: **`page_url` = `https://www.redgifs.com/watch/<external_id>`**
  como referencia canónica guardada pero **nunca accedida** por el crawler (robots
  disallow `/watch/`; disponibilidad solo por API). *Recomendación adoptada en la
  aprobación.*
- **2026-08-19 · Aprobación**: spec **`APPROVED`** por el humano responsable con la
  instrucción explícita *"quiero que se apruebe pero que no hagas nada todavía"*
  (sin correcciones a Q1–Q5 → D1–D5 adoptadas según recomendación). **La
  implementación queda PAUSADA por orden del humano** hasta nueva instrucción.
