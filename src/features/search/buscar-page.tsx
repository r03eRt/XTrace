"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  formatMatchTimestamp,
  formatSource,
  searchByImage,
  validateQueryImage,
} from "@/lib/api/xtrace";
import type { SearchResult } from "@/lib/api/schemas";

/**
 * Página de búsqueda por imagen (spec 003 · D2 · contracts §6).
 * Estados: idle → loading → results | error (UX-002); mensajes en español (UX-001).
 * Sin auth y solo local (SEC-001). La media de consulta nunca se persiste ni se loguea
 * desde el frontend (SEC-005); la validación definitiva es en servidor (SEC-002).
 */
type Status = "idle" | "loading" | "results" | "error";

export default function BuscarPage() {
  const [status, setStatus] = useState<Status>("idle");
  const [file, setFile] = useState<File | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [results, setResults] = useState<SearchResult[]>([]);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // Al desmontar se aborta la búsqueda en curso: sin estados colgados (edge case spec).
  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const handleFiles = (files: FileList | null) => {
    const selected = files?.[0];
    if (!selected) return;
    abortRef.current?.abort(); // cancela una búsqueda en curso (edge case spec)
    setFile(selected);
    setValidationError(null);
    setErrorMessage(null);
    setResults([]);
    setStatus("idle");
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!file) {
      setValidationError("Selecciona una imagen para buscar.");
      return;
    }
    const clientError = validateQueryImage(file);
    if (clientError) {
      setValidationError(clientError);
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setValidationError(null);
    setErrorMessage(null);
    setStatus("loading");
    try {
      const response = await searchByImage(file, { signal: controller.signal });
      setResults(response.results);
      setStatus("results");
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        setStatus("idle"); // cancelada por el usuario: sin estados colgados
        return;
      }
      setErrorMessage(err instanceof Error ? err.message : "Error inesperado al buscar.");
      setStatus("error");
    }
  };

  return (
    <main
      data-testid="buscar-page"
      style={{
        display: "flex",
        minHeight: "100vh",
        flexDirection: "column",
        alignItems: "center",
        gap: "1rem",
        padding: "2rem 1rem",
        fontFamily: "system-ui, sans-serif",
      }}
    >
      <h1 data-testid="buscar-title" style={{ margin: 0 }}>
        Buscar por imagen
      </h1>
      <p style={{ margin: 0, textAlign: "center", color: "#555" }}>
        Sube una captura para encontrar vídeos similares en el índice.
      </p>

      <form
        onSubmit={handleSubmit}
        data-testid="search-form"
        style={{
          display: "flex",
          width: "100%",
          maxWidth: 480,
          flexDirection: "column",
          gap: "0.75rem",
        }}
      >
        <label
          htmlFor="search-file-input"
          data-testid="search-dropzone"
          onDragOver={(e) => {
            e.preventDefault();
            setDragActive(true);
          }}
          onDragLeave={() => setDragActive(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragActive(false);
            handleFiles(e.dataTransfer.files);
          }}
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: "0.5rem",
            padding: "2rem 1rem",
            border: dragActive ? "2px dashed #1a73e8" : "2px dashed #8a8a8a",
            borderRadius: 8,
            cursor: "pointer",
            background: dragActive ? "#eef4ff" : "#fafafa",
            textAlign: "center",
          }}
        >
          <span>Arrastra una imagen aquí o haz clic para seleccionarla</span>
          <span style={{ fontSize: "0.85rem", color: "#666" }}>JPEG, PNG o WebP · máx. 10 MB</span>
        </label>
        <input
          id="search-file-input"
          data-testid="search-file-input"
          type="file"
          accept="image/jpeg,image/png,image/webp"
          onChange={(e) => handleFiles(e.target.files)}
          style={{
            position: "absolute",
            width: 1,
            height: 1,
            opacity: 0,
            overflow: "hidden",
          }}
        />
        {file && (
          <p
            data-testid="search-file-name"
            style={{ margin: 0, fontSize: "0.85rem", color: "#444" }}
          >
            Fichero: {file.name}
          </p>
        )}
        {validationError && (
          <p
            data-testid="search-validation-error"
            role="alert"
            style={{ margin: 0, color: "#b3261e" }}
          >
            {validationError}
          </p>
        )}
        <button
          type="submit"
          data-testid="search-submit"
          disabled={status === "loading"}
          style={{
            padding: "0.6rem 1rem",
            borderRadius: 6,
            border: "1px solid #1a73e8",
            background: status === "loading" ? "#b9cdf2" : "#1a73e8",
            color: "#fff",
            fontSize: "1rem",
            cursor: status === "loading" ? "wait" : "pointer",
          }}
        >
          {status === "loading" ? "Buscando…" : "Buscar"}
        </button>
      </form>

      {status === "loading" && (
        <p data-testid="search-loading" role="status" aria-live="polite">
          Buscando resultados, esto puede tardar unos segundos…
        </p>
      )}

      {status === "error" && errorMessage && (
        <p data-testid="search-error" role="alert" style={{ margin: 0, color: "#b3261e" }}>
          No se pudo completar la búsqueda: {errorMessage}
        </p>
      )}

      {status === "results" &&
        (results.length === 0 ? (
          <p data-testid="search-empty" style={{ margin: 0 }}>
            No se encontraron resultados para esta imagen.
          </p>
        ) : (
          <section aria-label="Resultados de la búsqueda" style={{ width: "100%", maxWidth: 640 }}>
            <h2 style={{ margin: "0 0 0.5rem" }}>Resultados</h2>
            <ol
              data-testid="search-results"
              style={{
                listStyle: "none",
                padding: 0,
                margin: 0,
                display: "flex",
                flexDirection: "column",
                gap: "0.75rem",
              }}
            >
              {results.map((result, index) => (
                <li
                  key={result.video_id}
                  data-testid={`search-result-${index}`}
                  style={{
                    border: "1px solid #ddd",
                    borderRadius: 8,
                    padding: "0.75rem 1rem",
                    background: "#fff",
                  }}
                >
                  <h3
                    data-testid="search-result-title"
                    style={{ margin: "0 0 0.25rem", fontSize: "1rem" }}
                  >
                    {result.title ?? result.local_ref ?? "Vídeo sin título"}
                  </h3>
                  <p style={{ margin: 0, fontSize: "0.85rem", color: "#555" }}>
                    Fuente:{" "}
                    <span data-testid="search-result-source">{formatSource(result.page_url)}</span>
                  </p>
                  <p style={{ margin: 0, fontSize: "0.85rem", color: "#555" }}>
                    Puntuación:{" "}
                    <span data-testid="search-result-score">{result.match_score.toFixed(3)}</span>
                  </p>
                  <p style={{ margin: 0, fontSize: "0.85rem", color: "#555" }}>
                    Marca de tiempo:{" "}
                    <span data-testid="search-result-timestamp">
                      {formatMatchTimestamp(result.match_timestamp_ms)}
                    </span>
                  </p>
                  {result.page_url ? (
                    <a
                      href={result.page_url}
                      target="_blank"
                      rel="noreferrer"
                      data-testid="search-result-link"
                    >
                      Ver original
                    </a>
                  ) : (
                    <span
                      data-testid="search-result-local-ref"
                      style={{ fontSize: "0.85rem", color: "#555" }}
                    >
                      {result.local_ref ?? "—"}
                    </span>
                  )}
                </li>
              ))}
            </ol>
          </section>
        ))}
    </main>
  );
}
