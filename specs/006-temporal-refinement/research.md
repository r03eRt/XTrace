# Research: Refinamiento temporal bajo demanda

**Fecha**: 2026-08-18
**Spec**: [spec.md](spec.md)
**Estado**: decisión técnica consolidada para planificación

## Estado actual observado

1. `POST /search` ejecuta la cadena del spike (`ImageSearch` + `rank_candidates`) y
   devuelve el timestamp del mejor frame del índice base.
2. La API ya limpia los temporales de consulta y registra una fila en `searches`, pero
   no tiene una frontera para consultar assets de una fuente ni una métrica de segundo
   pase.
3. El crawler ya contiene el contrato `SourceAdapter`, el gate de compliance de
   `AdapterRegistry`, `SafeHTTPClient`, `AssetFetcher` y la allowlist de assets de
   XVIDEOS. Reutilizar esas piezas evita crear una segunda vía de acceso a la web.
4. El índice base persiste `videos` y `frames`, pero no debe recibir frames de una
   búsqueda de usuario. Los vídeos web ya relacionan `videos.source_id` con
   `sources.enabled` y conservan `external_id`, `page_url`, duración y thumbnail.
5. El frontend valida la respuesta con Zod y actualmente muestra el timestamp sin
   distinguir si procede del índice o de una evidencia posterior.

## Decisiones técnicas

### D1 — Refinamiento síncrono y acotado dentro de `POST /search`

El segundo pase se ejecuta en la misma petición, después del ANN base, porque la spec
exige un resultado automático y un límite de 10 s por búsqueda. Se procesa como máximo
la política configurada de candidatos (3 por defecto, 5 absoluto). Un worker asíncrono
se reserva para una futura modalidad de búsqueda diferida y no forma parte de esta
feature.

Alternativa descartada: encolar un job para responder después. No cumpliría la UX
actual ni permitiría entregar el timestamp refinado en la respuesta de la búsqueda.

### D2 — El adapter sigue siendo la única frontera de red

La API añadirá `xtrace-crawler` como dependencia editable y usará un registro de
adapters pequeño, inicialmente con `XvideosAdapter`. La resolución exige:

- `sources.enabled=true` en la BD;
- manifest de código con robots, términos y fecha de revisión válidos;
- host de asset presente en la allowlist declarada por el adapter.

La API no parsea HTML ni construye URLs de thumbnails por su cuenta. Para cada candidato
llama a `get_visual_assets`, filtra a `thumbnail`/`storyboard` y descarta `preview` para
que el refinamiento nunca abra una vía de descarga de vídeo.

Alternativa descartada: duplicar el parser de XVIDEOS dentro de la API. Duplicaría
permisos y rompería ADR-0009.

### D3 — Evaluar assets en memoria, sin escribir el índice

`TemporalRefinementEvaluator` materializa cada imagen con `AssetFetcher` y
`open_image_limited`, calcula embeddings en lotes pequeños y cierra cada imagen al
terminar. Solo conserva en memoria el mejor candidato del vídeo. El índice vectorial y
la tabla `frames` no se modifican durante una búsqueda.

Solo se acepta un asset con `timestamp_ms` respaldado por el adapter (incluidos
timestamps producidos por el splitter de storyboards ya declarado por ese adapter). No
se deriva un timestamp genérico a partir de la posición de una URL ni se interpola entre
frames.

### D4 — Mantener el ranking base y proteger la evidencia

El refinamiento no reordena vídeos ni cambia `match_score`, `matching_frames` o la
evidencia pHash del primer pase. Para cada vídeo conserva el timestamp base y solo lo
sustituye si un asset adicional válido supera la guardia visual del candidato y tiene
una posición temporal distinta y trazable. Si no supera la guardia, se devuelve el
resultado base con estado `unchanged`. Esto implementa el fallback cuando la evidencia
refinada empeora y evita que un thumbnail genérico robe un candidato correcto.

### D5 — Política explícita y fail-closed

La política se lee de `Settings` y puede desactivar la feature sin reindexar:

| Límite | Default | Máximo/validación |
| --- | ---: | --- |
| Refinamiento habilitado | `true` | `false` permitido |
| Candidatos | `3` | `1..5` |
| Assets adicionales por candidato | `30` | `1..30` |
| Presupuesto búsqueda | `10_000 ms` | `>0` |
| Presupuesto por candidato | `3_000 ms` | `>0` y no mayor que el global |
| Bytes por asset | `10 MiB` | `>0` |
| Reintentos de asset | `0` | no se eluden límites de la fuente |

La política efectiva también considera el límite declarado en el manifest y una
allowlist de fuentes habilitadas por entorno. Agotado el tiempo o el presupuesto de
assets, el estado es `limited` y el resultado base permanece válido.

### D6 — Métricas durables, media efímera

Se añaden tablas server-only `search_refinements` y
`search_refinement_evidence`, relacionadas con `searches`. Se guardan contadores,
latencias, estados y la URL pública sanitizada del asset seleccionado (sin query ni
fragmento, además de un hash estable), nunca bytes de consulta, bytes de vídeo ni una
imagen persistente. La limpieza existente de `searches` elimina las filas hijas por
`ON DELETE CASCADE`.

### D7 — Benchmark pareado antes de cambiar defaults

El benchmark reutiliza exactamente las mismas consultas y verdad temporal independiente
para comparar base/refinamiento. Reporta Top-1/Top-5, error absoluto, latencias, assets,
fuente y tramo de duración. El resultado se guarda fuera de Git y solo permite adoptar
la feature como política operativa si satisface SC-001..SC-008.

## Riesgos abiertos tratados en el diseño

- **Galería cambiante o 403**: cada asset degrada de forma independiente; el adapter y
  la allowlist son la autoridad; se registra el motivo tipado sin reintentos infinitos.
- **Latencia de fuentes lentas**: `asyncio.timeout` por búsqueda/candidato, límite de
  assets y cancelación; el primer pase nunca espera más allá de su propio contrato.
- **URLs no trazables**: solo se presentan timestamps con `timestamp_ms` válido y se
  guarda la procedencia; posición sin timestamp se descarta.
- **Acoplamiento API-crawler**: el plan limita el acoplamiento a los modelos y
  protocolos ya públicos; la extracción de assets queda en un módulo de refinamiento
que depende del contrato, no del pipeline de jobs.
