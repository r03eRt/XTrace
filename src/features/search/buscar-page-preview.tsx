"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  formatMatchTimestamp,
  formatSource,
  searchByImage,
  validateQueryImage,
} from "@/lib/api/xtrace";
import type { RefinementSummary, SearchResult } from "@/lib/api/schemas";
import styles from "./buscar-page-preview.module.css";

/**
 * Spike de UI (no forma parte de spec 003): explora un layout tipo grid/dark-theme
 * inspirado en buscadores visuales del sector, reutilizando el mismo cliente/contrato
 * de `@/lib/api/xtrace`. No sustituye a `buscar-page.tsx`; pendiente de spec/aprobación
 * antes de promoverlo a la ruta principal.
 */
type Status = "idle" | "loading" | "results" | "error";

export default function BuscarPagePreview() {
  const [status, setStatus] = useState<Status>("idle");
  const [file, setFile] = useState<File | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [results, setResults] = useState<SearchResult[]>([]);
  const [refinement, setRefinement] = useState<RefinementSummary | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const handleFiles = (files: FileList | null) => {
    const selected = files?.[0];
    if (!selected) return;
    abortRef.current?.abort();
    setFile(selected);
    setValidationError(null);
    setErrorMessage(null);
    setResults([]);
    setRefinement(null);
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
      setRefinement(response.refinement ?? null);
      setStatus("results");
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        setStatus("idle");
        return;
      }
      setErrorMessage(err instanceof Error ? err.message : "Error inesperado al buscar.");
      setRefinement(null);
      setStatus("error");
    }
  };

  const showLimitedNotice =
    status === "results" &&
    refinement !== null &&
    ["limited", "unavailable", "failed"].includes(refinement.status);

  return (
    <main className={styles.page} data-testid="buscar-preview-page">
      <header className={styles.header}>
        <div className={styles.logo}>
          <span className={styles.logoMark} />
          XTrace
        </div>
        <nav className={styles.navLinks}>
          <a href="/">Inicio</a>
          <a href="/buscar">Búsqueda clásica</a>
        </nav>
      </header>

      <section className={styles.hero}>
        <h1 className={styles.heroTitle}>Encuentra el vídeo de origen</h1>
        <p className={styles.heroSubtitle}>
          Sube una captura, un frame o un clip corto. XTrace lo compara contra el índice visual y
          localiza el vídeo, la fuente y un timestamp aproximado de la escena.
        </p>

        <form onSubmit={handleSubmit} className={styles.searchShell}>
          <label
            htmlFor="preview-search-file-input"
            className={`${styles.dropzone} ${dragActive ? styles.dropzoneActive : ""}`}
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
          >
            <input
              id="preview-search-file-input"
              className={styles.fileInput}
              type="file"
              accept="image/jpeg,image/png,image/webp"
              onChange={(e) => handleFiles(e.target.files)}
            />
            <span className={styles.uploadButton}>Subir imagen</span>
            <span className={`${styles.searchText} ${file ? styles.searchTextActive : ""}`}>
              {file ? file.name : "Arrastra una imagen o haz clic para seleccionarla…"}
            </span>
            <button type="submit" className={styles.submitButton} disabled={status === "loading"}>
              {status === "loading" ? "Buscando…" : "Buscar"}
            </button>
          </label>
          <p className={styles.hint}>JPEG, PNG o WebP · máx. 10 MB</p>
          {validationError && (
            <p className={`${styles.hint} ${styles.errorStatus}`}>{validationError}</p>
          )}
        </form>
      </section>

      {status === "loading" && (
        <p className={styles.status} role="status" aria-live="polite">
          Buscando resultados, esto puede tardar unos segundos…
        </p>
      )}

      {status === "error" && errorMessage && (
        <p className={`${styles.status} ${styles.errorStatus}`} role="alert">
          No se pudo completar la búsqueda: {errorMessage}
        </p>
      )}

      {showLimitedNotice && (
        <p className={styles.noticeBar} role="status" aria-live="polite">
          Disponibilidad limitada: se muestran los resultados del índice base y sus timestamps
          aproximados.
        </p>
      )}

      {status === "results" && (
        <section className={styles.resultsSection} aria-label="Resultados de la búsqueda">
          <h2 className={styles.resultsHeading}>
            Resultados
            <span className={styles.resultsCount}>{results.length}</span>
          </h2>

          {results.length === 0 ? (
            <p className={styles.empty}>No se encontraron resultados para esta imagen.</p>
          ) : (
            <ul className={styles.grid}>
              {results.map((result) => {
                const provenance = result.timestamp_provenance;
                const thumbUrl =
                  provenance?.origin === "refined_asset" ? (provenance.asset_url ?? null) : null;
                return (
                  <li key={result.video_id}>
                    <a
                      className={styles.card}
                      href={result.page_url ?? undefined}
                      target={result.page_url ? "_blank" : undefined}
                      rel={result.page_url ? "noreferrer" : undefined}
                    >
                      <div className={styles.thumb}>
                        {thumbUrl ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={thumbUrl} alt="" className={styles.thumbImage} />
                        ) : (
                          <span>Sin miniatura</span>
                        )}
                        {provenance?.origin === "refined_asset" &&
                          provenance.status === "improved" && (
                            <span
                              className={`${styles.provenanceBadge} ${styles.provenanceRefined}`}
                            >
                              Refinado
                            </span>
                          )}
                        {provenance?.origin === "base_index" && (
                          <span className={`${styles.provenanceBadge} ${styles.provenanceBase}`}>
                            Índice base
                          </span>
                        )}
                        <span className={styles.badge}>
                          {formatMatchTimestamp(result.match_timestamp_ms)}
                        </span>
                      </div>
                      <div className={styles.cardBody}>
                        <p className={styles.cardTitle}>
                          {result.title ?? result.local_ref ?? "Vídeo sin título"}
                        </p>
                        <div className={styles.cardMeta}>
                          <span>{formatSource(result.page_url)}</span>
                          <span>{result.match_score.toFixed(3)}</span>
                        </div>
                      </div>
                    </a>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      )}
    </main>
  );
}
