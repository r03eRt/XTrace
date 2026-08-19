## Hallazgos de seguridad

- [SEC-006] **LOW — operación**: el runbook permite túneles HTTPS temporales para
  pruebas remotas. La configuración exige publicar solo frontend/API, mantener
  Supabase en loopback, usar CORS explícito y cerrar el túnel al terminar. No se
  detecta una exposición por defecto; el riesgo queda acotado a la operación
  manual.
- [SEC-007] **INFO — deuda preexistente**: dos fixtures de integración API fallan
  por datos de prueba heredados (`work` no creado y `tags` no serializado como
  JSON). No afectan autenticación, RLS ni la ruta de refinamiento; deben
  corregirse en una tarea de higiene separada si se exige mypy/tests globales
  absolutamente verdes.

## Comprobaciones

- `src/server/supabase-admin.ts` está marcado `server-only`; la clave
  `SUPABASE_SERVICE_ROLE_KEY` no se importa desde cliente.
- `.env.example` no contiene secretos reales; las variables sensibles son
  placeholders vacíos y las claves server-only no usan `NEXT_PUBLIC_*`.
- La migración `20260818000000_temporal_refinement.sql` habilita RLS y revoca
  permisos a `anon`/`authenticated`; no crea políticas que abran lectura.
- pgTAP y las pruebas Postgres verifican permisos positivos server-side,
  denegaciones client-side, constraints de estado/contadores, cascada y ausencia
  de columnas de consulta/media.
- La validación del servidor limita multipart a 10 MiB, elimina temporales en
  `finally`, y aplica timeouts/rate-limit/allowlist antes de obtener assets.
- El bridge exige `AdapterRegistry`/fuente habilitada, el materializador acepta
  solo `thumbnail`/`storyboard`, y XVIDEOS conserva allowlist y validación
  anti-DNS-rebinding en descubrimiento y assets.
- No hay código de descarga de vídeo completo, bypass de CAPTCHA/paywall/DRM/auth,
  reconocimiento facial ni logging de bytes/URLs privadas en la ruta de
  refinamiento.
- Búsqueda estática de secretos y de rutas `preview`/vídeo completo: sin
  secretos reales ni nuevas rutas de acceso detectadas; las apariciones son
  comentarios, contratos o tests negativos esperados.

## Veredicto: PASS

No se encontró vulnerabilidad de severidad alta o crítica. La exposición remota
solo es una operación temporal documentada y no forma parte del despliegue por
defecto.
