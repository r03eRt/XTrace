# Runbook: refinamiento temporal bajo demanda

## Propósito y límites

Este runbook cubre una búsqueda local con el primer pase del índice base y el segundo
pase de refinamiento de la feature 006. El refinamiento solo examina candidatos
principales y assets visuales públicos permitidos por un adapter habilitado.

Los invariantes operativos son:

- el índice base conserva su política global de 8 frames objetivo;
- una búsqueda no escribe en `frames` ni reindexa el corpus;
- el límite por defecto es 3 candidatos, 30 assets por candidato, 10 s por búsqueda
  y 3 s por candidato;
- los máximos absolutos son 5 candidatos, 30 assets, 10 000 ms por búsqueda y 3 000
  ms por candidato;
- un timestamp solo se sustituye por evidencia realmente evaluada y visualmente no
  peor; si no, se devuelve el timestamp del índice base como aproximado.

## Arranque local

1. Comprueba que Supabase local está disponible si quieres telemetría. Usa una base
   desechable antes de aplicar `supabase db reset`.
2. Arranca la API con `--host 127.0.0.1 --port 8000` y el frontend en el puerto 3000.
3. Verifica `GET http://127.0.0.1:8000/health` y abre `/buscar`.
4. Empieza con `XTRACE_REFINEMENT_ENABLED=false`, confirma que el primer pase funciona
   y habilita después el segundo pase con límites explícitos.

La guía reproducible con los comandos completos está en
[`specs/006-temporal-refinement/quickstart.md`](../../specs/006-temporal-refinement/quickstart.md).

## Cambiar la política de forma segura

Las variables `XTRACE_REFINEMENT_*` son server-only. Mantén los valores dentro de la
tabla de la quickstart y reinicia el proceso para aplicar cambios. Para una fuente
con disponibilidad limitada se puede usar un override acotado:

```bash
XTRACE_REFINEMENT_SOURCE_OVERRIDES='{"xvideos":{"candidate_limit":2,"enabled":true}}'
```

No uses overrides para superar los máximos, ni para activar una fuente cuyo manifest,
allowlist, robots o términos de servicio no hayan sido revisados. Si la configuración
no valida, el sistema debe fallar cerrado y responder con el primer pase.

## Interpretar una búsqueda

| Estado | Significado operativo | Acción |
| --- | --- | --- |
| `completed` / `improved` | Se evaluó evidencia y un asset respaldado mejoró el timestamp. | Mostrarlo como aproximado y conservar su provenance. |
| `completed` / `unchanged` | Hubo evaluación, pero no hubo mejora segura. | Conservar el resultado base. |
| `disabled` | La política está apagada o no es válida. | No contactar la fuente; revisar configuración solo si se esperaba activar. |
| `unavailable` | La fuente/adaptador no ofrece assets utilizables o está fuera de la allowlist. | No reintentar eludiendo restricciones; usar el resultado base. |
| `limited` | Se agotó candidato, asset, bytes o tiempo. | Aceptar el mejor resultado disponible; reducir coste o revisar disponibilidad. |
| `failed` | Error controlado del segundo pase. | Conservar el primer pase y revisar métricas/logs acotados. |

Un error de refinamiento no equivale a vídeo ausente. Si el primer pase no devuelve
candidatos, no hay evidencia que refinar y debe comunicarse la ausencia de candidato.

## Incidentes y fallback

### 403/404, rate limit o CAPTCHA

Detén la repetición manual. La respuesta esperada es `unavailable`, `limited` o
`failed`, con el primer pase intacto. No se cambian headers para eludir controles, no
se intenta otra URL y no se descarga el vídeo.

### Timeout o proveedor lento

Comprueba que `XTRACE_REFINEMENT_SEARCH_TIMEOUT_MS` y
`XTRACE_REFINEMENT_CANDIDATE_TIMEOUT_MS` siguen dentro de sus máximos. El servicio
debe retornar con el presupuesto agotado; no aumentes el límite para ocultar el
incidente. Repite solo con un fixture o adapter mock hasta entender el caso.

### Assets corruptos, duplicados o sin timestamp

El materializador debe descartarlos. No conviertas la posición del nombre del fichero,
la duración o una interpolación en una verdad temporal. Si no queda evidencia válida,
mantén `base_index`/`unchanged` o el fallback correspondiente.

### Temporales o telemetría

Después de una búsqueda, verifica que el directorio configurado en
`XTRACE_API_WORK_ROOT` se limpia. La telemetría contiene contadores y referencias
sanitizadas, no la imagen de consulta ni bytes. RLS debe impedir lectura desde
`anon`/`authenticated`; solo la conexión server-side registra métricas.

## Prueba remota mediante túnel

El modo recomendado es local. Para una prueba remota puntual:

1. Mantén API y frontend escuchando en loopback.
2. Usa túneles HTTPS temporales separados si el navegador necesita alcanzar ambos
   servicios; publica únicamente los puertos de la aplicación.
3. Configura `NEXT_PUBLIC_XTRACE_API_URL` con la URL pública de la API antes de iniciar
   el frontend y añade la URL pública del frontend a `XTRACE_API_CORS_ORIGINS`.
4. No expongas el puerto de Supabase, no pongas secretos en variables públicas y no
   mantengas un túnel abierto después de la prueba.

Un túnel no resuelve autenticación ni autoriza una fuente. Si el proveedor devuelve
403, paywall, CAPTCHA o una página de acceso, se aplica el fallback; nunca se intenta
sortearlo.

## Benchmark y decisión de adopción

El benchmark necesita un manifest pareado con verdad temporal independiente, mínimo
30 positivos únicos, local + web y los tramos de duración requeridos. Ejecuta el
script con `--output` fuera del repositorio. Revisa `accepted`, `coverage`, las puertas
`SC-001`/`SC-002`/`SC-003`/`SC-007`, Top-1/Top-5, error temporal, coste y latencia.

Un benchmark rechazado no justifica cambiar defaults. Incluso uno aceptado es
evidencia para revisión: no modifica por sí solo el índice de 8 frames ni reindexa el
corpus. Cualquier cambio posterior requiere una spec/tarea aprobada y una nueva
validación.

## Cierre

- Apaga los procesos locales y los túneles.
- Conserva solo el JSON de benchmark fuera del checkout si hace falta auditarlo.
- No subas capturas, consultas, assets ni temporales.
- Registra `search_id`, estado y métricas agregadas en el handoff de la tarea, sin
  incluir URLs privadas, secretos o contenido multimedia.
