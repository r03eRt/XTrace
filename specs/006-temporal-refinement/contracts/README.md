# Contratos: refinamiento temporal

## 1. REST `POST /search`

Se mantienen todos los campos del contrato de Spec 003. La respuesta añade campos
compatibles hacia delante:

```json
{
  "search_id": "uuid",
  "processing_ms": 1234,
  "refinement": {
    "status": "completed",
    "candidates_requested": 3,
    "candidates_processed": 2,
    "assets_evaluated": 18,
    "assets_discarded": 1,
    "errors_count": 0,
    "bytes_downloaded": 184320,
    "embedding_count": 18,
    "embedding_elapsed_ms": 72,
    "improved_results": 1,
    "elapsed_ms": 940
  },
  "results": [
    {
      "video_id": "uuid",
      "local_ref": null,
      "title": "Vídeo",
      "page_url": "https://www.xvideos.com/video.example/slug",
      "match_score": 0.938,
      "matching_frames": 1,
      "match_timestamp_ms": 454000,
      "evidence": {"visual": 0.99, "phash": 0.91},
      "timestamp_provenance": {
        "origin": "refined_asset",
        "status": "improved",
        "source": "xvideos",
        "asset_kind": "thumbnail",
        "asset_url": "https://thumb-cdn77.xvideos-cdn.com/.../xv_12_t.jpg",
        "asset_position": 12
      }
    }
  ]
}
```

`asset_url` se omite/queda `null` para resultados base. Nunca contiene credenciales ni
se acepta como entrada para una petición posterior del cliente. Los consumidores que
no conozcan `refinement` o `timestamp_provenance` siguen leyendo
`match_timestamp_ms`.

Estados de un resultado: `improved` (se sustituyó el timestamp), `unchanged` (se
evaluaron assets pero no superaron la guardia), `unavailable` (fuente/asset no
disponible), `limited` (presupuesto agotado) y `disabled` (operador o entorno lo
desactivó). Estados de resumen: `completed`, `disabled`, `unavailable`, `limited`,
`failed`.

## 2. Puerto interno de refinamiento

El API define un puerto inyectable, sin exponerlo como endpoint:

```python
class TemporalRefinementService(Protocol):
    async def refine(
        self,
        query_image: Image.Image,
        ranked: Sequence[RankedVideo],
        metadata: Mapping[str, VideoMetadata],
        *,
        policy: RefinementPolicy,
    ) -> RefinementOutcome: ...
```

`RefinementOutcome` conserva el orden de `ranked`, reemplaza únicamente
`match_timestamp_ms`/provenance y devuelve un resumen de contadores. El puerto no tiene
métodos para escribir en `VectorStore`.

## 3. Frontera de assets

El servicio usa únicamente estos miembros existentes del adapter:

- `AdapterRegistry.get_enabled(source, enabled_in_db=True)`;
- `SourceAdapter.get_visual_assets(VideoSource)`;
- `SourceAdapter.fetch_asset_bytes(url)` si el adapter lo ofrece;
- `AssetFetcher`/`SafeHTTPClient` con `asset_hosts` cuando debe usar HTTP.

Se aceptan `thumbnail` y `storyboard`; `preview` se rechaza explícitamente. El adapter
debe proporcionar `timestamp_ms` válido para que un asset pueda fijar el resultado.

## 4. Telemetría

Los logs estructurados solo pueden incluir `search_id`, `source`, `video_id`, contador,
estado y duración. Nunca se registran bytes, imagen de consulta, URL completa con
parámetros ni excepciones de proveedor. La tabla de evidencia guarda la URL pública
sanitizada únicamente para trazabilidad server-side; los contadores de bytes y
embeddings son agregados y no contienen contenido.
