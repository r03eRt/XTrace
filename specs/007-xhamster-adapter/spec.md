# Feature Specification: Adapter xhamster.com (segunda fuente real)

**Feature Branch**: `feature/007-xhamster-adapter`

**Created**: 2026-08-19

**Status**: APPROVED

**Input**: User description: "quiero que leas el proyecto y comiences el adapter de
https://es.xhamster.com/categories/amateur"

> Origen de requisitos: `docs/PRODUCT_IDEA.md` (fuente candidata nº 3: `xhamster.com`,
> "Probable storyboard/sprite + thumbnails") y la spec 002 (SDK + primer crawler) — donde
> "más de una fuente real" quedó explícitamente **fuera de alcance**: cada fuente nueva es
> un adapter posterior con su propia revisión legal/ToS/robots. El contrato `SourceAdapter`
> ya está implementado y probado con `xvideos.com`.
>
> Esta spec describe **qué** y **por qué**. Las decisiones técnicas concretas (selectores,
> slicing del sprite, layout del adapter) se fijan en `technical-planning` tras la
> **aprobación humana**.

## Objetivo

Añadir la **segunda fuente real** al SDK de crawling de XTrace — **`xhamster.com`**,
empezando por su categoría `amateur` (`https://es.xhamster.com/categories/amateur`) — sin
tocar el core de indexación/búsqueda:

1. Implementar el adapter real `xhamster` que cumple el contrato `SourceAdapter`
   (spec 002 · FR-001): `discover()`, `get_video()`, `get_visual_assets()`,
   `check_availability()` + **manifest de compliance**.
2. Aprovechar los visual assets que la fuente expone de forma pública: **storyboard/sprite
   webp** (`data-sprite`) + **thumbnail** (`og:image`); según la jerarquía FR-005
   (storyboard/sprite → thumbnails → preview → vídeo), **nunca** el vídeo completo.
3. Registrar la fuente en el SDK (registry + seed en BD) **deshabilitada** hasta la
   revisión legal/ToS/robots del humano (gate SEC-002), con fixtures sintéticos para
   desarrollar y testear todo el flujo **sin red**.
4. Tras la aprobación humana, ejecutar un **backfill acotado real** de la categoría
   `amateur` que alimente el índice visual del spike con la misma calidad validada, y
   demostrar que **añadir una fuente no toca el core** (SC-007 de la spec 002, objetivo de
   producto).

El éxito de la fase es demostrar que el SDK soporta una segunda fuente con una estructura
web radicalmente distinta a xvideos (slugs `/videos/<slug>-<id>`, sprite webp, sin
JSON-LD), sin modificar el core.

## Alcance

- Adapter real **`xhamster`** (HTML — método de acceso según jerarquía FR-004) sobre el
  contrato existente `SourceAdapter` (spec 002), reutilizando `SafeHTTPClient`,
  rate limiter, pipeline, jobs y repositorios **sin cambios en el core**.
- **Discover por categoría**: entrada inicial `/categories/amateur` (host canónico
  **`xhamster.com`**, Decisión D1) vía el mecanismo `section` ya existente en el contrato
  (PR-049 de la spec 002: `discover(section=...)` + `--section <path>` del CLI), con
  paginación y protección anti-bucle. **Sin `section`, el adapter rechaza el discover**
  (fail-fast, Decisión D2): en v1 xhamster solo se explora por sección.
- `get_video()`: metadata normalizada desde la página de vídeo (`og:*` +
  `window.initials.videoModel`: title, duration, tags, fecha).
- `get_visual_assets()`: **sprite/storyboard webp** (con sus tiles/timestamps) y
  **thumbnail**; los previews mp4 (`data-previewvideo`) observados **no se exponen en v1**
  (Decisión D3: quedan documentados como `preview_url=None`, SC-004).
- **Manifest de compliance** documentando: source, access method (`html`), assets
  accesidos (`storyboard`, `thumbnail`), robots revisados, términos revisados, rate limit
  y review date. **Decisión D5 (clarificación 2026-08-19)**: el humano da su OK a la
  revisión legal/ToS/robots **en modo prueba** → el manifest queda revisado
  (`robots_reviewed=true`, `terms_reviewed=true`, `review_date="2026-08-19"`); la
  habilitación **efectiva** sigue exigiendo `sources.enabled=true` en BD (aprobación
  humana explícita final, gate existente del registry). El seed registra la fuente con
  `enabled=false`.
- **Fixtures sintéticos anonimizados** (`tests/fixtures/xhamster/`) derivados de la
  estructura real observada (sin media real en el repo — SEC-004) + tests unitarios del
  adapter y de integración del flujo completo **sin red**, deterministas en CI.
- **Backfill acotado real** (tras aprobación humana): límite de prueba
  **`--max-videos 50`** (Decisión D4) sobre `/categories/amateur`, BACKFILL +
  INCREMENTAL, frames con timestamp desde el sprite e indexación reutilizando el pipeline
  del spike.
- Aislamiento: un fallo de xhamster no afecta a xvideos ni al mock; estadísticas por
  fuente ya cubiertas por el core (FR-014 de la spec 002) sin cambios.

## Fuera de alcance

- Cualquier otra fuente candidata (redgifs, pornhub, erome).
- Crawlear xhamster **fuera de la categoría de validación** en backfills reales (el adapter
  puede aceptar otras secciones por diseño del contrato, pero la validación real se acota
  a `/categories/amateur`).
- Secciones de xhamster desaconsejadas por robots.txt (filtros `*/categories/*/best/*`,
  `most-viewed`, etc.) — no se usan.
- Descarga de **vídeos completos** (prohibido, SC-006 de la spec 002) ni acceso a
  contenido premium/privado (robots.txt: `/premium/` disallowed).
- Saltarse auth, paywalls, CAPTCHA, DRM, anti-bot o cualquier protección.
- Modificar el core (pipeline/jobs/registry/CLI): si la implementación revelara una
  necesidad real de cambio, se convierte en enmienda explícita del contrato de la spec 002
  con su propio PR trazado, nunca un cambio silencioso.
- Exposición pública y compliance de lanzamiento.

## Actores

- **Operador** (persona técnica): revisa legalmente la fuente (robots/ToS), habilita
  `sources.enabled`, lanza el backfill acotado e incremental y consulta stats.
- **Crawler** (proceso interno): descubre IDs de la categoría, obtiene metadata y visual
  assets, respetando manifest, rate limit y robots.
- **Worker de jobs** (proceso interno, existente): ejecuta jobs `FOR UPDATE SKIP LOCKED`
  con retries/backoff (sin cambios).
- **Sistema de indexación** (proceso interno, existente): convierte los visual assets en
  frames con timestamp e indexa con el pipeline del spike (sin cambios).

## Historias de usuario

### User Story 1 - Adapter xhamster sobre el contrato existente, con fixtures y tests sin red (Priority: P1)

El operador quiere que xhamster.com se consuma a través del mismo contrato `SourceAdapter`
que xvideos, sin tocar el core, y que el adapter esté listo y probado antes de cualquier
acceso real.

**Why this priority**: Es la base de toda la fase; sin adapter probado con fixtures no hay
nada que habilitar legalmente después, y la estructura de xhamster (slugs, sprite webp,
`window.initials`) es lo suficientemente distinta a xvideos como para ejercitar de verdad
el contrato.

**Independent Test**: Ejecutar el flujo completo (discover → get_video → get_visual_assets
→ jobs → frames) con fixtures sintéticos de xhamster, sin red, determinista en CI, y
verificar que ningún módulo del core cambia.

**Acceptance Scenarios**:

1. **Given** fixtures sintéticos del listado `/categories/amateur` (ítems con
   `data-video-id`, enlaces `/videos/<slug>-<id>`, paginación numérica) y de una página de
   vídeo (`og:*` + `window.initials.videoModel` + `data-sprite`), **When** se ejecuta el
   flujo completo offline, **Then** se producen `VideoSource` normalizados
   (`external_id` estable, `title`, `page_url`, `duration_ms`, `thumbnail_url`,
   `storyboard_urls`, `tags`, `published_at` cuando exista) y jobs persistidos, sin que el
   core importe nada específico de xhamster.
2. **Given** un fixture que simula HTML cambiado (sin ítems de vídeo ni señales de
   página de vídeo), **When** el adapter lo parsea, **Then** falla con un error tipado y
   claro del adapter, contenido en la fuente (no corrompe el resto del flujo).
3. **Given** el manifest del adapter, **When** se consulta, **Then** documenta `source`,
   `access method`, `assets accessed`, `robots reviewed`, `terms reviewed`, `rate limit` y
   `review date`; con `robots_reviewed=false` o `terms_reviewed=false` el registry
   **rechaza** habilitar la fuente (`AdapterNotEnabledError`).

### User Story 2 - Backfill acotado real de la categoría amateur al índice visual (Priority: P1)

El operador quiere llenar el índice desde la categoría `amateur` de xhamster, con un
backfill de prueba acotado, y verificar incremental sin duplicados — igual que se hizo con
xvideos.

**Why this priority**: Es la validación real del adapter (SEC-002 liberado por el humano)
y el primer contenido de xhamster en el índice.

**Independent Test**: Con la fuente habilitada y `--max-videos` acotado, ejecutar
BACKFILL sobre `--section /categories/amateur` y verificar vídeos, frames con timestamp
(desde el sprite) y embeddings consultables; ejecutar INCREMENTAL y verificar que no se
duplican vídeos ni frames.

**Acceptance Scenarios**:

1. **Given** la fuente `xhamster` habilitada (manifest revisado + `enabled=true`), **When**
   se ejecuta `backfill --source xhamster --section /categories/amateur --max-videos N`,
   **Then** se crean vídeos con unicidad `(source_id, external_id)` y jobs de
   metadata/visual assets, y el backfill se detiene al alcanzar la cota N.
2. **Given** vídeos ya descubiertos de esa fuente, **When** se ejecuta INCREMENTAL,
   **Then** solo se procesan IDs nuevos o cambiados; no se duplican vídeos ni frames.
3. **Given** los visual assets de un vídeo (sprite + thumbnail; nunca el vídeo completo),
   **When** se procesan, **Then** se extraen frames con timestamp derivado del sprite e
   indexan con el pipeline del spike, reutilizando su idempotencia.
4. **Given** un vídeo retirado (404), **When** se comprueba su disponibilidad, **Then**
   queda marcado `unavailable`/`removed` sin reintentos infinitos.

### User Story 3 - Aislamiento y observabilidad con dos fuentes reales (Priority: P2)

El operador quiere que un fallo de xhamster no tumbe el crawler ni afecte a xvideos, y ver
el estado por fuente.

**Why this priority**: Con dos fuentes reales, el aislamiento por fuente (FR-010 de la
spec 002) se ejercita de verdad.

**Independent Test**: Provocar errores persistentes en xhamster (fixtures con errores) y
verificar que los jobs de xvideos/mock siguen procesándose; consultar stats por fuente.

**Acceptance Scenarios**:

1. **Given** xvideos y xhamster registrados, **When** xhamster falla de forma persistente,
   **Then** los jobs de xvideos continúan procesándose con normalidad.
2. **Given** jobs en curso o terminados de ambas fuentes, **When** se consultan las
   estadísticas, **Then** se obtienen conteos por estado y fuente sin cambios en el core.

### Edge Cases

- Página de vídeo eliminada (404/`removed`) → terminal `unavailable`/`removed`, sin
  reintentos infinitos (paridad con spec 002).
- HTML de xhamster cambia sin aviso → el adapter falla de forma aislada; fixtures
  versionados detectan la regresión; el vídeo queda `failed` con error legible.
- 429 / bloqueo temporal / anti-bot legítimo → backoff con jitter; **nunca** se intenta
  saltar la protección.
- Vídeo sin sprite (p. ej. `data-sprite` ausente) → degradación a thumbnail único según la
  jerarquía FR-005, sin fallar todo el vídeo.
- IDs de vídeo con dos formas observadas (`/videos/<slug>-<numérico>` y
  `/videos/<slug>-<alfanumérico>`) → el `external_id` debe ser estable y derivable de la
  URL canónica para ambas.
- Paginación de categoría con saltos (páginas `2..6` y luego páginas con numeración alta,
  p. ej. `/16828`, `/33654`) → la cadena avanza con anti-bucle (cursor repetido / 0 IDs
  nuevos → fin) y sin truncación silenciosa.
- VideoSource sin `published_at`, sin tags o sin duración → campos opcionales nulos; el
  vídeo sigue procesándose (paridad con spec 002).
- Frame sin timestamp fiable (sprite sin duración) → frame indexado sin timestamp, sin
  fallar (paridad FR-012 del spike).
- Contenido retirado / takedown → registro + exclusión del índice (mecanismo existente).

## Requirements _(mandatory)_

### Functional Requirements

- **FR-001**: El sistema MUST implementar el adapter **`xhamster`** cumpliendo el contrato
  `SourceAdapter` de la spec 002 (`discover`, `get_video`, `get_visual_assets`,
  `check_availability` + `manifest`), **sin modificar el core**.
- **FR-002**: El método de acceso MUST ser **`html`**, documentado en el manifest según la
  jerarquía FR-004 (API/feed oficial → sitemap → JSON → HTML → navegador): no hay API/feed
  oficial ni sitemap accesible (prospección 2026-08-19: `sitemap.xml` → 404, robots.txt
  sin directivas `Sitemap:`).
- **FR-003**: `discover()` MUST usar la sección `/categories/amateur` como URL inicial
  (host canónico **`xhamster.com`**, Decisión D1) vía el mecanismo `section` del contrato
  (PR-049), con paginación por cursor y protección anti-bucle; **sin `section` el adapter
  MUST rechazar el discover** con error claro (fail-fast, Decisión D2: en v1 no se explora
  la home).
- **FR-004**: `get_video()` MUST normalizar a `VideoSource` desde la página de vídeo:
  `og:title`/`og:url`/`og:image` + `window.initials.videoModel` (`id`, `duration`,
  `title`, `created`, `tags`/`keywords`), con `external_id` estable derivable de la URL
  canónica `/videos/<slug>-<id>` (formas numérica y alfanumérica), `page_url` completo y
  campos opcionales nulos cuando falten.
- **FR-005**: `get_visual_assets()` MUST devolver, según la jerarquía FR-005 de la spec
  002: (a) el **storyboard/sprite** del vídeo principal (desde
  `window.initials.spriteLoader.template`, con `position`/`timestamp_ms` derivados de
  la duración cuando sea fiable) y (b) el **thumbnail** (`og:image`);
  degradación a thumbnail único si no hay sprite. **MUST NOT** exponer ni descargar el
  vídeo completo, y **MUST NOT** exponer en v1 los previews mp4 observados
  (`preview_url=None`, Decisión D3).
- **FR-006**: El adapter MUST declarar `asset_hosts` (allowlist SEC-001) con los hosts de
  assets observados (`thumb-v*.xhcdn.com`, `ic-vt-nss.xhcdn.com`, y los que la validación
  real confirme), nunca derivada de las URLs parseadas.
- **FR-007**: El adapter MUST declarar un **manifest de compliance**. **Decisión D5**:
  revisión legal humana OK en modo prueba → `robots_reviewed=true`, `terms_reviewed=true`,
  `review_date="2026-08-19"`; la habilitación efectiva sigue exigiendo
  `sources.enabled=true` en BD (aprobación humana explícita final), y el seed registra la
  fuente con `enabled=false`.
- **FR-008**: El sistema MUST incluir **fixtures sintéticos anonimizados** de la
  estructura real de xhamster (listado de categoría y página de vídeo, sin media real en
  el repo — SEC-004) y **tests** que ejecuten el flujo completo **sin red** y de forma
  determinista en CI.
- **FR-009**: El backfill real MUST estar **acotado** (**`--max-videos 50`**, Decisión
  D4, obligatorio) y restringido a la sección de validación `/categories/amateur`;
  BACKFILL e INCREMENTAL reutilizan los jobs/pipeline existentes (idempotencia por
  `(source_id, external_id)`).
- **FR-010**: El adapter MUST respetar el rate limit declarado (defaults conservadores en
  código + override por entorno `XTRACE_CRAWLER_RATE_XHAMSTER_*`, Decisión D5 de la spec
  002), y MUST NOT acceder a rutas desaconsejadas por robots.txt (filtros best/daily/…,
  `/premium/`, páginas privadas).

### Security Requirements

- **SEC-001**: El sistema MUST NOT saltarse auth, paywalls, CAPTCHA, DRM o anti-bot, ni
  acceder a contenido privado; solo recursos públicos legalmente accesibles (paridad spec
  002). Prospección 2026-08-19: robots.txt permite `/categories/` (salvo filtros) y
  `/videos/`; disallow `/premium/` y filtros `*/categories/*/best/*`, `most-viewed`, etc.
- **SEC-002**: El adapter real **no podrá habilitarse** sin manifest de compliance
  revisado (legal/ToS/robots con `review date`) y **aprobación humana explícita**
  (`sources.enabled=true`). Reutiliza el gate existente del registry (sin cambios). La
  revisión legal del humano se dio en clarificación **en modo prueba** (Decisión D5).
- **SEC-003**: Todo acceso HTTP del adapter pasa por `SafeHTTPClient` con allowlist de
  hosts de página (`xhamster.com`, `www.xhamster.com`, `es.xhamster.com` — Decisión D1
  + corrección del análisis post-tasks A1: con IP española el `og:url` canónico puede
  servirse en `es.*`; se acepta como objetivo de redirect/URL canónica, no como base)
  y de assets (FR-006), con validación anti-DNS-rebinding heredada.
- **SEC-004**: Los fixtures derivados de la estructura real se mantienen anonimizados y
  sin media real commiteada; el HTML real capturado en prospección vive en `/tmp`, nunca
  en el repo.

### Data Requirements

- **DATA-001**: El seed MUST registrar la fuente `xhamster` (name/adapter `xhamster`,
  manifest de compliance, `enabled=false`) sin migración nueva (el esquema de la spec 002
  ya cubre `sources`).
- **DATA-002**: Los vídeos de xhamster conviven con los de xvideos y el dataset local sin
  colisiones (unicidad `(source_id, external_id)`, ya existente).
- **DATA-003**: Los frames derivados del sprite usan `source_kind` storyboard/thumbnail ya
  contemplado; los jobs reutilizan los tipos existentes (DISCOVER/FETCH_METADATA/
  INDEX_VIDEO/CHECK_AVAILABILITY) sin tipos nuevos.

### Non-Functional Requirements

- **NFR-001**: Desarrollo y tests con coste ~0 € (Supabase local, fixtures, sin red).
- **NFR-002**: Un fallo de xhamster no degrada el throughput de xvideos/mock
  (aislamiento, heredado).
- **NFR-003**: El flujo completo con fixtures se ejecuta **sin red** y de forma
  determinista en CI.
- **NFR-004**: El adapter respeta en todo momento los límites declarados (rate limit,
  robots) — medible en logs y tests (paridad SC-005 de la spec 002).

## Key Entities

- **Source** `xhamster`: manifest (access_method=`html`, assets_accessed=
  `["storyboard", "thumbnail"]`, robots/terms revisados 2026-08-19 en modo prueba,
  Decisión D5), `enabled=false` en el seed (habilitación efectiva = acción humana en BD).
- **Video** (ampliación del spike, ya existente): filas de xhamster con
  `source_id`/`external_id` (URL canónica `/videos/<slug>-<id>`), `page_url`, `title`,
  `duration_ms`, `tags`, `published_at`, `thumbnail_url`, `storyboard_urls`.
- **VisualAsset**: sprite storyboard (webp, con `position` por tile) y thumbnail;
  preview mp4 observado pero **no expuesto en v1** (Decisión D3).
- **Frame** (reutilizado): frames con `timestamp_ms` derivado de la posición del tile del
  sprite sobre la duración (clamp `[0, duration_ms)`), `pHash`/`embedding` del pipeline
  existente.

## Success Criteria _(mandatory)_

### Measurable Outcomes

- **SC-001**: El flujo completo (discover → get_video → get_visual_assets → jobs) se
  ejecuta con fixtures sintéticos de xhamster **sin red**, en CI, de forma determinista.
- **SC-002**: Un **backfill acotado real** (**`--max-videos 50`**, Decisión D4) de
  `/categories/amateur` produce vídeos, frames con timestamp (desde el sprite) y
  embeddings **consultables** en el índice del spike.
- **SC-003**: Una segunda ejecución **INCREMENTAL** sobre la misma fuente **no duplica**
  vídeos ni frames.
- **SC-004**: **0 descargas de vídeo completo**; solo assets permitidos (sprite +
  thumbnail; previews mp4 no expuestos en v1, Decisión D3).
- **SC-005**: El rate limit declarado se **respeta** (0 violaciones medibles en
  logs/tests).
- **SC-006**: Añadir xhamster **no requiere modificar el core**: solo se añaden ficheros
  del adapter, su registro, seed y fixtures (objetivo de producto, SC-007 de la spec 002).
- **SC-007**: Un fallo persistente de xhamster (fixtures) **no impide** que los jobs de
  xvideos se procesen.

## Assumptions

- **Estructura real observada en prospección (2026-08-19, recursos públicos, sin bypass)**:
  listado con ítems `div.video-thumb[data-video-id]` y enlaces
  `a.video-thumb__image-container[data-role=thumb-link]` a
  `/videos/<slug>-<id>` (id numérico o alfanumérico), `data-previewvideo` (mp4 av1 +
  fallback), `data-sprite` (hover sprite webp 526×298 en CSS; fichero real 5260×298 ≈
  20 tiles; **el sprite del vídeo principal en la página de vídeo vive en
  `window.initials.spriteLoader.template`**, p. ej. `160x160.50.s.jpg` → fichero real
  8000×131 → 50 tiles de 160×131 con `spriteCount=50`; los `data-sprite` del HTML de la
  página de vídeo pertenecen a vídeos **relacionados** y no se usan),
  paginación numérica `/categories/amateur/N` con salto a páginas de numeración alta
  (`/16828`, `/33654`); página de vídeo con `og:title`/`og:url`/`og:image`,
  `window.initials.videoModel` (`id`, `duration`, `title`, `created`, `tags`,
  `keywords`), **sin JSON-LD** ni `og:duration`; robots.txt sin `Sitemap:` y con
  `/premium/` y filtros de categoría disallowed. **Todo esto se re-valida con fixtures
  versionados durante la implementación** (mismo patrón que los PR-043…PR-053 de xvideos).
- El host de trabajo es **`xhamster.com`** (dominio neutro, Decisión D1); los locales
  (`es.xhamster.com`, etc.) sirven el mismo catálogo y no se usan como base, pero
  `es.xhamster.com` se acepta en la allowlist como objetivo de redirect/URL canónica
  (corrección A1 del análisis post-tasks: con IP española `og:url` puede servirse en
  `es.*`).
- El `external_id` es el **sufijo de la URL canónica** `/videos/<slug>-<id>` (estable y
  derivable de `og:url`), no el `data-video-id` interno del listado.
- **Hallazgo del análisis post-tasks (A2)**: la página real de `/categories/amateur`
  trae 46–51 ítems; el backfill real debe usar `--limit 64` (≥ tamaño de página
  observado) para no disparar el error de truncación.
- La revisión legal/ToS/robots es responsabilidad **del humano**; sin ella el adapter
  permanece deshabilitado y el desarrollo usa solo fixtures.
- El crawler reutiliza pipeline, jobs, repositorios, registry y CLI de la spec 002 sin
  cambios; cualquier cambio necesario en el core es una enmienda explícita aparte.
- El contenido audiovisual real capturado en validaciones **no se commitea** (SEC-004).

## Dependencies

- `specs/000-platform-foundation` — esqueleto técnico, `IMPLEMENTED`.
- `specs/001-visual-search-spike` — pipeline frames/pHash/embedding/ANN, `IMPLEMENTED`.
- `specs/002-source-sdk-crawler` — contrato `SourceAdapter`, registry, jobs, pipeline y
  primer adapter real (xvideos), `IMPLEMENTED`.
- Revisión legal/ToS/robots del humano para xhamster (puerta SEC-002).
- Supabase local operativa (Docker) para la tabla `jobs`/`sources`.

## Risks

- **Cambios de HTML de xhamster** → adapters aislados + fixtures versionados que detectan
  la regresión.
- **Bloqueo / rate limits / anti-bot** → backoff con jitter, respeto estricto a límites y
  robots, aislamiento por fuente; nunca se intenta saltar protecciones.
- **Riesgo legal** → habilitación bloqueada hasta revisión humana; el desarrollo avanza
  con fixtures sin depender de ella.
- **Sprite sin semántica temporal documentada** (tiles sobre duración) → derivación
  uniforme con clamp `[0, duration_ms)` y `timestamp_ms=None` defensivo; la validación
  real de capturas del operador lo confirma o corrige (paridad PR-053 de xvideos).
- **Alcance del backfill real** → acotado a N vídeos de la categoría amateur definidos por
  el operador.

## Open Questions

_Todas las preguntas críticas resueltas en la ronda de clarificación del 2026-08-19
(D1–D5, ver `## Historial de decisiones`)._ Sin ambigüedades pendientes capaces de
cambiar la implementación.

## Approval

**Estado**: `APPROVED` — aprobada por el humano responsable el 2026-08-19 (frase exacta
**`Especificación aprobada`**). Se habilita `technical-planning`.

## Historial de decisiones

- **2026-08-19 · Borrador inicial**: spec creada por el agente (DeepSeek) tras preflight y
  prospección factual de recursos públicos de xhamster (robots.txt, listado
  `/categories/amateur`, página de vídeo, sprite/previews en CDN `xhcdn.com`). Preguntas
  abiertas listadas en `## Open Questions`.
- **2026-08-19 · D1 (Q1)**: **Host canónico = `xhamster.com`** (dominio neutro; los
  locales sirven el mismo catálogo). Allowlist de página: `xhamster.com` +
  `www.xhamster.com`. *Decisión del humano responsable.*
- **2026-08-19 · D2 (Q2)**: **Discover solo por sección** `/categories/amateur` vía
  `--section`; **sin `section` el adapter rechaza el discover** (fail-fast, error claro).
  En v1 no se explora la home. *Decisión del humano responsable.*
- **2026-08-19 · D3 (Q3)**: **Assets de v1 = sprite/storyboard + thumbnail**; los
  previews mp4 (`data-previewvideo`) observados **no se exponen** (`preview_url=None`)
  y quedan documentados para fases futuras. *Decisión del humano responsable.*
- **2026-08-19 · D4 (Q4)**: **Backfill de validación real con `--max-videos 50`** sobre
  `/categories/amateur`. *Decisión del humano responsable.*
- **2026-08-19 · D5 (Q5)**: **Revisión legal/ToS/robots de xhamster OK en modo prueba**
  (puerta SEC-002 liberada por el humano) → manifest con `robots_reviewed=true`,
  `terms_reviewed=true`, `review_date="2026-08-19"`. La habilitación efectiva sigue
  exigiendo `sources.enabled=true` en BD (aprobación humana final). *Decisión del humano
  responsable.*
- **2026-08-19 · Aprobación**: spec **`APPROVED`** por el humano responsable (frase
  exacta "Especificación aprobada"). Se habilita `technical-planning` (plan + ADR).
- **2026-08-19 · Análisis post-tasks (speckit-analyze)**: sin hallazgos CRITICAL;
  correcciones aplicadas por el orquestador: A1 (allowlist de página +
  `es.xhamster.com` como objetivo de redirect/URL canónica), A2 (`--limit 64` para
  el backfill real, página de 46–51 ítems), A3 (trazabilidad DATA-002/DATA-003 en
  tasks.md). Feature **planteada y lista para implementar** (PR-062…PR-065).
