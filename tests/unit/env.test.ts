import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

/**
 * Valida FR-008: la app falla de forma explícita si falta una variable obligatoria.
 * Importamos el módulo de forma dinámica tras ajustar el entorno.
 */
describe("env validation (FR-008)", () => {
  const OLD = { ...process.env };

  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    process.env = { ...OLD };
  });

  it("carga cuando las variables públicas obligatorias están presentes", async () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL = "http://127.0.0.1:54321";
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY = "anon-key";
    const mod = await import("@/lib/env");
    expect(mod.env.NEXT_PUBLIC_SUPABASE_URL).toBe("http://127.0.0.1:54321");
  });

  it("lanza error explícito si falta NEXT_PUBLIC_SUPABASE_ANON_KEY", async () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL = "http://127.0.0.1:54321";
    delete process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
    await expect(import("@/lib/env")).rejects.toThrow(/NEXT_PUBLIC_SUPABASE_ANON_KEY/);
  });
});
