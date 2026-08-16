import type { Metadata } from "next";
import BuscarPage from "@/features/search/buscar-page";

export const metadata: Metadata = {
  title: "Buscar — Proyect-skeleton",
  description: "Búsqueda visual por imagen en el índice de XTrace",
};

/**
 * Página `/buscar` (spec 003 · D2 · FR-009): server mínima que delega en el componente
 * cliente. La home y sus tests permanecen intactos.
 */
export default function Buscar() {
  return <BuscarPage />;
}
