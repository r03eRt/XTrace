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
  timestamp_provenance: z
    .object({
      origin: z.enum(["base_index", "refined_asset"]),
      status: z.enum(["improved", "unchanged", "unavailable", "limited", "disabled"]),
      source: z.string().nullish(),
      asset_kind: z.enum(["thumbnail", "storyboard"]).nullish(),
      asset_url: z.string().url().nullish(),
      asset_position: z.number().int().nullish(),
    })
    .nullish(),
});

export const refinementSummarySchema = z
  .object({
    status: z.enum(["completed", "disabled", "unavailable", "limited", "failed"]),
    candidates_requested: z.number().int().nonnegative(),
    candidates_processed: z.number().int().nonnegative(),
    assets_evaluated: z.number().int().nonnegative(),
    assets_discarded: z.number().int().nonnegative(),
    errors_count: z.number().int().nonnegative(),
    bytes_downloaded: z.number().int().nonnegative(),
    embedding_count: z.number().int().nonnegative(),
    embedding_elapsed_ms: z.number().int().nonnegative(),
    improved_results: z.number().int().nonnegative(),
    elapsed_ms: z.number().int().nonnegative(),
  })
  .refine((summary) => summary.candidates_processed <= summary.candidates_requested, {
    message: "candidates_processed no puede superar candidates_requested",
    path: ["candidates_processed"],
  });

export const searchResponseSchema = z.object({
  search_id: uuidShape,
  processing_ms: z.number().int(),
  refinement: refinementSummarySchema.nullish(),
  results: z.array(searchResultSchema),
});

export type SearchResult = z.infer<typeof searchResultSchema>;
export type RefinementSummary = z.infer<typeof refinementSummarySchema>;
export type SearchResponse = z.infer<typeof searchResponseSchema>;
