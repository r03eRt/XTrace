import { spawn, type ChildProcess } from "node:child_process";
import * as http from "node:http";
import { browser } from "@wdio/globals";
/// <reference types="@wdio/types" />

const headless = process.env.WDIO_HEADLESS !== "false";
const isCI = !!process.env.CI;

// --- Stub HTTP real de la API de búsqueda (PR-059) ---------------------------
// La API no corre en CI: el stub `tests/e2e/stub-api.mjs` (Node nativo, sin deps)
// escucha en 127.0.0.1:8000 (default de NEXT_PUBLIC_XTRACE_API_URL) y sustituye la
// interceptación BiDi del E2E (frágil) por HTTP real y determinista (SC-005).
// Nota: WebdriverIO v9 eliminó la opción `webServer` del config (existía en v8), así
// que el stub se arranca con el hook estándar `onPrepare` y se para en `onComplete`.
const STUB_URL = "http://127.0.0.1:8000";
const STUB_START_TIMEOUT_MS = 30000;
let stubProcess: ChildProcess | null = null;

/** Espera a que el stub responda `GET /health` (200), o falla con mensaje claro. */
async function startApiStub(): Promise<void> {
  stubProcess = spawn(process.execPath, ["tests/e2e/stub-api.mjs"], {
    cwd: process.cwd(),
    stdio: "inherit",
  });
  const deadline = Date.now() + STUB_START_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (stubProcess.exitCode !== null) {
      throw new Error(
        `[stub-api] el proceso terminó antes de arrancar (exit ${stubProcess.exitCode}); ` +
          `¿hay algo escuchando ya en ${STUB_URL}?`,
      );
    }
    if (await isStubHealthy()) return;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`[stub-api] ${STUB_URL}/health no responde tras ${STUB_START_TIMEOUT_MS} ms`);
}

function isStubHealthy(): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(`${STUB_URL}/health`, { timeout: 1000 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

function stopApiStub(): void {
  if (stubProcess && stubProcess.exitCode === null) {
    stubProcess.kill("SIGTERM"); // el stub cierra el server y sale (graceful)
  }
  stubProcess = null;
}

export const config: WebdriverIO.Config = {
  runner: "local",
  tsConfigPath: "./tests/e2e/tsconfig.json",
  specs: ["./tests/e2e/specs/**/*.e2e.ts"],
  suites: {
    smoke: ["./tests/e2e/specs/**/*.smoke.e2e.ts"],
  },
  maxInstances: 1,
  capabilities: [
    {
      browserName: "chrome",
      "goog:chromeOptions": {
        args: [
          ...(headless ? ["--headless=new", "--disable-gpu"] : []),
          "--no-sandbox",
          "--window-size=1280,800",
        ],
      },
    },
  ],
  logLevel: "info",
  baseUrl: process.env.WDIO_BASE_URL ?? "http://localhost:3000",
  waitforTimeout: 10000,
  connectionRetryTimeout: 120000,
  connectionRetryCount: 3,
  specFileRetries: isCI ? 1 : 0,
  framework: "mocha",
  reporters: ["spec", ["junit", { outputDir: "./tests/e2e/.reports" }]],
  mochaOpts: { ui: "bdd", timeout: 60000 },
  onPrepare: startApiStub,
  onComplete: stopApiStub,
  afterTest: async function (_test, _context, { passed }) {
    if (!passed) {
      await browser.saveScreenshot(`./tests/e2e/screenshots/${Date.now()}.png`);
    }
  },
};
