# Contracts — MVP de Búsqueda (API REST + Frontend)

Contratos estables que los implementadores deben respetar. Cambios a estos contratos
requieren actualizar la spec/plan primero (constitución §1).

Base URL local: `http://127.0.0.1:8000` (default; coincide con el default de
`NEXT_PUBLIC_XTRACE_API_URL` del frontend). Sin auth (D3). La API **solo** escucha en
`127.0.0.1` (SEC-001).

## 1. API REST — `POST /search` (FR-001..005)

`multipart/form-data` con la parte de fichero **`image`** (JPEG/PNG/WebP, ≤ 10 MB) y campos
de formulario opcionales `top_k` (int, default `10`) y `min_score` (float, default `0.0`) —
mismos defaults que la CLI `search` (contracts spike §1).

**Request** (curl):

```bash
curl -F "image=@captura.png" -F "top_k=10" -F "min_score=0.0" \
  http://127.0.0.1:8000/search
```

**Response 200** — reutiliza el JSON de la CLI `search` (spec 001 contracts §1, FR-004);
los campos `title` y `page_url` son la **extensión MAY** de metadatos de visualización
(nullables) y no alteran los campos existentes:

```json
{
  "search_id": "3f2a1c4e-8b6d-4f2e-9a1c-0e5d7b9a2c11",
  "processing_ms": 4123,
  "results": [
    {
      "video_id": "1a2b3c4d-0000-0000-0000-000000000001",
      "local_ref": "MAYO 2026 (386).mp4",
      "title": "Video de ejemplo del corpus",
      "page_url": "https://www.xvideos.com/video.abc123/ejemplo",
      "match_score": 0.938,
      "matching_frames": 2,
      "match_timestamp_ms": 51000,
      "evidence": { "visual": 0.95, "phash": 0.84 }
    }
  ]
}
```

- `search_id` es un UUID **único por búsqueda** (búsquedas concurrentes → ids
  independientes; es el `id` de la fila en `searches`).
- `match_timestamp_ms` puede ser `null` (frame sin timestamp fiable, paridad FR-012 spike).
- `local_ref` puede ser `null` (backend in-memory, paridad CLI).
- `title`/`page_url` **pueden ser `null`** (vídeos locales solo con `local_ref`).
- **Sin resultados** por encima del umbral de match → `200` con `"results": []` (no es un
  error).
- Los vídeos excluidos (`excluded=true`) nunca aparecen (paridad FR-014 spike).

## 2. API REST — `GET /health` (FR-006)

```json
{ "status": "ok", "service": "xtrace-api", "version": "0.1.0" }
```

Responde `200` siempre que el proceso vive; **no** depende de la BD.

## 3. API REST — `GET /stats` (FR-007)

Mismos campos que la CLI `stats` (coherencia FR-007):

```json
{
  "videos": 147,
  "frames": 3480,
  "vectors": 3480,
  "backend": "postgres",
  "embedding_provider": "ViT-B-16-SigLIP"
}
```

- `backend`: `"postgres"` (índice real) o `"in-memory"` (modo tests/dev sin `SUPABASE_DB_URL`).
- `embedding_provider`: `model_id` del proveedor activo (`fake` o SigLIP).

## 4. API REST — `GET /videos/{id}` (FR-008)

Ficha del vídeo con metadatos, fuente y enlace original. `id` es un UUID.

**Response 200**:

```json
{
  "video_id": "1a2b3c4d-0000-0000-0000-000000000001",
  "local_ref": "MAYO 2026 (386).mp4",
  "title": "Video de ejemplo del corpus",
  "page_url": "https://www.xvideos.com/video.abc123/ejemplo",
  "source": "xvideos",
  "status": "indexed",
  "duration_ms": 483000,
  "frame_count": 30,
  "tags": ["buttfucking"],
  "published_at": "2026-08-10T12:00:00Z",
  "thumbnail_url": "https://thumbs.example.com/t.jpg",
  "excluded": false
}
```

- Campos nullables: `title`, `page_url`, `source` (vídeos locales sin fuente),
  `duration_ms`, `tags`, `published_at`, `thumbnail_url`.
- `source` es el nombre de la fuente (`sources.name`, join existente) — `null` para el
  dataset local del spike.
- **404** si el `id` no existe (con cuerpo de error); **400** si `id` no es un UUID válido.

## 5. Errores estructurados (FR-011, UX-001)

Cuerpo siempre:

```json
{ "error": "la imagen de consulta supera el límite de 10 MB", "error_type": "media_too_large" }
```

| Código | Condición | `error_type` |
| --- | --- | --- |
| `400` | petición sin parte `image`, nombre de fichero vacío | `missing_file_part` |
| `400` | media con firma válida pero contenido corrupto/ilegible | `media_corrupt` |
| `400` | `id` de `/videos/{id}` no es UUID | `invalid_uuid` |
| `404` | vídeo inexistente en `/videos/{id}` | `video_not_found` |
| `413` | media > 10 MB (sin procesar) | `media_too_large` |
| `415` | firma MIME no soportada (no JPEG/PNG/WebP) | `media_type_not_supported` |
| `503` | índice/BD no disponible | `index_unavailable` |
| `500` | fallo interno | `internal_error` |

- Los mensajes van en **español** (idioma del frontend, UX-001).
- Los errores 4xx de validación ocurren **sin ejecutar la búsqueda** (SC-006).
- `error_type` es estable para consumo programático (el frontend puede mostrarlo o mapear a
  un mensaje propio).

## 6. Contrato del frontend (D2, FR-009/010, UX-001..003)

**Página**: `/buscar` en el skeleton Next.js (`src/app/buscar/page.tsx` + componente cliente
en `src/features/search/`). La home actual no se toca.

**Llamada a la API** (solo cliente; sin auth):

```ts
const url = `${env.NEXT_PUBLIC_XTRACE_API_URL}/search`; // default http://127.0.0.1:8000
const form = new FormData();
form.append("image", file);
form.append("top_k", "10");
form.append("min_score", "0.0");
const res = await fetch(url, { method: "POST", body: form, signal: AbortSignal.timeout(60_000) });
```

- `NEXT_PUBLIC_XTRACE_API_URL` se añade a `src/lib/env.ts` (zod) con **default**
  `http://127.0.0.1:8000` (el build/CI no necesita env adicional).
- La respuesta se valida con **zod** (`src/lib/api/schemas.ts`) contra el contrato §1
  (paridad FR-004 como frontera estable).
- **Estados** de la página: `idle` (selector de fichero) → `loading` (feedback visible,
  UX-002) → `results` | `error`. Un 5xx o timeout muestra el error en español sin quedarse
  colgado; la cancelación de la subida no deja estados colgados (edge cases spec).

**Render de resultados** (UX-003): ordenados por `match_score` descendente (el API ya los
devuelve ordenados; el frontend no reordena); por resultado: título (o `local_ref`),
fuente (dominio de `page_url` o nombre de `source`), `match_score` (formato 0.000) y
`match_timestamp_ms` (formato `mm:ss` o `—` si `null`); **enlace "Ver original"** a
`page_url` cuando exista; si no, se muestra `local_ref` **sin enlace** y sin fallar.

**Testabilidad sin API real (SC-005)**: toda la lógica de red de la página va por `fetch`
estándar, de modo que el E2E WebdriverIO la intercepta con `browser.mock('**/search',
{ method: "POST" })` y responde el fixture `tests/e2e/fixtures/search-response.json` (y un
fixture 4xx para el caso de error). No se necesita API real en CI.

## 7. Invariantes

1. **La media de consulta nunca se persiste** (SEC-005, ASSUMPTION-6): tras cada búsqueda
   (éxito o error) ya no existe fichero en disco ni temporales (SC-003); la media rechazada
   por validación tampoco deja restos (en la API el fichero es del sistema, a diferencia de
   la CLI donde el original del operador no se toca).
2. **Paridad con la CLI** (FR-004/005, SC-001): `/search` devuelve los **mismos vídeos, en
   el mismo orden y con los mismos `match_score`** que `xtrace-spike search` para la misma
   imagen contra el mismo índice y configuración (`top_k`, `min_score`, proveedor, backend).
   Procedimiento de verificación: ejecutar ambas vías sobre el mismo índice y comparar los
   JSON (campos CLI; ignorar la extensión `title`/`page_url`). Test automatizado:
   `tests/integration/test_parity_cli_api.py` con ≥ 5 imágenes.
3. **Sin auth y solo local** (SEC-001, D3): la API escucha en `127.0.0.1`; no hay tokens,
   cookies ni cabeceras de autorización.
4. **RLS deny-by-default intacta** (SEC-004): la API accede a la BD con credenciales de
   servidor (`SUPABASE_DB_URL`, service-side); no se añaden políticas ni grants; el frontend
   nunca toca la BD.
5. **Sin migraciones ni tablas nuevas** (DATA-001): `searches`, `videos` y `frames` se
   usan tal cual; el TTL de `searches` es cleanup por `created_at` configurable por env.
6. **Errores en español** (UX-001) y **analítica sin media** (FR-012): cada búsqueda
   aceptada inserta una fila en `searches` (`id = search_id`, `search_type='image'`,
   `processing_ms`, `results_count`); nada más se registra.

## 8. Frontera con el spike (ADR-0011/0012)

- La API **importa** `xtrace_spike` (dep editable) y reutiliza: `ImageSearch`,
  `rank_candidates`, `validate_query_image`/`QueryMediaContext` (security.py),
  `PgVectorStore`, `PgRepo`, `FakeEmbeddingProvider`/`SiglipLocalProvider` — **sin
  modificarlo**.
- El spike **no** se modifica; cualquier cambio necesario en él es un PR propio trazado a
  esta spec (frontera documentada en ADR-0011 §Consecuencias).
- La CLI sigue siendo la vía de validación/benchmark; la API es el **mismo pipeline con
  transporte HTTP** (paridad por construcción).
