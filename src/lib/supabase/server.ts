import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";
import { env } from "@/lib/env";
import type { Database } from "@/types/supabase";

/**
 * Cliente Supabase para Server Components / route handlers.
 * Usa cookies para la sesión. Solo clave pública (anon); nunca service_role aquí.
 */
export async function getServerClient() {
  const cookieStore = await cookies();
  return createServerClient<Database>(
    env.NEXT_PUBLIC_SUPABASE_URL,
    env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
    {
      cookies: {
        getAll() {
          return cookieStore.getAll();
        },
        setAll(cookiesToSet) {
          try {
            cookiesToSet.forEach(({ name, value, options }) =>
              cookieStore.set(name, value, options),
            );
          } catch {
            // Llamado desde un Server Component: puede ignorarse si hay middleware
            // refrescando la sesión.
          }
        },
      },
    },
  );
}
