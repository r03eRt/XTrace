/**
 * Tipos generados desde el esquema de Supabase (DATA-002).
 * Regenera con: `pnpm supabase:types`.
 * Este es un placeholder hasta la primera generación con Supabase local en marcha.
 */
export type Json = string | number | boolean | null | { [key: string]: Json } | Json[];

export interface Database {
  public: {
    Tables: {
      health_check: {
        Row: { id: string; note: string | null; created_at: string };
        Insert: { id?: string; note?: string | null; created_at?: string };
        Update: { id?: string; note?: string | null; created_at?: string };
        Relationships: [];
      };
    };
    Views: Record<string, never>;
    Functions: Record<string, never>;
    Enums: Record<string, never>;
    CompositeTypes: Record<string, never>;
  };
}
