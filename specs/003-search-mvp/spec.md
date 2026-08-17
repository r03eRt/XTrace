# Feature Specification: MVP de Búsqueda — API REST + Frontend Mínimo (XTrace)

**Feature Branch**: `feature/003-search-mvp-spec`

**Created**: 2026-08-16

**Status**: APPROVED

**Input**: User description: "Fase 3 — MVP de búsqueda USABLE: API REST de búsqueda por
imagen + frontend mínimo Next.js (subir imagen → ver resultados) contra el índice real
existente (vídeos locales del spike + vídeos web del crawler, p. ej. los 104 del tag
`buttfucking`). Local por defecto, con acceso remoto temporal protegido para pruebas del
operador; sin despliegue público permanente."

> Origen de requisitos: `docs/PRODUCT_IDEA.md` (ASSUMPTION-2: sin acceso público hasta
> cerrar compliance; ASSUMPTION-6: media de consulta borrada inmediatamente) y el estado
> real del índice (Fase 1 spike `IMPLEMENTED` + Fase 2 crawler `IMPLEMENTED`, corpus
> real: 104 vídeos `indexed` del tag `buttfucking` con embeddings SigLIP).
>
> Esta spec describe **qué** y **por qué**. Las decisiones técnicas concretas (framework
> y empaquetado del API, mecanismo de subida) se fijan en `technical-planning` tras la
> **aprobación humana**; el despliegue queda acotado por **D4/D6** (sin despliegue
> permanente de la API; acceso remoto solo temporal y protegido). La reutilización del pipeline de
> búsqueda del spike (`xtrace_spike`) se menciona como **dirección**, no como diseño.

## Objetivo

Que el **operador** pueda probar la búsqueda visual de XTrace desde un **navegador** (o
`curl`) contra el **índice real existente**: sube una captura, recibe resultados con
**vídeo, score y timestamp**, y puede abrir el vídeo en su fuente original. Es el primer
paso de producto usable tras las fases de validación (spike) e ingesta (crawler). Opera
localmente por defecto y puede compartirse de forma temporal con el operador mediante un
túnel remoto protegido, sin convertirse en un despliegue público permanente.

## Alcance

- **API REST de búsqueda por imagen** con 4 endpoints (D1): **`POST /search`** (subida de
  una imagen → resultados rankeados), **`GET /health`**, **`GET /stats`** y **`GET
  /videos/{id}`** (ficha del vídeo con metadatos, fuente y enlace original).
- **Reutilización del pipeline de búsqueda del spike**: mismo comportamiento y **mismo
  contrato de resultados** que la CLI `search` del spike (paridad API-CLI).
- **Frontend mínimo** (una página, D2): subir imagen → ver resultados con título, fuente,
  score y timestamp, con enlace al vídeo original en la fuente; **sin exploración del
  corpus**.
- **Validación de media de consulta** (MIME por firma y tamaño, en servidor) y **borrado
  inmediato** tras procesar la búsqueda (extiende FR-018 del spike).
- **Logs de búsquedas**: registro analítico sin media, con TTL configurable.
- **Local por defecto**: frontend y API escuchan en loopback y no se despliegan de forma
  permanente.
- **Acceso remoto temporal del operador** (D6): se permite un túnel de Cloudflare
  supervisado, revocable y protegido por una capa de acceso autenticada. La URL no se
  considera un despliegue, no permanece activa al terminar la prueba y no expone
  credenciales ni la base de datos.

## Fuera de alcance

- Búsqueda por **clip/vídeo** (consistencia temporal, sigue diferida del spike, Decisión
  D1 de 001) y búsqueda por **URL**.
- **Autenticación, cuentas de usuario y panel de administración**.
- **UI de takedowns/reports** (el mecanismo de exclusión del índice ya existe y la API lo
  respeta, pero no hay interfaz nueva para gestionarlo).
- **Exploración del corpus en el frontend** (listar/navegar el índice; D2: solo upload +
  resultados).
- **Ranking nuevo** o cambios en la cadena de búsqueda (se reutiliza la del spike tal
  cual).
- **Crawler nuevo** o reindexado del corpus (se consume el índice real existente).
- **Exposición pública anónima**, Quick Tunnels sin protección de acceso, publicación
  permanente y adaptación **móvil**.
- Almacenamiento de vídeos completos o de la media de consulta.

## Actores

- **Operador** (persona técnica, único usuario en esta fase): sube una captura desde el
  navegador o `curl`/Swagger, revisa los resultados y opera la API (health, stats).
- **API de búsqueda** (proceso interno): valida la media, ejecuta el pipeline de búsqueda
  del spike contra el índice real y devuelve el contrato de resultados.
- **Frontend mínimo** (proceso interno): página única que sube la imagen a la API y
  renderiza los resultados.
- **Sistema de búsqueda del spike** (reutilizado): normaliza → pHash → embedding → ANN →
  agrupar → ranking (comportamiento inalterado).

## Historias de usuario

### User Story 1 - Buscar por imagen desde la API con los mismos resultados que la CLI (Priority: P1)

El operador quiere lanzar una búsqueda por imagen vía HTTP (`curl`/Swagger) y recibir
exactamente los mismos resultados que ya conoce de la CLI del spike, para integrar y
automatizar sin sorpresas. La API expone los 4 endpoints fijados en **D1**: `POST
/search`, `GET /health`, `GET /stats` y `GET /videos/{id}`.

**Why this priority**: La API es el núcleo de la fase; sin paridad con la CLI validada, el
resto (frontend, criterios) no es fiable.

**Independent Test**: Ejecutar `search` por CLI y la búsqueda por la API con la misma
imagen contra el mismo índice y comparar los JSON (mismos vídeos, orden y scores).

**Acceptance Scenarios**:

1. **Given** la API en marcha contra el índice real y una imagen válida, **When** el
   operador la envía por HTTP, **Then** la respuesta sigue el contrato estable de la CLI
   `search` (`search_id`, `processing_ms`, `results[]` con `video_id`, `local_ref`,
   `match_score`, `matching_frames`, `match_timestamp_ms`, `evidence`) y los top-k con sus
   scores coinciden con la salida de la CLI para la misma imagen.
2. **Given** una imagen inválida (no imagen o > 10 MB), **When** se envía a la API,
   **Then** la API responde con un error 4xx claro y **no** ejecuta la búsqueda.
3. **Given** una búsqueda ejecutada (con éxito o con error), **When** termina, **Then** la
   media de consulta ya no existe en el sistema (borrado inmediato, FR-018 del spike).

### User Story 2 - Buscar por imagen desde el frontend (Priority: P1)

El operador abre la página en el navegador, sube una captura y ve los resultados con
título, fuente, score y timestamp, pudiendo ir al vídeo original en la fuente.

**Why this priority**: Es la experiencia usable que motiva la fase; sin frontend no hay
"MVP usable" para el operador.

**Independent Test**: E2E WebdriverIO: subir una captura real del corpus desde la página y
verificar que su vídeo aparece en los resultados con el enlace a la fuente.

**Acceptance Scenarios**:

1. **Given** la página cargada y una imagen válida seleccionada, **When** el operador la
   envía, **Then** se renderizan los resultados ordenados con título, fuente, score y
   timestamp, y un enlace a la URL original del vídeo en la fuente (cuando exista).
2. **Given** un fichero inválido seleccionado en la interfaz, **When** se envía, **Then**
   aparece un mensaje de error claro (en español) y no se ejecuta la búsqueda.
3. **Given** una búsqueda en curso, **When** el operador espera, **Then** hay feedback de
   carga visible hasta que llegan los resultados o el error.

### User Story 3 - Operar la API (Priority: P2)

El operador quiere comprobar que la API responde (**`GET /health`**) y conocer el estado
básico del índice (**`GET /stats`**) sin usar la CLI.

**Why this priority**: Hace la API autosuficiente para el operador (diagnóstico rápido),
pero no es imprescindible para la búsqueda.

**Independent Test**: Llamar a `GET /health` y `GET /stats` y verificar que devuelven
estado y conteos coherentes con el índice real.

**Acceptance Scenarios**:

1. **Given** la API en marcha, **When** se consulta `GET /health`, **Then** responde con
   el estado del servicio.
2. **Given** la API contra el índice real, **When** se consulta `GET /stats`, **Then**
   devuelve métricas básicas (vídeos, frames, vectores, backend, proveedor de embeddings)
   coherentes con las de la CLI `stats`.
3. **Given** una petición malformada o media no soportada, **When** se envía, **Then** la
   API devuelve un error estructurado 4xx con mensaje claro en el idioma del frontend.

### User Story 4 - Probar remotamente mediante un túnel temporal (Priority: P2)

El operador quiere abrir la interfaz desde otro dispositivo o red durante una sesión de
prueba controlada, sin desplegar XTrace ni dejar el servicio accesible permanentemente.

**Why this priority**: Permite validar el flujo real desde fuera del equipo de desarrollo
sin convertir el MVP local en un servicio público.

**Independent Test**: Iniciar un túnel temporal protegido, acceder como operador
autorizado, ejecutar una búsqueda y comprobar que un cliente no autenticado es rechazado;
detener el túnel y verificar que la URL deja de responder.

**Acceptance Scenarios**:

1. **Given** frontend y API ejecutándose en loopback, **When** el operador inicia una
   sesión remota autorizada, **Then** puede acceder al flujo de búsqueda mediante una URL
   temporal protegida sin exponer Supabase ni secretos.
2. **Given** el túnel activo, **When** un cliente no autenticado intenta acceder, **Then**
   la capa de acceso lo rechaza antes de que la petición alcance XTrace.
3. **Given** una sesión de prueba terminada, **When** el operador detiene el túnel,
   **Then** la URL remota deja de proporcionar acceso al frontend y a la API.

### Edge Cases

- Fichero renombrado o con extensión falsa (no es una imagen real) → rechazado por firma
  MIME, no por extensión (415/400).
- Imagen > 10 MB → 413, sin procesar.
- Imagen con firma válida pero contenido corrupto/ilegible → 400, sin ejecutar búsqueda.
- Petición sin parte de fichero o con nombre vacío → 400.
- Búsqueda sin resultados por encima del umbral de match → respuesta con `results` vacío
  (no es un error).
- Índice/BD no disponible → 5xx con mensaje claro; el frontend muestra el error sin
  quedarse colgado.
- Vídeo excluido del índice → no aparece en los resultados (paridad con FR-014 del spike).
- Resultado sin URL de fuente (p. ej. vídeo del dataset local, solo `local_ref`) → se
  muestra la referencia local sin enlace, sin fallar.
- `match_timestamp_ms` sin timestamp fiable → `null`, sin fallar (paridad FR-012 del
  spike).
- Fallo al borrar la media → se registra como warning sin enmascarar el resultado.
- Búsquedas concurrentes → independientes, cada una con su `search_id`.
- Subida cancelada en el navegador → el frontend no deja estados colgados.
- Fallo o desconexión del túnel → la aplicación local continúa disponible y la sesión
  remota falla de forma cerrada.
- URL remota compartida accidentalmente → no concede acceso sin superar la capa de
  autenticación.

## Requirements _(mandatory)_

### Functional Requirements

- **FR-001**: La API REST MUST ofrecer **búsqueda por imagen**: el operador sube una
  imagen y recibe los vídeos rankeados del índice real, ejecutando el **mismo pipeline de
  búsqueda del spike** (normalizar → pHash → embedding → ANN → agrupar → ranking) sin
  reimplementarlo.
- **FR-002**: El endpoint de búsqueda MUST validar la media de consulta **en servidor** con
  los mismos criterios que la CLI del spike: fichero regular, ≤ 10 MB y firma MIME
  JPEG/PNG/WebP comprobada por cabecera (no por extensión ni Content-Type declarado).
- **FR-003**: El sistema MUST **borrar inmediatamente** la media de consulta tras procesar
  la búsqueda (éxito o fallo), garantizado aunque la búsqueda falle (extiende FR-018 del
  spike). La media rechazada por validación no se toca.
- **FR-004**: La respuesta de búsqueda MUST seguir un **contrato JSON estable que reutiliza
  el de la CLI `search`** (`search_id`, `processing_ms`, `results[]` con `video_id`,
  `local_ref`, `match_score`, `matching_frames`, `match_timestamp_ms` —que puede ser
  `null`— y `evidence.visual`/`evidence.phash`); MAY ampliarlo con metadatos de
  visualización (título, URL de la fuente) sin cambiar los campos existentes.
- **FR-005**: La API MUST producir **los mismos resultados que la CLI** (mismos vídeos,
  mismo orden y mismos scores) para la misma imagen contra el mismo índice y configuración
  (paridad API-CLI).
- **FR-006**: La API MUST exponer **`GET /health`**, que informa si el servicio responde.
- **FR-007**: La API MUST exponer **`GET /stats`** con métricas básicas del índice
  (vídeos, frames, vectores, backend, proveedor de embeddings), coherentes con la CLI
  `stats` (D1).
- **FR-008**: La API MUST exponer **`GET /videos/{id}`**, que devuelve la **ficha del
  vídeo** con sus **metadatos** (título, `local_ref`), **fuente** y **enlace original**
  (p. ej. `page_url` de los vídeos del crawler); responde **404** si el `id` no existe
  (D1).
- **FR-009**: El frontend MUST ofrecer una **página única** (D2) donde el operador sube
  una imagen y ve los resultados (título, fuente, score y timestamp por resultado), sin
  exploración del corpus.
- **FR-010**: Cada resultado MOSTRADO MUST incluir un **enlace a la URL original del vídeo
  en la fuente** cuando esta exista (p. ej. `page_url` de los vídeos del crawler).
- **FR-011**: La API MUST manejar los errores de forma clara y estructurada: **413** (media
  > 10 MB), **415** (tipo de media no soportado), **400** (solicitud/media inválida),
  **404** (recurso inexistente) y **5xx** (fallo interno), con mensajes en el idioma del
  frontend.
- **FR-012**: El sistema MUST registrar cada búsqueda como **analítica sin media** (tabla
  `searches` existente) con **TTL configurable**; la media de consulta nunca se guarda.
- **FR-013**: La búsqueda MUST operar contra el **índice real actual** (D5: dataset local
  del spike, 43 vídeos + vídeos web del crawler, 104 `indexed` del tag `buttfucking`) tal
  cual está, sin requerir reindexado previo en esta fase.
- **FR-014**: El operador MUST poder habilitar y revocar una sesión de acceso remoto
  temporal al frontend y a la API sin desplegar permanentemente ninguno de los dos.

### Security Requirements

- **SEC-001**: El MVP MUST ejecutarse **en local por defecto**, con frontend, API y base
  de datos ligados a loopback. Solo MAY habilitar acceso remoto durante una sesión de
  prueba explícita del operador conforme a SEC-007..SEC-010; no se permite despliegue
  público permanente.
- **SEC-002**: La validación de la media de consulta MUST hacerse **en servidor** (tamaño y
  firma MIME), no solo en la interfaz.
- **SEC-003**: El borrado de la media de consulta MUST estar **garantizado incluso ante
  fallos** (try/finally); un fallo de borrado se registra sin enmascarar el resultado.
- **SEC-004**: El acceso a la BD MUST usar credenciales de **servidor** (nunca expuestas al
  cliente); las tablas accesibles mantienen **RLS deny-by-default** (paridad con el spike;
  no se debilita RLS para esta fase).
- **SEC-005**: El sistema MUST NOT almacenar ni loguear la media de consulta ni su
  contenido (solo el registro analítico sin media).
- **SEC-006**: No se añaden **secretos** al repositorio; la configuración local usa
  variables de entorno (las ya contempladas en el skeleton).
- **SEC-007**: Toda sesión remota MUST estar protegida por una capa de acceso autenticada
  delante del frontend y la API; una URL aleatoria por sí sola no constituye
  autenticación. Los Quick Tunnels anónimos están prohibidos.
- **SEC-008**: El túnel MUST exponer únicamente el frontend y las rutas de API necesarias
  para la prueba. MUST NOT exponer Supabase, PostgreSQL, puertos de administración,
  secretos, ficheros locales ni endpoints de depuración.
- **SEC-009**: El acceso remoto MUST ser temporal, supervisado y revocable: se inicia por
  acción explícita del operador y se detiene al terminar la sesión. Un reinicio del equipo
  o de la aplicación no debe reabrirlo automáticamente.
- **SEC-010**: La sesión remota MUST mantener la validación, límites de tamaño, borrado
  inmediato de media y CORS restringido al origen temporal autorizado. Las credenciales
  del túnel o de su capa de acceso nunca se registran en el repositorio.

### Data Requirements

- **DATA-001**: Se reutiliza la tabla **`searches` existente** (registro analítico de
  consultas sin media, RLS deny-by-default); si la fase necesitara cambios de esquema,
  estos MUST ser una migración versionada y no destructiva — preferiblemente **ninguna
  tabla nueva**.
- **DATA-002**: El contrato de resultados deriva del **mismo índice y ranking del spike**
  (paridad de datos con la CLI); no se crea un corpus ni un ranking paralelo.
- **DATA-003**: El corpus de la fase es el **índice real actual** (D5): dataset local del
  spike (43 vídeos) + vídeos web del crawler (104 vídeos `indexed` del tag `buttfucking`
  con embeddings SigLIP reales), sin reindexar.

### Non-Functional Requirements

- **NFR-001**: El MVP debe ejecutarse **localmente con coste ~0 €** (CPU, Supabase
  local/Docker, sin servicios de pago).
- **NFR-002**: La búsqueda por imagen debe responder en **< 3 s p95** (objetivo a medir y
  reportar, no garantía; paridad con PRODUCT_IDEA y SC-003 del spike).
- **NFR-003**: La fase **reutiliza el pipeline de búsqueda del spike** (`xtrace_spike`) en
  lugar de reimplementar búsqueda/ranking (dirección confirmada; el empaquetado exacto se
  decide en `technical-planning`).
- **NFR-004**: El frontend y la API deben arrancar y funcionar en local con la
  configuración mínima documentada, sin servicios externos de pago.
- **NFR-005**: Habilitar o revocar el acceso remoto temporal no debe modificar el corpus,
  debilitar RLS ni impedir que el flujo local siga funcionando.

### UX Requirements

- **UX-001**: Los mensajes de error de la API y del frontend MUST estar en **español**
  (idioma del frontend).
- **UX-002**: El frontend MUST mostrar **feedback de carga** durante la búsqueda.
- **UX-003**: Los resultados MUST mostrarse **ordenados por score**, con **score y
  timestamp visibles** y el **enlace a la fuente identificable** (o la referencia local
  cuando no haya URL).

## Key Entities

- **Video** (existente, Fases 1-2): vídeo del índice; aporta al contrato del spike `title`
  y `page_url` (fuente original) — lo que el frontend muestra y enlaza.
- **Frame** (existente): unidad de búsqueda del índice (`video_id`, `timestamp_ms`
  opcional, `pHash`, `embedding`); sin cambios.
- **Search** (existente, tabla `searches`): registro analítico de una consulta ejecutada,
  sin media; TTL configurable.
- **SearchResult** (contrato de respuesta reutilizado): vídeo candidato con `match_score`,
  `matching_frames`, `match_timestamp_ms` (nullable) y `evidence` (visual/phash).

## Success Criteria _(mandatory)_

### Measurable Outcomes

- **SC-001**: **Paridad API-CLI**: para un conjunto representativo de consultas (≥ 5
  imágenes del corpus), la API devuelve **los mismos top-k vídeos, en el mismo orden y con
  los mismos scores** que la CLI `search` contra el mismo índice y configuración.
- **SC-002**: Una **captura real de un vídeo del corpus real actual** (D5: 104 vídeos del
  tag `buttfucking` + 43 del dataset local del spike) devuelve **su vídeo en el Top-5**
  vía API.
- **SC-003**: Tras cada búsqueda (con éxito o con error) la **media de consulta ya no
  existe** en el sistema (verificable: no queda fichero en disco ni temporales).
- **SC-004**: La latencia de búsqueda vía API se **mide y reporta**: **p95 < 3 s**
  (objetivo, no garantía).
- **SC-005**: El **E2E WebdriverIO** del frontend de una página (D2: subir captura → ver
  resultado con enlace a la fuente) es **verde en CI**.
- **SC-006**: La validación **rechaza con 4xx** la media inválida (no imagen, > 10 MB,
  corrupta) **sin ejecutar búsqueda**.
- **SC-007**: Durante una prueba remota, el operador autenticado completa una búsqueda de
  extremo a extremo y un cliente no autenticado no alcanza XTrace.
- **SC-008**: Tras detener la sesión remota, la URL deja de dar acceso mientras el
  frontend y la API continúan disponibles únicamente en loopback.

## Assumptions

- **D3 (enmendada por D6, 2026-08-17)**: el MVP se ejecuta localmente y sin auth propia;
  no se publica ni despliega permanentemente. Una sesión remota solo se permite con una
  capa de acceso externa autenticada y temporal.
- **D6 (aprobada por el humano responsable, 2026-08-17)**: el operador puede habilitar un
  túnel temporal de Cloudflare protegido para probar desde otra red o dispositivo. Un
  Quick Tunnel anónimo no queda autorizado.
- **D5 (confirmado por el humano responsable, 2026-08-16)**: el **corpus es el índice real
  actual** (Fases 1-2): **104 vídeos** `indexed` del tag `buttfucking` (web) + **43
  vídeos** del dataset local del spike; no se reindexa ni se crawlea contenido nuevo en
  esta fase.
- La media de consulta se **borra inmediatamente** tras procesar (ASSUMPTION-6
  confirmada).
- Los registros analíticos de búsqueda son **temporales con TTL configurable**
  (PRODUCT_IDEA).
- El frontend vive en la **aplicación Next.js existente del repo** (skeleton 000); el API
  es un servicio/endpoints nuevo cuyo framework y empaquetado exactos se deciden en
  `technical-planning`. La reutilización de `xtrace_spike` es **dirección**, no diseño.
- La latencia de embeddings en CPU local puede superar los 3 s con SigLIP real (medido
  7-11 s por consulta en el spike): SC-004 es objetivo a medir, no garantía.
- La legalidad del material usado para probar (capturas del corpus) es responsabilidad del
  operador; el contenido real no se commitea.

## Dependencies

- `specs/000-platform-foundation` — esqueleto Next.js (`src/app/**`), `IMPLEMENTED`.
- `specs/001-visual-search-spike` — pipeline de búsqueda, contrato de resultados CLI y
  tabla `searches`, `IMPLEMENTED`.
- `specs/002-source-sdk-crawler` — vídeos web con `title`/`page_url` y corpus real,
  `IMPLEMENTED`.
- Índice real operativo en Supabase local (Docker): corpus de la fase (D5) — tag
  `buttfucking` (104 vídeos `indexed`) + dataset local del spike (43 vídeos).
- WebdriverIO ya configurado en el repo (`wdio.conf.ts`) para el E2E de SC-005.

## Risks

- **Paridad API-CLI**: si la API no reutiliza exactamente la misma cadena de búsqueda, los
  resultados divergen de la CLI; mitigación: SC-001 y reutilización del mismo pipeline.
- **Latencia en CPU**: embeddings SigLIP en CPU local medidos en 7-11 s por consulta en el
  spike; el objetivo < 3 s p95 puede no cumplirse en local; se mide y reporta (SC-004) sin
  bloquear la fase.
- **Media sensible**: la imagen de consulta puede ser contenido sensible; mitigación:
  validación en servidor, temporal seguro y borrado inmediato (SEC-002/003), sin logs de
  media (SEC-005).
- **Exposición accidental**: un bind, túnel anónimo o despliegue incorrecto expondría una
  API de subida y contenido adulto; mitigación: loopback por defecto, autenticación previa
  al túnel, CORS exacto, revocación al terminar y revisión de seguridad
  (SEC-001/007..010).
- **Acoplamiento frontend-API**: si el frontend asume detalles del API antes de fijarse el
  contrato, hay retrabajo; mitigación: FR-004 como frontera estable.
- **Contenido adulto en CI/E2E**: el E2E necesita una captura de prueba; se usan fixtures
  locales permitidas sin commitear contenido real (paridad con la Fase 2).

## Open Questions

_Ronda 1 resuelta por el humano responsable el 2026-08-16 (decisiones D1..D5). La
enmienda D6 fue aprobada el 2026-08-17._

1. **Endpoints mínimos exactos** → **D1**: `POST /search` (subida de imagen → resultados)
   + `GET /health` + `GET /stats` + `GET /videos/{id}` (ficha con metadatos, fuente y
   enlace original).
2. **Frontend** → **D2**: una sola página (upload + resultados: título, fuente, score,
   timestamp y enlace al vídeo original); **sin exploración del corpus**.
3. **Auth y local** → **D3**: decisión original de ejecución solo local, posteriormente
   enmendada por D6 para permitir sesiones remotas temporales protegidas.
4. **Deploy** → **D4**: el frontend usa el **Preview automático de Vercel del PR** y la
   API no se despliega permanentemente; D6 permite únicamente un túnel temporal protegido.
5. **Corpus de prueba** → **D5**: índice real actual — **104 vídeos** del tag
   `buttfucking` (web) + **43 vídeos** del dataset local del spike.
6. **Acceso remoto temporal** → **D6**: permitido mediante túnel de Cloudflare temporal
   y autenticado; prohibido como Quick Tunnel anónimo o despliegue permanente.

## Approval

**Estado**: `APPROVED` — la spec original fue aprobada el 2026-08-16 y la enmienda D6 de
acceso remoto temporal fue aprobada por el humano responsable el 2026-08-17 con la frase
exacta "Especificación aprobada". Habilitado el paso a `technical-planning`.

**Cierre de la implementación (2026-08-17, PR-054…PR-058)**: la implementación está
completa y **SC-001/SC-003/SC-005/SC-006 quedan verdes** por tests automatizados (paridad
CLI-API, borrado inmediato, E2E WebdriverIO en la smoke suite de CI y validación 4xx sin
ejecutar búsqueda). La spec pasa a **`IMPLEMENTING`** (no a `IMPLEMENTED`) porque
**SC-002** (captura real de un vídeo del corpus → su vídeo en el Top-5 vía API) y el
reporte de **SC-004** (p95 de `processing_ms`, objetivo < 3 s, no garantía) son
**validación manual del operador** en local (quickstart §4/§Notas operativas; puerta
documentada en `docs/handoffs/PR-058.md`). El humano responsable la moverá a
`IMPLEMENTED` tras superar esa puerta.

## Historial de decisiones

- **2026-08-17 · Aprobación D6**: el humano responsable aprueba la enmienda de acceso
  remoto temporal con la frase exacta "Especificación aprobada".
- **2026-08-17 · D6 READY_FOR_REVIEW**: a petición del operador, se permite acceso remoto
  temporal mediante un túnel de Cloudflare protegido por autenticación, conservando
  loopback como valor por defecto y prohibiendo Quick Tunnels anónimos y despliegues
  permanentes. Pendiente de aprobación humana.
- **2026-08-16 · Aprobación**: spec **`APPROVED`** por el humano responsable (frase
  exacta "Especificación aprobada"). Se habilita `technical-planning`.
- **2026-08-16 · Borrador inicial**: decisión de alcance del operador: la Fase 3 es el
  **MVP de búsqueda usable** — **API REST de búsqueda por imagen + frontend mínimo
  Next.js** (subir imagen → ver resultados) contra el **índice real existente** (dataset
  local del spike + vídeos web del crawler); **solo local** (sin exposición pública,
  ASSUMPTION-2) y **sin auth** en esta fase. Quedan fuera: búsqueda por clip/URL,
  admin/takedowns UI, ranking nuevo y crawler nuevo.
- **2026-08-16 · Ronda de clarificación 1 — Decision del humano responsable**:
  - **D1 — Endpoints**: la API expone `POST /search` (subida de imagen → resultados
    rankeados), `GET /health`, `GET /stats` y `GET /videos/{id}` (ficha con metadatos,
    fuente y enlace original).
  - **D2 — Frontend**: una única página de upload + resultados (título, fuente, score,
    timestamp y enlace al vídeo original); sin exploración del corpus.
  - **D3 — Auth y exposición**: sin auth; solo local (frontend incluido); nunca se publica
    hasta cerrar compliance (ASSUMPTION-2).
  - **D4 — Deploy**: el frontend usa el Preview automático de Vercel del PR; la API solo
    local (no se despliega).
  - **D5 — Corpus de prueba**: índice real actual — 104 vídeos del tag `buttfucking`
    (web) + 43 vídeos del dataset local del spike.
