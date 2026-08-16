import { z } from "zod";

/**
 * Validación centralizada de variables de entorno.
 * La app DEBE fallar de forma explícita si falta una variable obligatoria (FR-008).
 * Las variables NEXT_PUBLIC_* son accesibles en cliente; el resto solo en servidor.
 */
const publicSchema = z.object({
  NEXT_PUBLIC_APP_URL: z.string().url().default("http://localhost:3000"),
  NEXT_PUBLIC_SUPABASE_URL: z.string().url(),
  NEXT_PUBLIC_SUPABASE_ANON_KEY: z.string().min(1, "NEXT_PUBLIC_SUPABASE_ANON_KEY requerida"),
  // API de búsqueda XTrace (spec 003 · contracts §6): solo local, sin auth (SEC-001).
  // Default http://127.0.0.1:8000 → build/CI no necesitan env adicional.
  NEXT_PUBLIC_XTRACE_API_URL: z.string().url().default("http://127.0.0.1:8000"),
});

const serverSchema = z.object({
  SUPABASE_SERVICE_ROLE_KEY: z.string().min(1).optional(),
});

function format(error: z.ZodError): string {
  return error.issues.map((i) => `  - ${i.path.join(".")}: ${i.message}`).join("\n");
}

const publicParsed = publicSchema.safeParse({
  NEXT_PUBLIC_APP_URL: process.env.NEXT_PUBLIC_APP_URL,
  NEXT_PUBLIC_SUPABASE_URL: process.env.NEXT_PUBLIC_SUPABASE_URL,
  NEXT_PUBLIC_SUPABASE_ANON_KEY: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
  NEXT_PUBLIC_XTRACE_API_URL: process.env.NEXT_PUBLIC_XTRACE_API_URL,
});

if (!publicParsed.success) {
  throw new Error(
    `Variables de entorno públicas inválidas o ausentes:\n${format(publicParsed.error)}`,
  );
}

export const env = publicParsed.data;

/** Solo debe usarse en código de servidor. */
export function getServerEnv() {
  const parsed = serverSchema.safeParse({
    SUPABASE_SERVICE_ROLE_KEY: process.env.SUPABASE_SERVICE_ROLE_KEY,
  });
  if (!parsed.success) {
    throw new Error(`Variables de entorno de servidor inválidas:\n${format(parsed.error)}`);
  }
  return parsed.data;
}
