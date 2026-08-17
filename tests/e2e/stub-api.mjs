/**
 * Stub HTTP real de la API de búsqueda de XTrace (spec 003 · contracts/README.md).
 *
 * PR-059: sustituye la interceptación BiDi del E2E (frágil: carreras de red) por un
 * servidor HTTP real **sin dependencias** (solo `node:http`). Escucha en
 * `127.0.0.1:8000`, el mismo default de `NEXT_PUBLIC_XTRACE_API_URL` del frontend, y lo
 * arranca `wdio.conf.ts` (hook `onPrepare`) antes de la smoke suite → el E2E habla con
 * él por HTTP real (determinista, sin carreras, sin API real en CI — SC-005).
 *
 * Endpoints:
 * - `POST /search` (multipart): si el nombre de fichero de la parte `image` empieza por
 *   `bad` → `415` del contracts §5 (mismo `error_type` y mensaje que
 *   `services/api/xtrace_api/media.py`); en otro caso espera `SEARCH_DELAY_MS` (~1,5 s,
 *   para que la UI muestre el estado de carga — UX-002) y responde el fixture
 *   `tests/e2e/fixtures/search-response.json` (contracts §1, Content-Type
 *   application/json).
 * - `GET /health` → `{ "status": "ok", "service": "xtrace-api", "version": "0.1.0" }`
 *   (contracts §2).
 * - `GET /__count` → `{ "search_calls": N }`: contador de `POST /search` recibidos;
 *   permite a los tests E2E asertar "sin llamar a la API" (validación de cliente).
 * CORS: `Access-Control-Allow-Origin: *` + preflight `OPTIONS` (contracts §6: el
 * `fetch` del frontend es cross-origin localhost:3000 → 127.0.0.1:8000).
 * Graceful: `SIGTERM`/`SIGINT` → cierra el server (el runner de wdio lo mata al
 * terminar la suite).
 */
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HOST = "127.0.0.1";
const PORT = 8000;
/** Retardo de `POST /search` (éxito): deja visible el estado de carga (UX-002). */
const SEARCH_DELAY_MS = Number(process.env.STUB_SEARCH_DELAY_MS ?? 1500);
/** contracts §1: media ≤ 10 MB; margen para cabeceras multipart. */
const MAX_BODY_BYTES = 11 * 1024 * 1024;

const fixturePath = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "fixtures",
  "search-response.json",
);
const searchFixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));

/** Nº de peticiones `POST /search` recibidas (aserciones E2E vía `GET /__count`). */
let searchCalls = 0;

function sendJson(res, statusCode, body) {
  const payload = JSON.stringify(body);
  res.writeHead(statusCode, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(payload),
    "Access-Control-Allow-Origin": "*",
  });
  res.end(payload);
}

function getBoundary(contentType) {
  const match = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(contentType ?? "");
  return match ? (match[1] ?? match[2]) : null;
}

/** Nombre de fichero de la parte multipart `image`, o `null` si no existe. */
function extractImageFilename(body, boundary) {
  if (!boundary) return null;
  const parts = body.toString("latin1").split(`--${boundary}`);
  for (const part of parts) {
    const headerEnd = part.indexOf("\r\n\r\n");
    if (headerEnd === -1) continue;
    const headers = part.slice(0, headerEnd);
    if (!/name="image"/.test(headers)) continue;
    const filename = /filename="([^"]*)"/.exec(headers);
    return filename ? filename[1] : null;
  }
  return null;
}

function handleSearch(req, res) {
  searchCalls += 1;
  const chunks = [];
  let size = 0;
  req.on("data", (chunk) => {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) {
      req.destroy(); // supera el límite del contrato: cortar la conexión
      return;
    }
    chunks.push(chunk);
  });
  req.on("error", () => undefined); // cliente desconectado (abort): no romper el stub
  req.on("end", () => {
    if (req.destroyed) return;
    const filename = extractImageFilename(
      Buffer.concat(chunks),
      getBoundary(req.headers["content-type"]),
    );
    if (filename === null) {
      sendJson(res, 400, {
        error: "la petición no incluye la parte de fichero 'image'",
        error_type: "missing_file_part",
      });
      return;
    }
    if (filename.startsWith("bad")) {
      // 415 del contracts §5: mismo `error_type` y mensaje que services/api (media.py).
      sendJson(res, 415, {
        error: "la imagen de consulta debe ser JPEG, PNG o WebP (firma por cabecera)",
        error_type: "media_type_not_supported",
      });
      return;
    }
    // Retardo deliberado: la UI debe mostrar el feedback de carga hasta la respuesta.
    setTimeout(() => sendJson(res, 200, searchFixture), SEARCH_DELAY_MS);
  });
}

const server = http.createServer((req, res) => {
  const { pathname } = new URL(req.url ?? "/", `http://${HOST}:${PORT}`);

  if (req.method === "OPTIONS") {
    // Preflight CORS (contracts §6): fetch del frontend es cross-origin.
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Access-Control-Max-Age": "86400",
    });
    res.end();
    return;
  }
  if (req.method === "GET" && pathname === "/health") {
    sendJson(res, 200, { status: "ok", service: "xtrace-api", version: "0.1.0" });
    return;
  }
  if (req.method === "GET" && pathname === "/__count") {
    sendJson(res, 200, { search_calls: searchCalls });
    return;
  }
  if (req.method === "POST" && pathname === "/search") {
    handleSearch(req, res);
    return;
  }
  sendJson(res, 404, { error: "recurso no encontrado", error_type: "not_found" });
});

server.on("error", (err) => {
  console.error(`[stub-api] error en ${HOST}:${PORT}: ${err.message}`);
  process.exit(1);
});

server.listen(PORT, HOST, () => {
  console.log(
    `[stub-api] escuchando en http://${HOST}:${PORT} (delay /search: ${SEARCH_DELAY_MS} ms)`,
  );
});

/** Graceful shutdown: el runner de wdio envía SIGTERM al terminar la suite. */
function shutdown(signal) {
  console.log(`[stub-api] ${signal} recibido, cerrando…`);
  server.close(() => process.exit(0));
  // Fuerza la salida si alguna conexión mantiene el server abierto.
  setTimeout(() => process.exit(0), 2000).unref();
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
