import { browser } from "@wdio/globals";
/// <reference types="@wdio/types" />

const headless = process.env.WDIO_HEADLESS !== "false";
const isCI = !!process.env.CI;

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
  afterTest: async function (_test, _context, { passed }) {
    if (!passed) {
      await browser.saveScreenshot(`./tests/e2e/screenshots/${Date.now()}.png`);
    }
  },
};
