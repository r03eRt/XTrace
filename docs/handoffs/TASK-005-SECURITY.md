# Handoff — TASK-005-SECURITY

## Alcance y preflight

Revisión estática de seguridad y compliance de la feature 005 (muestreo adaptativo,
reindexación `REINDEX` y benchmark). Se leyeron íntegramente:

- `AGENTS.md`.
- `.specify/memory/constitution.md`.
- `specs/005-adaptive-frame-sampling/spec.md` (`APPROVED`).
- `specs/005-adaptive-frame-sampling/plan.md`.
- `specs/005-adaptive-frame-sampling/tasks.md`.
- `.agents/skills/security-review/SKILL.md`.
- `.env.example`.
- `src/server/`, `supabase/migrations/` y `supabase/tests/`.
- El diff de la feature y los handoffs TASK-005-001..004.

No se modificaron código, configuración, migraciones, tests ni `tasks.md`. La única
escritura de esta revisión es este handoff.

## Hallazgos de seguridad

No se encontraron hallazgos bloqueantes.

- [SEC-005-REV-001] PASS — No hay reconocimiento facial ni análisis de identidad de
  personas. El escaneo de código Python/TypeScript no encontró `face_recognition`,
  DeepFace, InsightFace, OpenCV facial ni equivalentes. La feature opera solo con
  imágenes, pHash, embeddings visuales y timestamps.
- [SEC-005-REV-002] PASS — No se introducen nuevos tipos de asset. `AssetKind` sigue
  limitado a `storyboard`, `thumbnail` y `preview`; no existe el tipo `video`. La
  selección adaptativa trabaja sobre assets ya entregados por el adapter y no fabrica
  imágenes, posiciones ni timestamps.
- [SEC-005-REV-003] PASS — No se añade descarga permanente ni acceso a vídeo completo.
  `REINDEX` reutiliza `get_visual_assets` y la ruta existente de assets permitidos;
  los temporales se eliminan en `finally`. Los previews siguen sujetos a `ffprobe`,
  al máximo verificable de 120 segundos y a limpieza temporal. No se cambió
  `AssetFetcher`, `PreviewFrameExtractor` ni la allowlist para ampliar ese alcance.
- [SEC-005-REV-004] PASS — No hay bypass de CAPTCHA, paywall, DRM, autenticación o
  anti-bot. El worker mantiene el gate de adapter/manifest y `sources.enabled`; las
  peticiones siguen pasando por `SafeHTTPClient`, allowlist de hosts, validación de
  esquema, redirects y DNS/IP resuelta. La feature solo reindexa fuentes habilitadas
  y vídeos no excluidos en estados `indexed`/`failed`, con una segunda comprobación en
  el handler antes de acceder a assets.
- [SEC-005-REV-005] PASS — La entrada operativa está validada en servidor/worker y CLI:
  UUID de `run_id`, perfil exacto `adaptive`/120 s/máximo 8, `limit >= 1`, estados y
  exclusión del vídeo, y consultas SQL parametrizadas. El perfil se valida de nuevo al
  procesar el job; no se evalúan expresiones ni se ejecutan comandos con `shell=True`.
- [SEC-005-REV-006] PASS — No se encontraron secretos nuevos ni credenciales en el
  diff. No cambiaron dependencias, `src/server`, migraciones ni configuración de RLS.
  `service_role` permanece restringido a servidor y las tablas de catálogo/jobs/índice
  mantienen RLS deny-by-default sin grants a `anon`/`authenticated`.
- [SEC-005-REV-007] PASS — La persistencia nueva usa valores parametrizados de psycopg;
  `run_id`/perfil se almacenan en el payload JSONB existente. No se expone una nueva
  ruta HTTP ni se amplían permisos de base de datos.

## Evidencia ejecutada

- `git diff --check` — PASS.
- Escaneo de rutas modificadas bajo `src/server` y `supabase` — sin cambios.
- Escaneo de dependencias (`pyproject.toml`, `package.json`, locks) — sin cambios.
- Escaneo de secretos, credenciales embebidas, facial recognition, `shell=True`,
  `os.system`, `eval`, `exec`, `pickle` y `yaml.load` en el alcance — sin resultados
  relevantes.
- `uv run --project . ruff check xtrace_spike tests` — PASS.
- `uv run --project . mypy xtrace_spike` — PASS (30 archivos).
- `uv run --project . ruff check xtrace_crawler tests` — PASS.
- `uv run --project . mypy xtrace_crawler` — PASS (23 archivos).
- Pruebas dirigidas del spike (muestreo, benchmark, CLI, ingest y pipeline) — **69
  passed**.
- Pruebas dirigidas de crawler para REINDEX, allowlist, previews y tipos de asset —
  **16 passed**.
- `pnpm test:db` — PASS: **139 tests**, incluyendo RLS deny-by-default, privilegios
  negativos y esquema de `videos`, `frames`, `sources` y `jobs`.
- Suite crawler amplia ejecutada: **259 passed, 1 failed** por un fallo de entorno de
  la integración histórica `test_stats_rate_limits_section_after_backfill`: el
  `JobsRepo` local no encontró un job residual al completar (`ValueError: job no
  encontrado`). No es una aserción de seguridad ni una ruta nueva de la feature; queda
  registrado para la validación global del orquestador.

## Riesgos y trabajo pendiente

- Completar la validación global de la feature y resolver/reproducir de forma aislada
  el fallo de la integración crawler antes de cerrar CI.
- Ejecutar el benchmark con el corpus autorizado real; esta revisión no cambia el
  default histórico de 30 frames ni autoriza la adopción global del modo adaptativo.

## Veredicto: PASS

**Resultado de la tarea: APPROVED.** No se requiere cambio de seguridad/compliance para
T025.
