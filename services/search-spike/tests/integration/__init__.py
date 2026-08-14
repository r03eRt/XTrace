"""Tests de integración del servicio search-spike.

Los tests que requieren el extra `siglip` (torch/open_clip) viven en este
paquete y están marcados `@slow` (opcionales en CI); se saltan con
`pytest.importorskip("torch")` si el extra no está instalado (PR-005).
"""
