import { searchResponseSchema } from "./schemas";
import type { SearchResponse } from "./schemas";

/**
 * Cliente de la API de búsqueda de XTrace (spec 003 · contracts/README.md §6).
 * Solo cliente, sin auth (SEC-001/D3). Base URL por env con default local.
 */

/**
 * Base URL de la API (spec 003 · contracts §6 · SEC-001): se lee
 * `NEXT_PUBLIC_XTRACE_API_URL` directamente, SIN pasar por `@/lib/env` (PR-060).
 * `env.ts` exige `NEXT_PUBLIC_SUPABASE_URL`/`NEXT_PUBLIC_SUPABASE_ANON_KEY` del
 * skeleton al evaluarse, y el job de calidad de CI no las define: el build de Next
 * fallaba al prerenderizar `/buscar`. La búsqueda no necesita Supabase; si la env
 * existe pero es inválida se lanza un error claro (y solo entonces).
 */
function resolveApiBaseUrl(): string {
  const raw = process.env.NEXT_PUBLIC_XTRACE_API_URL ?? "http://127.0.0.1:8000";
  try {
    new URL(raw);
  } catch {
    throw new Error(
      `NEXT_PUBLIC_XTRACE_API_URL inválida ("${raw}"): debe ser una URL absoluta válida. ` +
        "Sin la variable se usa el default http://127.0.0.1:8000 (solo local, SEC-001).",
    );
  }
  return raw;
}

const API_BASE_URL = resolveApiBaseUrl();

export const MAX_QUERY_IMAGE_BYTES = 10 * 1024 * 1024; // 10 MB (FR-002, contracts §1)
export const SEARCH_TIMEOUT_MS = 60_000; // contracts §6: timeout de 60 s con abort
export const SUPPORTED_IMAGE_TYPES: readonly string[] = ["image/jpeg", "image/png", "image/webp"];

/** Validación de cliente (UX): tipo y tamaño. La validación definitiva es en servidor (SEC-002). */
export function validateQueryImage(file: File): string | null {
  if (!SUPPORTED_IMAGE_TYPES.includes(file.type)) {
    return "Formato no soportado: solo se aceptan imágenes JPEG, PNG o WebP.";
  }
  if (file.size > MAX_QUERY_IMAGE_BYTES) {
    return "La imagen supera el límite de 10 MB.";
  }
  return null;
}

/** Error de la API de búsqueda con estado HTTP y `error_type` estables (contracts §5). */
export class SearchApiError extends Error {
  readonly status?: number;
  readonly errorType?: string;

  constructor(message: string, status?: number, errorType?: string) {
    super(message);
    this.name = "SearchApiError";
    this.status = status;
    this.errorType = errorType;
  }
}

/** `mm:ss` desde milisegundos, o "—" si no hay timestamp fiable (contracts §6, UX-003). */
export function formatMatchTimestamp(ms: number | null): string {
  if (ms === null || Number.isNaN(ms) || ms < 0) return "—";
  const totalSeconds = Math.floor(ms / 1000);
  const mm = Math.floor(totalSeconds / 60);
  const ss = totalSeconds % 60;
  return `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
}

/** Fuente mostrada: dominio de `page_url`, o "—" si no hay URL (contracts §6, UX-003). */
export function formatSource(pageUrl: string | null | undefined): string {
  if (!pageUrl) return "—";
  try {
    return new URL(pageUrl).hostname;
  } catch {
    return "—";
  }
}

/**
 * Búsqueda por imagen: multipart → `POST {NEXT_PUBLIC_XTRACE_API_URL}/search`.
 * - Timeout de 60 s vía AbortController interno; cancelación externa vía `options.signal`.
 * - Respuesta validada con zod contra el contrato §1 (FR-004).
 * - Errores: mensajes en español (UX-001); los 4xx/5xx estructurados de la API
 *   (`error`/`error_type`, contracts §5) se propagan tal cual.
 */
export async function searchByImage(
  file: File,
  options: { signal?: AbortSignal } = {},
): Promise<SearchResponse> {
  const form = new FormData();
  form.append("image", file);
  form.append("top_k", "10");
  form.append("min_score", "0.0");

  const controller = new AbortController();
  const timedOut = { current: false };
  const timeoutId = setTimeout(() => {
    timedOut.current = true;
    controller.abort();
  }, SEARCH_TIMEOUT_MS);
  const onExternalAbort = () => controller.abort();
  options.signal?.addEventListener("abort", onExternalAbort, { once: true });

  try {
    let res: Response;
    try {
      res = await fetch(`${API_BASE_URL}/search`, {
        method: "POST",
        body: form,
        signal: controller.signal,
      });
    } catch (err) {
      if (timedOut.current) {
        throw new SearchApiError(
          "La búsqueda tardó demasiado. Inténtalo de nuevo.",
          undefined,
          "timeout",
        );
      }
      if (controller.signal.aborted) {
        // Cancelación externa: se propaga el AbortError para que la UI no deje estados colgados.
        throw err;
      }
      throw new SearchApiError(
        "No se pudo conectar con el servicio de búsqueda. Verifica que la API esté en marcha.",
        undefined,
        "network_error",
      );
    }

    if (!res) {
      throw new SearchApiError(
        "No se pudo conectar con el servicio de búsqueda. Verifica que la API esté en marcha.",
        undefined,
        "network_error",
      );
    }
    if (!res.ok) {
      throw await toApiError(res);
    }

    const json: unknown = await res.json();
    const parsed = searchResponseSchema.safeParse(json);
    if (!parsed.success) {
      throw new SearchApiError(
        "La respuesta del servicio no es válida. Inténtalo de nuevo.",
        res.status,
        "invalid_response",
      );
    }
    return parsed.data;
  } finally {
    clearTimeout(timeoutId);
    options.signal?.removeEventListener("abort", onExternalAbort);
  }
}

/** Convierte una respuesta HTTP no-2xx en SearchApiError con el mensaje en español (contracts §5). */
async function toApiError(res: Response): Promise<SearchApiError> {
  try {
    const body = (await res.json()) as { error?: unknown; error_type?: unknown };
    if (typeof body.error === "string" && body.error.length > 0) {
      return new SearchApiError(
        body.error,
        res.status,
        typeof body.error_type === "string" ? body.error_type : undefined,
      );
    }
  } catch {
    // Cuerpo no JSON: se usa el mensaje genérico.
  }
  return new SearchApiError(`Error del servicio de búsqueda (HTTP ${res.status}).`, res.status);
}
