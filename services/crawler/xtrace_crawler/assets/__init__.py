"""Subpaquete `assets`: descarga y transformación de visual assets (PR-029).

- `fetch.py` — descarga de assets permitidos con `SafeHTTPClient` (FR-005/FR-015).
- `storyboard.py` — recorte de tiles de un sprite con Pillow + timestamp
  aproximado (FR-005).
- `preview.py` — extracción de frames de previews CORTOS con FFmpeg; nunca
  vídeo completo (FR-005/SC-006).
"""
