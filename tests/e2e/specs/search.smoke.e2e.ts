import { browser, expect } from "@wdio/globals";
import path from "node:path";

/**
 * Smoke E2E de la página `/buscar` (spec 003 · SC-005 · contracts §6).
 *
 * La API no existe en CI: el **stub HTTP real** `tests/e2e/stub-api.mjs` (Node nativo,
 * sin dependencias) escucha en `127.0.0.1:8000` — el mismo default de
 * `NEXT_PUBLIC_XTRACE_API_URL` — y lo arranca `wdio.conf.ts` (`onPrepare`) antes de la
 * suite. El E2E **no intercepta nada**: el navegador habla con el stub por HTTP real
 * (PR-059), eliminando las carreras de la interceptación BiDi anterior ("No blocked
 * request found for network id …"). El contador `GET /__count` del stub verifica que la
 * página llama (o no) a la API.
 *
 * Requisitos cubiertos:
 * - FR-009/FR-010/UX-003: subir imagen → resultados con título, fuente, score, timestamp
 *   y enlace "Ver original" (o `local_ref` sin enlace cuando no hay URL).
 * - UX-002: feedback de carga visible durante la búsqueda (el stub retarda ~1,5 s).
 * - FR-011/UX-001: errores en español — validación de cliente (fichero no imagen, sin
 *   llamar a la API) y error 415 estructurado del stub (contracts §5, mismo mensaje que
 *   `services/api/xtrace_api/media.py`).
 */

const FIXTURE_DIR = path.resolve(process.cwd(), "tests/e2e/fixtures");
const STUB_COUNT_URL = "http://127.0.0.1:8000/__count";

/** Nº de `POST /search` recibidos por el stub (0 = la página no llamó a la API). */
async function stubSearchCalls(): Promise<number> {
  const res = await fetch(STUB_COUNT_URL);
  const body = (await res.json()) as { search_calls: number };
  return body.search_calls;
}

/** Selecciona un fichero en el input y envía el formulario. */
async function selectFileAndSubmit(fileName: string) {
  const input = await browser.$('[data-testid="search-file-input"]');
  await input.setValue(path.join(FIXTURE_DIR, fileName));
  const submit = await browser.$('[data-testid="search-submit"]');
  await submit.click();
}

describe("Buscar por imagen (smoke) — spec 003 · SC-005", () => {
  it("flujo feliz: resultados con título, fuente, score, timestamp y enlace 'Ver original'", async () => {
    const callsBefore = await stubSearchCalls();
    await browser.url("/buscar");
    await expect(await browser.$('[data-testid="buscar-title"]')).toBeDisplayed();

    await selectFileAndSubmit("query.png");

    const results = await browser.$('[data-testid="search-results"]');
    await results.waitForDisplayed({ timeout: 10000 });
    const items = await results.$$("li");
    await expect(items).toBeElementsArrayOfSize(3);

    // Resultado 0: vídeo web con título, fuente, score, timestamp y enlace.
    const first = await browser.$('[data-testid="search-result-0"]');
    await expect(await first.$('[data-testid="search-result-title"]')).toHaveText(
      "Video de ejemplo del corpus",
    );
    await expect(await first.$('[data-testid="search-result-source"]')).toHaveText(
      "www.xvideos.com",
    );
    await expect(await first.$('[data-testid="search-result-score"]')).toHaveText("0.938");
    await expect(await first.$('[data-testid="search-result-timestamp"]')).toHaveText("00:51");
    const link = await first.$('[data-testid="search-result-link"]');
    await expect(link).toHaveText("Ver original");
    await expect(link).toHaveAttribute(
      "href",
      "https://www.xvideos.com/video.abc123/ejemplo",
    );

    // Resultado 1: vídeo local sin `page_url` → `local_ref` sin enlace y timestamp "—".
    const second = await browser.$('[data-testid="search-result-1"]');
    await expect(await second.$('[data-testid="search-result-title"]')).toHaveText(
      "LOCAL 002.mp4",
    );
    await expect(await second.$('[data-testid="search-result-source"]')).toHaveText("—");
    await expect(await second.$('[data-testid="search-result-timestamp"]')).toHaveText("—");
    await expect(await second.$('[data-testid="search-result-local-ref"]')).toHaveText(
      "LOCAL 002.mp4",
    );
    await expect(
      await second.$('[data-testid="search-result-link"]'),
    ).not.toBeExisting();

    // Resultado 2: otro vídeo web con enlace propio.
    const third = await browser.$('[data-testid="search-result-2"]');
    await expect(await third.$('[data-testid="search-result-score"]')).toHaveText("0.705");
    await expect(await third.$('[data-testid="search-result-link"]')).toHaveAttribute(
      "href",
      "https://www.example.com/watch/xyz",
    );

    // La búsqueda llegó al stub por HTTP real (una sola llamada).
    expect(await stubSearchCalls()).toBe(callsBefore + 1);
  });

  it("muestra feedback de carga durante la búsqueda y luego los resultados (UX-002)", async () => {
    await browser.url("/buscar");
    await selectFileAndSubmit("query.png");

    const loading = await browser.$('[data-testid="search-loading"]');
    await loading.waitForDisplayed({ timeout: 3000 });
    await expect(loading).toBeDisplayed();
    await expect(await browser.$('[data-testid="search-submit"]')).toBeDisabled();

    const results = await browser.$('[data-testid="search-results"]');
    await results.waitForDisplayed({ timeout: 5000 });
    await expect(await browser.$('[data-testid="search-loading"]')).not.toBeDisplayed();
  });

  it("rechaza un fichero que no es imagen con error en español y sin llamar a la API", async () => {
    const callsBefore = await stubSearchCalls();
    await browser.url("/buscar");
    await selectFileAndSubmit("query-not-image.txt");

    const validationError = await browser.$('[data-testid="search-validation-error"]');
    await validationError.waitForDisplayed({ timeout: 3000 });
    await expect(validationError).toHaveText(
      "Formato no soportado: solo se aceptan imágenes JPEG, PNG o WebP.",
    );
    await expect(await browser.$('[data-testid="search-loading"]')).not.toBeDisplayed();
    await expect(await browser.$('[data-testid="search-results"]')).not.toBeDisplayed();
    // Validación de cliente: la API no recibió ninguna petición.
    expect(await stubSearchCalls()).toBe(callsBefore);
  });

  it("muestra el error 415 estructurado de la API en español (FR-011 · UX-001)", async () => {
    const callsBefore = await stubSearchCalls();
    await browser.url("/buscar");
    await selectFileAndSubmit("bad.png");

    const errorBox = await browser.$('[data-testid="search-error"]');
    await errorBox.waitForDisplayed({ timeout: 5000 });
    await expect(errorBox).toHaveText(
      "No se pudo completar la búsqueda: la imagen de consulta debe ser JPEG, PNG o WebP (firma por cabecera)",
    );
    // El 415 viene del stub por HTTP real (contracts §5): la petición sí llegó.
    expect(await stubSearchCalls()).toBe(callsBefore + 1);
  });

  it("distingue un timestamp refinado y conserva el enlace del candidato (UX-001)", async () => {
    await browser.url("/buscar");
    await selectFileAndSubmit("refined.png");

    const results = await browser.$('[data-testid="search-results"]');
    await results.waitForDisplayed({ timeout: 10000 });
    const first = await browser.$('[data-testid="search-result-0"]');

    await expect(await first.$('[data-testid="search-result-timestamp"]')).toHaveText("02:34");
    await expect(await first.$('[data-testid="search-result-timestamp-badge"]')).toHaveText(
      "Timestamp refinado (aproximado)",
    );
    await expect(await first.$('[data-testid="search-result-link"]')).toHaveAttribute(
      "href",
      "https://www.xvideos.com/video.abc123/ejemplo",
    );
    await expect(await browser.$('[data-testid="search-refinement-notice"]')).not.toBeExisting();
    await expect(await browser.$('[data-testid="search-error"]')).not.toBeExisting();
  });

  it("muestra el resultado base y disponibilidad limitada si se agota el presupuesto (UX-002, UX-003)", async () => {
    await browser.url("/buscar");
    await selectFileAndSubmit("limited.png");

    const results = await browser.$('[data-testid="search-results"]');
    await results.waitForDisplayed({ timeout: 10000 });
    const first = await browser.$('[data-testid="search-result-0"]');

    await expect(await first.$('[data-testid="search-result-timestamp"]')).toHaveText("00:51");
    await expect(await first.$('[data-testid="search-result-timestamp-badge"]')).toHaveText(
      "Timestamp del índice base (aproximado)",
    );
    await expect(await browser.$('[data-testid="search-refinement-notice"]')).toHaveText(
      "Disponibilidad limitada: se muestran los resultados del índice base y sus timestamps aproximados.",
    );
    await expect(await browser.$('[data-testid="search-error"]')).not.toBeExisting();
  });

  it("mantiene resultados válidos y explica una fuente no disponible (SC-004, SC-008)", async () => {
    await browser.url("/buscar");
    await selectFileAndSubmit("unavailable.png");

    const results = await browser.$('[data-testid="search-results"]');
    await results.waitForDisplayed({ timeout: 10000 });
    const first = await browser.$('[data-testid="search-result-0"]');

    await expect(await first.$('[data-testid="search-result-timestamp"]')).toHaveText("00:51");
    await expect(await first.$('[data-testid="search-result-timestamp-badge"]')).toHaveText(
      "Timestamp del índice base (aproximado)",
    );
    await expect(await browser.$('[data-testid="search-refinement-notice"]')).toHaveText(
      "Disponibilidad limitada: se muestran los resultados del índice base y sus timestamps aproximados.",
    );
    await expect(await browser.$('[data-testid="search-error"]')).not.toBeExisting();
    await expect(await first.$('[data-testid="search-result-link"]')).toHaveText("Ver original");
  });
});
