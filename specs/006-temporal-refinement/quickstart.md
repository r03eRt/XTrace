# Quickstart: refinamiento temporal bajo demanda

Esta guía opera la feature 006 en local o en un entorno controlado. El refinamiento
es un segundo pase sobre los candidatos del índice base: no sustituye el índice, no
reindexa el corpus y no descarga vídeos completos.

## Prerrequisitos

- Rama de la feature y spec `006-temporal-refinement` en estado `APPROVED`.
- Python 3.11, `uv`, Node/pnpm y dependencias instaladas.
- Supabase local disponible en `127.0.0.1:55322` si se quiere persistir telemetría.
- Un índice base ya existente. La feature no crea frames nuevos en `frames` durante
  una búsqueda.
- Un adapter habilitado y revisado para la fuente que se quiera probar. Los tests
  usan `MockAdapter`/`MockTransport`; no contactan fuentes reales.

## 1. Preparar la base local

La migración de la feature crea `search_refinements` y
`search_refinement_evidence`, con RLS deny-by-default y retención heredada de
`searches`. Para una base local desechable:

```bash
supabase db reset
```

No ejecutes `db reset` contra una base con datos que quieras conservar. Comprueba el
esquema y los tests RLS antes de usar un entorno compartido:

```bash
pnpm test:db
```

La API usa `SUPABASE_DB_URL` solo server-side. Si se deja vacío, la búsqueda puede
funcionar con el backend in-memory, pero no se persiste la telemetría Postgres.

## 2. Configurar los límites

Las variables se leen en el proceso de la API. También existe el alias con prefijo
`XTRACE_API_` para despliegues que lo requieran; no se deben exponer como
`NEXT_PUBLIC_*`.

| Variable | Default | Límite efectivo | Propósito |
| --- | ---: | ---: | --- |
| `XTRACE_REFINEMENT_ENABLED` | `true` | booleano | Activa/desactiva el segundo pase. |
| `XTRACE_REFINEMENT_CANDIDATE_LIMIT` | `3` | `1..5` | Candidatos del ranking base que pueden refinarse. |
| `XTRACE_REFINEMENT_MAX_ASSETS_PER_CANDIDATE` | `30` | `1..30` | Assets públicos evaluables por candidato. |
| `XTRACE_REFINEMENT_SEARCH_TIMEOUT_MS` | `10000` | `1..10000` | Presupuesto total de una búsqueda. |
| `XTRACE_REFINEMENT_CANDIDATE_TIMEOUT_MS` | `3000` | `1..3000` y `<=` total | Presupuesto por candidato. |
| `XTRACE_REFINEMENT_MAX_ASSET_BYTES` | `10485760` | `>0` | Límite de un asset visual (10 MiB por defecto). |
| `XTRACE_REFINEMENT_POLICY_VERSION` | `temporal-refinement-v1` | texto no vacío | Identidad de la política en métricas. |
| `XTRACE_REFINEMENT_SOURCE_OVERRIDES` | `{}` | JSON validado | Overrides acotados por fuente. |

Ejemplo seguro para una prueba local, con un límite menor por coste:

```bash
export XTRACE_REFINEMENT_ENABLED=true
export XTRACE_REFINEMENT_CANDIDATE_LIMIT=3
export XTRACE_REFINEMENT_MAX_ASSETS_PER_CANDIDATE=30
export XTRACE_REFINEMENT_SEARCH_TIMEOUT_MS=10000
export XTRACE_REFINEMENT_CANDIDATE_TIMEOUT_MS=3000
export XTRACE_REFINEMENT_SOURCE_OVERRIDES='{"xvideos":{"candidate_limit":2}}'
```

Un JSON inválido, un campo desconocido o un valor fuera de rango desactiva el
refinamiento de forma fail-closed; no amplía silenciosamente el presupuesto. Un
override por fuente tampoco puede superar los máximos absolutos.

## 3. Arrancar API y frontend

La API escucha en loopback por defecto y el frontend usa
`http://127.0.0.1:8000` como base de API si no se configura otra URL:

```bash
cd services/api
SUPABASE_DB_URL=postgresql://postgres:postgres@127.0.0.1:55322/postgres \
XTRACE_REFINEMENT_ENABLED=true \
uv run uvicorn xtrace_api.main:app --host 127.0.0.1 --port 8000
```

En otra terminal:

```bash
pnpm dev
```

Abre [http://localhost:3000/buscar](http://localhost:3000/buscar). El frontend
mostrará el origen del timestamp como aproximado: `refined_asset` cuando ganó un
asset evaluado y `base_index` cuando se conserva el frame del índice.

## 4. Comprobar fallback y límites

Para verificar que el primer pase sigue funcionando con la feature apagada:

```bash
XTRACE_REFINEMENT_ENABLED=false \
SUPABASE_DB_URL=postgresql://postgres:postgres@127.0.0.1:55322/postgres \
uv run --project services/api uvicorn xtrace_api.main:app --host 127.0.0.1 --port 8000
```

La respuesta conserva los resultados base y marca `refinement.status=disabled`.
Según la fuente o el presupuesto, `unavailable`, `limited` y `failed` también
conservan el resultado base; `unchanged` significa que sí se evaluaron assets, pero
ninguno mejoró la evidencia. Ninguno de esos estados autoriza a inventar un timestamp
intermedio.

Casos que deben probarse antes de habilitar una fuente real:

- fuente deshabilitada, adapter desconocido o sin assets: `unavailable`/`disabled`;
- 403, 404, rate limit, timeout o asset ilegible: fallback controlado, sin reintentos
  fuera de la política;
- presupuesto de candidatos, assets o tiempo agotado: `limited`;
- ningún resultado en el primer pase: respuesta base válida o ausencia honesta de
  candidato, sin segundo pase.

## 5. Cumplimiento y frontera de assets

Solo se admiten `thumbnail` y `storyboard` públicos que el adapter habilitado exponga
con una posición temporal fiable. La frontera crawler/adapter aplica allowlist,
robots, términos de servicio, rate limits y validación de host/IP. El core de API no
construye URLs ni parsea HTML.

Está prohibido durante el refinamiento:

- saltar CAPTCHA, paywalls, DRM, autenticación, anti-bot o cualquier control de
  acceso;
- solicitar o conservar `preview`, streams o vídeos completos;
- reconocimiento facial o identificación biométrica;
- guardar imágenes de consulta, bytes de assets o temporales en Git, logs públicos o
  datasets.

El número real de assets puede ser inferior al límite. Por ejemplo, si una fuente
publica solo uno o dos thumbnails permitidos, se evalúan esos y el resultado queda
limitado a esa evidencia; aumentar `MAX_ASSETS_PER_CANDIDATE` no genera assets que la
fuente no expone. La política global de 8 frames del índice base permanece separada y
no se modifica con esta feature.

## 6. Benchmark pareado antes de cambiar defaults

El benchmark consume únicamente metadatos ya producidos por otro proceso. No abre
imágenes, no descarga assets, no llama a la API ni inicia conexiones de red. El
manifest debe tener:

- al menos 30 consultas positivas únicas;
- verdad temporal anotada independientemente del asset evaluado;
- observaciones completas y pareadas de `base` y `refined`;
- una fuente `local` y una fuente `web`, además de los tramos `<5m`, `5-15m` y `>15m`
  cuando sean aplicables;
- `expected_video_id`, `duration_ms`, `truth_timestamp_ms`, ranking, timestamp
  predicho, assets, bytes, embeddings y latencia por observación.

Ejecuta el informe fuera del checkout para no introducir datos o artefactos en Git:

```bash
uv run --project services/api python scripts/benchmark_temporal_refinement.py \
  --manifest /ruta/manifest-pareado.json \
  --output /tmp/xtrace-benchmarks/temporal-refinement-run.json
```

El informe incluye Top-1/Top-5, error temporal, coste, latencia, cobertura por fuente
y duración, y las puertas `SC-001`, `SC-002`, `SC-003` y `SC-007`. `accepted=true`
solo significa que esa ejecución supera las puertas; el benchmark nunca cambia por sí
solo el default del índice ni habilita una fuente. Un manifest incompleto, duplicado,
con verdad inválida o con observaciones desparejadas debe producir `accepted=false` y
salida no adoptable.

## 7. Túnel opcional para una prueba remota

No hace falta un túnel para trabajar en local. Si se necesita probar desde otro
dispositivo, expón únicamente el frontend y, si hace falta, la API mediante un túnel
HTTPS temporal aprobado por el operador. No expongas Supabase ni cambies la API a
`0.0.0.0`.

El navegador remoto no puede usar `127.0.0.1` como API. En ese caso hay que publicar la
API con una URL temporal y arrancar el frontend con esa URL en
`NEXT_PUBLIC_XTRACE_API_URL`; el origen HTTPS del frontend debe figurar explícitamente
en `XTRACE_API_CORS_ORIGINS`. No pongas DSN, tokens ni contraseñas en URLs o variables
`NEXT_PUBLIC_*`, y cierra ambos túneles al terminar. La disponibilidad del túnel no
forma parte del contrato de la feature.

## 8. Limpieza y diagnóstico

Los temporales de consultas se eliminan en `finally`; el purge de `searches` arrastra
la telemetría por cascade según el TTL existente. Para inspeccionar el directorio
local configurado sin abrir ficheros de media:

```bash
du -sh "${XTRACE_API_WORK_ROOT:-/tmp/xtrace-api}" 2>/dev/null || true
```

Si el tamaño no vuelve a cero tras finalizar una búsqueda, detén la API, conserva el
log local para diagnóstico y revisa el cleanup antes de repetir con una fuente real.
