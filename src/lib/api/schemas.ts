import { z } from "zod";

/**
 * Contrato REST de `POST /search` (spec 003 · contracts/README.md §1 · paridad CLI FR-004).
 * El frontend valida la respuesta con zod: frontera estable entre frontend y API.
 *
 * Notas del contrato:
 * - `match_timestamp_ms` puede ser `null` (frame sin timestamp fiable).
 * - `local_ref` puede ser `null` (backend in-memory).
 * - `title`/`page_url` son la extensión MAY (metadatos de visualización): pueden faltar
 *   (`.nullish()`) o ser `null` — el frontend los trata como opcionales.
 */
/**
 * UUID con forma canónica (8-4-4-4-12 hexadecimal) sin exigir versión/variante:
 * el ejemplo del contrato §1 usa UUIDs de versión 0 (`...-0000-0000-...`) que zod v4
 * (`z.string().uuid()`) rechazaría; los ids reales de la API son uuid4 y también pasan.
 */
export const uuidShape = z
  .string()
  .regex(
    /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/,
    "debe ser un UUID",
  );

export const searchResultSchema = z.object({
  video_id: uuidShape,
  local_ref: z.string().nullable(),
  title: z.string().nullish(),
  page_url: z.string().url().nullish(),
  match_score: z.number(),
  matching_frames: z.number().int(),
  match_timestamp_ms: z.number().nullable(),
  evidence: z.object({
    visual: z.number(),
    phash: z.number(),
  }),
});

export const searchResponseSchema = z.object({
  search_id: uuidShape,
  processing_ms: z.number().int(),
  results: z.array(searchResultSchema),
});

export type SearchResult = z.infer<typeof searchResultSchema>;
export type SearchResponse = z.infer<typeof searchResponseSchema>;
