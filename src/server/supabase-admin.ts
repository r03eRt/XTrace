import "server-only";
import { createClient } from "@supabase/supabase-js";
import { env } from "@/lib/env";
import { getServerEnv } from "@/lib/env";
import type { Database } from "@/types/supabase";

/**
 * Cliente administrativo con privilegios service_role.
 * SEC-002: SOLO en código exclusivamente de servidor. Nunca exponer al cliente.
 * Salta RLS: úsalo con extremo cuidado y validando permisos en servidor.
 */
export function getAdminClient() {
  const { SUPABASE_SERVICE_ROLE_KEY } = getServerEnv();
  if (!SUPABASE_SERVICE_ROLE_KEY) {
    throw new Error("SUPABASE_SERVICE_ROLE_KEY es obligatoria para el cliente admin.");
  }
  return createClient<Database>(env.NEXT_PUBLIC_SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, {
    auth: { autoRefreshToken: false, persistSession: false },
  });
}
