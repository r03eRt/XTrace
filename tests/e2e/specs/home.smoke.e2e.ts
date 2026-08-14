import { browser, expect } from "@wdio/globals";

/**
 * Smoke E2E (FR-006, UX-001): la home carga y muestra el título.
 * Selectores estables por data-testid. Requiere app en marcha (WDIO_BASE_URL).
 */
describe("Home (smoke)", () => {
  it("carga la página principal", async () => {
    await browser.url("/");
    const title = await browser.$('[data-testid="home-title"]');
    await title.waitForDisplayed({ timeout: 10000 });
    await expect(title).toHaveText(expect.stringContaining("Proyect-skeleton"));
  });
});
