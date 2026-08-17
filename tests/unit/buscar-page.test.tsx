import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { searchResponseSchema } from "@/lib/api/schemas";
import type { SearchResponse } from "@/lib/api/schemas";
import { searchByImage, SEARCH_TIMEOUT_MS } from "@/lib/api/xtrace";

/**
 * PR-057 · Frontend `/buscar` (spec 003 · contracts §6).
 * Trazabilidad: FR-009 (página/upload), FR-010 (enlace "Ver original"), FR-011 (UI de
 * errores), FR-004 (zod del contrato), UX-001 (español), UX-002 (feedback de carga),
 * UX-003 (score/timestamp/fuente), SEC-001 (solo local, sin auth) y edge cases de la spec.
 */
vi.hoisted(() => {
  // env.ts valida NEXT_PUBLIC_SUPABASE_* al importar; se inyectan antes de cargar módulos.
  process.env.NEXT_PUBLIC_SUPABASE_URL = "http://127.0.0.1:55321";
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY = "test-anon-key";
});

// Import tras el hoisted para que env.ts se evalúe con el entorno completo.
import BuscarPage from "@/features/search/buscar-page";

/** Fixture del contrato REST `POST /search` (spec 003 · contracts §1). */
const SEARCH_RESPONSE_FIXTURE: SearchResponse = {
  search_id: "3f2a1c4e-8b6d-4f2e-9a1c-0e5d7b9a2c11",
  processing_ms: 4123,
  results: [
    {
      video_id: "1a2b3c4d-0000-0000-0000-000000000001",
      local_ref: "MAYO 2026 (386).mp4",
      title: "Video de ejemplo del corpus",
      page_url: "https://www.xvideos.com/video.abc123/ejemplo",
      match_score: 0.938,
      matching_frames: 2,
      match_timestamp_ms: 51000,
      evidence: { visual: 0.95, phash: 0.84 },
    },
    {
      video_id: "1a2b3c4d-0000-0000-0000-000000000002",
      local_ref: "dataset-local-2026.mp4",
      title: null,
      page_url: null,
      match_score: 0.5,
      matching_frames: 1,
      match_timestamp_ms: null,
      evidence: { visual: 0.5, phash: 0.4 },
    },
  ],
};

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

function pngFile(name = "captura.png"): File {
  return new File([new Uint8Array([137, 80, 78, 71])], name, { type: "image/png" });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("BuscarPage — estados (FR-009, UX-002)", () => {
  it("renderiza el estado inicial idle con selector de fichero y sin resultados (FR-009)", () => {
    render(<BuscarPage />);
    expect(screen.getByTestId("buscar-title")).toHaveTextContent("Buscar por imagen");
    expect(screen.getByTestId("search-dropzone")).toBeInTheDocument();
    expect(screen.getByTestId("search-file-input")).toBeInTheDocument();
    expect(screen.getByTestId("search-submit")).toHaveTextContent("Buscar");
    expect(screen.queryByTestId("search-results")).not.toBeInTheDocument();
    expect(screen.queryByTestId("search-error")).not.toBeInTheDocument();
    expect(screen.queryByTestId("search-loading")).not.toBeInTheDocument();
  });

  it("muestra feedback de carga durante la búsqueda y lo oculta al terminar (UX-002)", async () => {
    let resolveFetch!: (r: Response) => void;
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    const user = userEvent.setup();
    render(<BuscarPage />);
    await user.upload(screen.getByTestId("search-file-input"), pngFile());
    await user.click(screen.getByTestId("search-submit"));
    expect(await screen.findByTestId("search-loading")).toHaveTextContent("Buscando");
    expect(screen.getByTestId("search-submit")).toBeDisabled();
    await act(async () => {
      resolveFetch(jsonResponse(SEARCH_RESPONSE_FIXTURE));
    });
    expect(await screen.findByTestId("search-results")).toBeInTheDocument();
    expect(screen.queryByTestId("search-loading")).not.toBeInTheDocument();
  });
});

describe("BuscarPage — validación de cliente (UX-001, base SC-006)", () => {
  it("rechaza en cliente un tipo no soportado con mensaje en español y sin llamar a la API", async () => {
    // applyAccept: false → userEvent no filtra por el atributo accept del input;
    // así el fichero llega al onChange y se prueba la validación de tipo del cliente.
    const user = userEvent.setup({ applyAccept: false });
    render(<BuscarPage />);
    const bad = new File(["hola"], "nota.txt", { type: "text/plain" });
    await user.upload(screen.getByTestId("search-file-input"), bad);
    await user.click(screen.getByTestId("search-submit"));
    expect(await screen.findByTestId("search-validation-error")).toHaveTextContent(
      "Formato no soportado",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rechaza en cliente una imagen > 10 MB con mensaje en español y sin llamar a la API", async () => {
    const user = userEvent.setup();
    render(<BuscarPage />);
    const big = new File([new Uint8Array(10 * 1024 * 1024 + 1)], "grande.png", {
      type: "image/png",
    });
    await user.upload(screen.getByTestId("search-file-input"), big);
    await user.click(screen.getByTestId("search-submit"));
    expect(await screen.findByTestId("search-validation-error")).toHaveTextContent("10 MB");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("pide seleccionar una imagen si se envía el formulario sin fichero (edge: parte ausente)", async () => {
    const user = userEvent.setup();
    render(<BuscarPage />);
    await user.click(screen.getByTestId("search-submit"));
    expect(await screen.findByTestId("search-validation-error")).toHaveTextContent(
      "Selecciona una imagen",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("BuscarPage — render de resultados (FR-009/010, UX-003)", () => {
  it("renderiza los resultados del contrato: título, fuente, score, timestamp y enlace Ver original", async () => {
    fetchMock.mockResolvedValue(jsonResponse(SEARCH_RESPONSE_FIXTURE));
    const user = userEvent.setup();
    render(<BuscarPage />);
    await user.upload(screen.getByTestId("search-file-input"), pngFile());
    await user.click(screen.getByTestId("search-submit"));
    const first = await screen.findByTestId("search-result-0");
    expect(within(first).getByTestId("search-result-title")).toHaveTextContent(
      "Video de ejemplo del corpus",
    );
    expect(within(first).getByTestId("search-result-source")).toHaveTextContent("www.xvideos.com");
    expect(within(first).getByTestId("search-result-score")).toHaveTextContent("0.938");
    expect(within(first).getByTestId("search-result-timestamp")).toHaveTextContent("00:51");
    const link = within(first).getByTestId("search-result-link");
    expect(link).toHaveAttribute("href", "https://www.xvideos.com/video.abc123/ejemplo");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveTextContent("Ver original");
  });

  it("muestra la referencia local sin enlace cuando no hay page_url y '—' sin timestamp (edge cases)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(SEARCH_RESPONSE_FIXTURE));
    const user = userEvent.setup();
    render(<BuscarPage />);
    await user.upload(screen.getByTestId("search-file-input"), pngFile());
    await user.click(screen.getByTestId("search-submit"));
    const second = await screen.findByTestId("search-result-1");
    expect(within(second).getByTestId("search-result-title")).toHaveTextContent(
      "dataset-local-2026.mp4",
    );
    expect(within(second).getByTestId("search-result-source")).toHaveTextContent("—");
    expect(within(second).getByTestId("search-result-timestamp")).toHaveTextContent("—");
    expect(within(second).queryByTestId("search-result-link")).not.toBeInTheDocument();
    expect(within(second).getByTestId("search-result-local-ref")).toHaveTextContent(
      "dataset-local-2026.mp4",
    );
  });

  it("mantiene el orden devuelto por la API sin reordenar (UX-003)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(SEARCH_RESPONSE_FIXTURE));
    const user = userEvent.setup();
    render(<BuscarPage />);
    await user.upload(screen.getByTestId("search-file-input"), pngFile());
    await user.click(screen.getByTestId("search-submit"));
    const list = await screen.findByTestId("search-results");
    expect(list.textContent?.indexOf("0.938")).toBeGreaterThan(-1);
    expect(list.textContent!.indexOf("0.938")).toBeLessThan(list.textContent!.indexOf("0.500"));
  });

  it("muestra estado vacío sin error cuando la API devuelve results [] (edge case spec)", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ...SEARCH_RESPONSE_FIXTURE, results: [] }));
    const user = userEvent.setup();
    render(<BuscarPage />);
    await user.upload(screen.getByTestId("search-file-input"), pngFile());
    await user.click(screen.getByTestId("search-submit"));
    expect(await screen.findByTestId("search-empty")).toHaveTextContent(
      "No se encontraron resultados",
    );
    expect(screen.queryByTestId("search-error")).not.toBeInTheDocument();
  });
});

describe("BuscarPage — errores (FR-011 UI, UX-001)", () => {
  it("muestra un error en español si la red falla (edge: índice/API no disponible)", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const user = userEvent.setup();
    render(<BuscarPage />);
    await user.upload(screen.getByTestId("search-file-input"), pngFile());
    await user.click(screen.getByTestId("search-submit"));
    expect(await screen.findByTestId("search-error")).toHaveTextContent(
      "No se pudo conectar con el servicio de búsqueda",
    );
    expect(screen.queryByTestId("search-loading")).not.toBeInTheDocument();
  });

  it("muestra el mensaje estructurado de la API (5xx) en español", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ error: "fallo interno del servicio", error_type: "internal_error" }, 500),
    );
    const user = userEvent.setup();
    render(<BuscarPage />);
    await user.upload(screen.getByTestId("search-file-input"), pngFile());
    await user.click(screen.getByTestId("search-submit"));
    expect(await screen.findByTestId("search-error")).toHaveTextContent(
      "fallo interno del servicio",
    );
  });
});

describe("BuscarPage — cancelación (edge case spec: sin estados colgados)", () => {
  it("cancela la búsqueda en curso al seleccionar otro fichero y no deja estados colgados", async () => {
    let capturedSignal: AbortSignal | undefined;
    fetchMock.mockImplementation(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          capturedSignal = init?.signal ?? undefined;
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    const user = userEvent.setup();
    render(<BuscarPage />);
    await user.upload(screen.getByTestId("search-file-input"), pngFile("a.png"));
    await user.click(screen.getByTestId("search-submit"));
    expect(await screen.findByTestId("search-loading")).toBeInTheDocument();
    await user.upload(screen.getByTestId("search-file-input"), pngFile("b.png"));
    await waitFor(() => expect(capturedSignal?.aborted).toBe(true));
    expect(screen.queryByTestId("search-loading")).not.toBeInTheDocument();
    expect(screen.queryByTestId("search-error")).not.toBeInTheDocument();
  });
});

describe("Cliente API — contrato y timeout (FR-004, contracts §1/§6)", () => {
  it("el cliente envía multipart con la parte image y valida la respuesta del contrato", async () => {
    fetchMock.mockResolvedValue(jsonResponse(SEARCH_RESPONSE_FIXTURE));
    const response = await searchByImage(pngFile());
    expect(response.search_id).toBe(SEARCH_RESPONSE_FIXTURE.search_id);
    expect(response.results).toHaveLength(2);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8000/search");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get("image")).toBeInstanceOf(File);
    expect((init.body as FormData).get("top_k")).toBe("10");
    expect((init.body as FormData).get("min_score")).toBe("0.0");
  });

  it("el cliente lanza error en español si la búsqueda supera el timeout de 60 s", async () => {
    vi.useFakeTimers();
    try {
      fetchMock.mockImplementation(
        (_url: string, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      );
      const pending = searchByImage(pngFile());
      vi.advanceTimersByTime(SEARCH_TIMEOUT_MS + 1_000);
      await expect(pending).rejects.toThrow(/tardó demasiado/);
    } finally {
      vi.useRealTimers();
    }
  });

  it("zod acepta el fixture del contrato §1", () => {
    expect(searchResponseSchema.safeParse(SEARCH_RESPONSE_FIXTURE).success).toBe(true);
  });

  it("zod tolera title/page_url ausentes (extensión MAY del contrato)", () => {
    const minimal = {
      ...SEARCH_RESPONSE_FIXTURE,
      results: [
        {
          video_id: "1a2b3c4d-0000-0000-0000-000000000003",
          local_ref: "local.mp4",
          match_score: 0.5,
          matching_frames: 1,
          match_timestamp_ms: null,
          evidence: { visual: 0.5, phash: 0.4 },
        },
      ],
    };
    expect(searchResponseSchema.safeParse(minimal).success).toBe(true);
  });

  it("zod rechaza desviaciones del contrato (video_id ausente)", () => {
    const badResult = { ...SEARCH_RESPONSE_FIXTURE.results[0]! } as Record<string, unknown>;
    delete badResult.video_id;
    const bad = { ...SEARCH_RESPONSE_FIXTURE, results: [badResult] };
    expect(searchResponseSchema.safeParse(bad).success).toBe(false);
  });
});
