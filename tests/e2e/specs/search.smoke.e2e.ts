import { browser, expect } from "@wdio/globals";
import fs from "node:fs";
import path from "node:path";

/**
 * Smoke E2E de la página `/buscar` (spec 003 · SC-005 · contracts §6).
 *
 * La API no existe en CI: se **stubbea la llamada** `POST {NEXT_PUBLIC_XTRACE_API_URL}/search`
 * interceptándola en el nivel de red (WebDriver BiDi) y respondiendo los fixtures de
 * `tests/e2e/fixtures/` (contrato §1). No se necesita API real (SC-005).
 *
 * Requisitos cubiertos:
 * - FR-009/FR-010/UX-003: subir imagen → resultados con título, fuente, score, timestamp
 *   y enlace "Ver original" (o `local_ref` sin enlace cuando no hay URL).
 * - UX-002: feedback de carga visible durante la búsqueda.
 * - FR-011/UX-001: errores en español — validación de cliente (fichero no imagen, sin
 *   llamar a la API) y error 415 estructurado de la API (contracts §5).
 *
 * Nota: se usa interceptación BiDi directa (`network.provideResponse` en la fase
 * `beforeRequestSent`) en lugar de `browser.mock`: el mock de WebdriverIO v9 solo
 * sustituye la respuesta en la fase `responseStarted`, que requiere que un servidor real
 * empiece a responder — imposible en CI sin API. La respuesta proporcionada en
 * `beforeRequestSent` nunca sale a la red y admite retardo determinista (UX-002).
 */

const FIXTURE_DIR = path.resolve(process.cwd(), "tests/e2e/fixtures");
const SEARCH_PATHNAME = "/search";

interface BeforeRequestSentEvent {
  request: {
    /** id de la petición (BiDi `network.request`) */
    request: string;
    url: string;
    method: string;
  };
}

interface SearchStubOptions {
  statusCode: number;
  /** cuerpo JSON de la respuesta (contrato §1 o error §5) */
  body: unknown;
  /** retardo antes de responder; permite observar el estado de carga (UX-002) */
  delayMs?: number;
}

interface SearchStub {
  /** nº de peticiones `POST /search` interceptadas (0 = no se llamó a la API) */
  calls: number;
  dispose: () => Promise<void>;
}

/**
 * Stub de red de `POST <API_URL>/search` vía WebDriver BiDi (contracts §6 · SC-005).
 * Responde con cabeceras CORS mínimas para que el `fetch` del frontend pueda leerla.
 */
async function stubSearchApi(options: SearchStubOptions): Promise<SearchStub> {
  await browser.sessionSubscribe({ events: ["network.beforeRequestSent"] });
  const added = await browser.networkAddIntercept({
    phases: ["beforeRequestSent"],
    urlPatterns: [{ type: "pattern", pathname: SEARCH_PATHNAME }],
  });

  let calls = 0;
  const onBeforeRequestSent = async (event: BeforeRequestSentEvent) => {
    const req = event.request;
    if (req.method !== "POST" || !req.url.endsWith(SEARCH_PATHNAME)) {
      await browser.networkContinueRequest({ request: req.request });
      return;
    }
    calls += 1;
    try {
      if (options.delayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.delayMs));
      }
      await browser.networkProvideResponse({
        request: req.request,
        statusCode: options.statusCode,
        headers: [
          { name: "access-control-allow-origin", value: { type: "string", value: "*" } },
          { name: "content-type", value: { type: "string", value: "application/json" } },
        ],
        body: {
          type: "base64",
          value: Buffer.from(JSON.stringify(options.body)).toString("base64"),
        },
      });
    } catch (err) {
      // Nunca dejar la petición colgada: se falla a nivel de red y el frontend muestra error.
      console.error("stubSearchApi: error al responder la petición", err);
      await browser.networkFailRequest({ request: req.request }).catch(() => undefined);
    }
  };
  browser.on("network.beforeRequestSent", onBeforeRequestSent);

  return {
    get calls() {
      return calls;
    },
    async dispose() {
      browser.off("network.beforeRequestSent", onBeforeRequestSent);
      await browser.networkRemoveIntercept({ intercept: added.intercept });
    },
  };
}

/** Fixture del contrato §1 (respuesta 200 de `POST /search`). */
function searchResponseFixture(): unknown {
  return JSON.parse(
    fs.readFileSync(path.join(FIXTURE_DIR, "search-response.json"), "utf8"),
  );
}

/** Error estructurado 415 del contracts §5 (mensaje real de la API, UX-001). */
const mediaTypeNotSupportedFixture = {
  error: "la imagen de consulta debe ser JPEG, PNG o WebP (firma por cabecera)",
  error_type: "media_type_not_supported",
};

/** Selecciona un fichero en el input y envía el formulario. */
async function selectFileAndSubmit(fileName: string) {
  const input = await browser.$('[data-testid="search-file-input"]');
  await input.setValue(path.join(FIXTURE_DIR, fileName));
  const submit = await browser.$('[data-testid="search-submit"]');
  await submit.click();
}

describe("Buscar por imagen (smoke) — spec 003 · SC-005", () => {
  it("flujo feliz: resultados con título, fuente, score, timestamp y enlace 'Ver original'", async () => {
    const stub = await stubSearchApi({ statusCode: 200, body: searchResponseFixture() });
    try {
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

      expect(stub.calls).toBe(1);
    } finally {
      await stub.dispose();
    }
  });

  it("muestra feedback de carga durante la búsqueda y luego los resultados (UX-002)", async () => {
    const stub = await stubSearchApi({
      statusCode: 200,
      body: searchResponseFixture(),
      delayMs: 1500,
    });
    try {
      await browser.url("/buscar");
      await selectFileAndSubmit("query.png");

      const loading = await browser.$('[data-testid="search-loading"]');
      await loading.waitForDisplayed({ timeout: 3000 });
      await expect(loading).toBeDisplayed();
      await expect(await browser.$('[data-testid="search-submit"]')).toBeDisabled();

      const results = await browser.$('[data-testid="search-results"]');
      await results.waitForDisplayed({ timeout: 5000 });
      await expect(await browser.$('[data-testid="search-loading"]')).not.toBeDisplayed();
      expect(stub.calls).toBe(1);
    } finally {
      await stub.dispose();
    }
  });

  it("rechaza un fichero que no es imagen con error en español y sin llamar a la API", async () => {
    const stub = await stubSearchApi({ statusCode: 200, body: searchResponseFixture() });
    try {
      await browser.url("/buscar");
      await selectFileAndSubmit("query-not-image.txt");

      const validationError = await browser.$('[data-testid="search-validation-error"]');
      await validationError.waitForDisplayed({ timeout: 3000 });
      await expect(validationError).toHaveText(
        "Formato no soportado: solo se aceptan imágenes JPEG, PNG o WebP.",
      );
      await expect(await browser.$('[data-testid="search-loading"]')).not.toBeDisplayed();
      await expect(await browser.$('[data-testid="search-results"]')).not.toBeDisplayed();
      expect(stub.calls).toBe(0);
    } finally {
      await stub.dispose();
    }
  });

  it("muestra el error 415 estructurado de la API en español (FR-011 · UX-001)", async () => {
    const stub = await stubSearchApi({
      statusCode: 415,
      body: mediaTypeNotSupportedFixture,
    });
    try {
      await browser.url("/buscar");
      await selectFileAndSubmit("query.png");

      const errorBox = await browser.$('[data-testid="search-error"]');
      await errorBox.waitForDisplayed({ timeout: 5000 });
      await expect(errorBox).toHaveText(
        "No se pudo completar la búsqueda: la imagen de consulta debe ser JPEG, PNG o WebP (firma por cabecera)",
      );
      expect(stub.calls).toBe(1);
    } finally {
      await stub.dispose();
    }
  });
});
