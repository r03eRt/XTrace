# Servicio crawler de XTrace

Servicio Python (3.11) de ingesta de fuentes web al índice visual de XTrace.
Paquete: `xtrace_crawler` · CLI: `xtrace-crawler` · spec: `specs/002-source-sdk-crawler/`.

> Estado actual (PR-019): **bootstrap** — scaffolding, toolchain (uv + ruff + mypy +
> pytest) y CI. Sin lógica de dominio todavía (adapters, jobs y pipeline llegan en
> PR-020..PR-032).

## Toolchain

```bash
cd services/crawler
uv sync            # instala dependencias + crea uv.lock (primera vez)
uv run ruff check .
uv run ruff format --check .
uv run mypy xtrace_crawler
uv run pytest
uv run xtrace-crawler --help
```

## Dependencia editable al spike (ADR-0011)

`xtrace_crawler` reutiliza el pipeline validado del spike (`xtrace_spike`: pHash,
embeddings, vector store, ranking) como **dependencia de camino editable**:

```toml
[tool.uv.sources]
xtrace-spike = { path = "../search-spike", editable = true }
```

El spike permanece **intocado** (solo lectura). Cualquier cambio necesario en él
debe ser un PR propio trazado a la spec 002.

## Configuración (variables de entorno)

Sin secretos en el repositorio; todo se inyecta por env con prefijo
`XTRACE_CRAWLER_` (ver `xtrace_crawler/config.py`):

| Variable                                   | Descripción                          |
| ------------------------------------------ | ------------------------------------ |
| `XTRACE_CRAWLER_SUPABASE_URL`              | URL del proyecto Supabase (SEC-003)  |
| `XTRACE_CRAWLER_SUPABASE_SERVICE_ROLE_KEY` | clave `service_role` (solo servidor) |
| `XTRACE_CRAWLER_LOG_LEVEL`                 | nivel de log (default `INFO`)        |
| `XTRACE_CRAWLER_REQUEST_TIMEOUT_SECONDS`   | timeout HTTP global (default `30.0`) |

## Docker

El build context debe ser la **raíz del repositorio** (la dependencia editable
vive en `../search-spike`):

```bash
docker build -f services/crawler/Dockerfile -t xtrace-crawler .
docker run --rm xtrace-crawler --help
```

## Seguridad

Hardening de la ruta de descarga de assets (PR-036 · SEC-001 · contracts §1/§7):

- **Anti-SSRF — allowlist por fuente, fail-closed**: el pipeline **no** deriva
  los hosts permitidos de las URLs parseadas de la fuente; cada adapter declara
  `asset_hosts` (dominio canónico + CDNs de imágenes/vídeo revisados) como
  parte del contrato `SourceAdapter` (PR-040) y el cliente de assets
  (`SafeHTTPClient`) rechaza cualquier host fuera de esa allowlist
  (`HostNotAllowedError`, degradación por asset, sin red). Un adapter con
  `asset_hosts` vacío (o sin declararlo) **no descarga assets por HTTP**
  (`NoAssetHostsError`); el mock lo declara vacío a propósito porque sirve sus
  assets in-process (`fetch_asset_bytes`, PR-034).
- **Anti-DNS-rebinding**: en la ruta de assets se valida la **IP resuelta** de
  cada host en cada petición —incluidos los redirects— y se rechazan rangos
  privados/link-local/loopback/metadata (RFC1918, `169.254.0.0/16` —incluida
  `169.254.169.254`—, `127.0.0.0/8`, `::1`, `fc00::/7`, `fe80::/10`) con
  `PrivateIPError`, antes de emitir la petición.
- **Decompression bomb**: toda imagen descargada se abre con un límite estricto
  de píxeles (`XTRACE_CRAWLER_MAX_IMAGE_PIXELS`, default 50 MP), verificado por
  header **antes** de decodificar (`ImageTooManyPixelsError`, degradación por
  asset).
- **Residual TOCTOU de DNS**: la validación resuelve con `socket.getaddrinfo`
  en el momento de la petición; la conexión real la abre httpx, así que existe
  una ventana entre validación y conexión (mitigación prevista en plan §Risks:
  pinning de IP con transporte propio).
- **Hosts de assets de xvideos PROVISIONAL**: la allowlist del adapter xvideos
  es provisional (estructura asumida; ver `tests/fixtures/xvideos/README.md` y
  `adapters/xvideos.py`) — se valida contra la estructura real en PR-033 tras
  la revisión legal humana (SEC-002). Mientras tanto el adapter permanece
  **deshabilitado** (gate SEC-002): sin allowlist revisada no hay descarga
  (fail-closed).

## Referencias

- Spec: `specs/002-source-sdk-crawler/spec.md` (APPROVED) · Plan: `plan.md` · Tareas: `tasks.md`
- ADR-0011: reutilización del spike como dependencia editable
