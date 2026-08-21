# 0016. Adapter redgifs: API oficial con token temporal, sin storyboard (tercera fuente real)

- **Estado**: Aceptada
- **Fecha**: 2026-08-20
- **Spec/Requisitos relacionados**: 008-redgifs-adapter · FR-001…FR-010, SEC-001…005,
  SC-001…007 · extiende ADR-0009 (contrato SourceAdapter) y el patrón de composición
  de PR-031/SC-007; primer adapter con `access_method="api"` (jerarquía FR-004 de la
  spec 002, nivel superior a sitemap/JSON/HTML/browser)

## Contexto

La spec 008 añade la tercera fuente real, `redgifs.com`, empezando por los nichos
`homemade` y `real-cellphone-clips`. La prospección factual (2026-08-19, recursos
públicos, sin bypass) mostró una estructura radicalmente distinta a
xvideos/xhamster/erome: `www.redgifs.com` es una SPA sin SSR (HTML inservible),
robots.txt permite `/niches/*` pero disallow `/watch/` y `/ifr/`, y RedGIFs expone
una **API oficial pública** (`api.redgifs.com`) documentada en
github.com/Redgifs/api: token temporal sin clave (`GET /v2/auth/temporary`, JWT
`scope=read`, ≈24h), listado de nicho paginado por `page`
(`GET /v2/niches/{id}/gifs?order=new&count=100&page=N`, envelope
`{gifs,page,pages,total}`), objeto gif individual (`GET /v2/gifs/{id}` →
`{"gif":{...}}`, 404 `GifNotFound` si se retira). Cada ítem tiene como mucho dos
imágenes estáticas (`urls.thumbnail`, `urls.poster`) — **sin storyboard/sprite**; los
mp4 (`urls.sd/hd/silent`) son el contenido completo del ítem (prohibidos, SC-006 de
la spec 002).

Hay que decidir: método de acceso, manejo del token efímero, qué identificador usar,
cómo tratar la ausencia de storyboard, y cómo mantener "añadir una fuente no toca el
core" (SC-006/SC-007) con un mecanismo de acceso (API JSON con auth) nunca usado
antes en el SDK.

## Decisión

1. **Acceso `api`** (jerarquía FR-004, nivel 1 — el más alto usado hasta ahora):
   sin parsing HTML; `httpx` sobre `SafeHTTPClient` con allowlist de host
   `{"api.redgifs.com"}` para metadata y `{"media.redgifs.com"}` para assets. La web
   `www.redgifs.com` **nunca** se fetchea (SPA sin SSR + robots disallow en las
   rutas de interés).
2. **Token temporal como secreto efímero de sesión** (SEC-005): el adapter obtiene
   `GET /v2/auth/temporary` bajo demanda (primer uso), lo cachea **solo en memoria
   del proceso**, y lo renueva automáticamente ante `401` con el backoff del rate
   limiter existente (sin cambios en `crawling/ratelimit.py`). Nunca se persiste en
   BD, se loguea, ni aparece en fixtures/errores; una renovación persistentemente
   fallida produce un error tipado contenido en la fuente (paridad con el resto de
   fuentes: fallo aislado, sin caída del crawler).
3. **`external_id` = `id` del objeto gif, normalizado a lowercase** (la API exige
   lowercase en `GET /v2/gifs/<id>`; observado en mayúsculas/minúsculas mixtas según
   el origen del listado). Es estable y derivable directamente del JSON — a
   diferencia de xhamster/xvideos no hace falta derivarlo de una URL.
4. **Sin storyboard → solo thumbnail + poster, sin timestamps** (FR-005): el adapter
   emite hasta **dos** `VisualAsset(kind="thumbnail", timestamp_ms=None)` — uno desde
   `urls.thumbnail` y otro desde `urls.poster` (kind "poster" no existe en el
   contrato; ambos se modelan como `thumbnail`, paridad FR-012: frame sin timestamp,
   sin fallar). **A diferencia de xhamster/xvideos, este adapter no exporta ningún
   grid resolver** — no hay sprite que recortar, así que no hay wire adicional en el
   CLI más allá del registro del adapter. Los mp4 (`urls.sd/hd/silent`) **nunca** se
   leen del JSON de respuesta más allá de validarlos como ausentes de la superficie
   expuesta (SC-004/SC-006).
5. **Discover acotado por sección `/niches/<id>`** (Decisión D2): `section`
   obligatorio con prefijo `/niches/`; sin sección o con sección inválida → error
   tipado (fail-fast, paridad D2 de la 007). Paginación por **`page`** (1-based,
   `count=100` máximo aceptado verificado) con anti-bucle (página repetida, 0 IDs
   nuevos, o `page >= pages` → fin); la cota `--max-videos 50` acota la cadena.
6. **`page_url` fijo, nunca fetcheado** (Decisión D5): `page_url =
   https://www.redgifs.com/watch/<external_id>` se guarda como referencia canónica
   pero el adapter **nunca** emite un GET contra `/watch/` ni `/ifr/` (robots
   disallow); la disponibilidad se comprueba únicamente vía `GET /v2/gifs/<id>`.
7. **Compliance**: manifest revisado en modo prueba (Decisión D4:
   `robots_reviewed=true`, `terms_reviewed=true`, `review_date="2026-08-19"`), seed
   con `enabled=false` y gate SEC-002 del registry sin cambios.

## Alternativas consideradas

- **Parsear el HTML de `www.redgifs.com`**: descartada de raíz — la prospección
  confirmó que es una SPA sin SSR (8 KB de shell, sin `og:` ni ítems); el JSON de la
  API es la única vía viable y además de mayor jerarquía (FR-004 nivel 1). Rechazada.
- **Usar `/v2/gifs/search` para discover**: cubre búsqueda por texto pero fuera del
  alcance v1 (Decisión D2, cota de 10k resultados, no ejercita el mecanismo
  `section` del contrato del mismo modo que un nicho). Rechazada para v1; posible
  ampliación futura documentada en la spec.
- **Persistir el token en BD para reuso entre procesos**: evitaría una petición de
  token por arranque del crawler, pero convierte un secreto efímero en estado
  persistente (viola SEC-005 y el principio de mínima superficie de secretos).
  Rechazada.
- **Descargar y exponer los mp4 cortos como "storyboard equivalente"**: los mp4 son
  el contenido completo del ítem (no hay recorte posible como con un sprite);
  expandir a mp4 requeriría enmienda formal a SC-006 de la spec 002 con revisión
  legal propia. Fuera de alcance de esta ADR.
- **Modelar `poster` como un `AssetKind` nuevo en el contrato**: el contrato ya
  cubre el caso con `thumbnail` (dos imágenes sin semántica temporal); añadir un
  kind nuevo tocaría el core sin necesidad real. Rechazada.

## Consecuencias

- (+) Tercera fuente con **0 cambios en el core** (solo adapter + registro + seed +
  fixtures): SC-006/SC-007 medibles con test AST, igual que xhamster/erome.
- (+) Primer adapter `access_method="api"`: ejercita de verdad la jerarquía FR-004 y
  demuestra que el contrato `SourceAdapter` es agnóstico al mecanismo de acceso
  (HTML vs JSON vs auth por token).
- (+) Sin sprite que recortar: el adapter es más simple que xhamster/xvideos (sin
  grid resolver, sin wire adicional en el CLI).
- (−) Densidad visual baja (1–2 frames sin timestamp por ítem, frente a los
  storyboards de xvideos/xhamster) — limitación documentada y medible con las stats
  existentes; no bloquea la fase pero condiciona la calidad de búsqueda del corpus
  redgifs.
- (−) El token temporal añade un estado mutable (aunque solo en memoria) que las
  fuentes anteriores no tenían; el adapter debe manejar la renovación ante 401 sin
  fugar el valor en ningún log/error — cubierto por tests dedicados.
- (−) Dependencia de la disponibilidad continua de la API pública de RedGIFs (sin
  SLA); un cambio de contrato JSON se detecta por fixtures versionados que fallan
  con error tipado, igual que el resto de fuentes.
