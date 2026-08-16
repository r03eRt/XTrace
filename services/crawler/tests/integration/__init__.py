"""Tests de integración del servicio crawler (PR-028): repo fuentes/vídeos-web/stats.

Se skippean si Supabase local no es alcanzable (mismo patrón que el spike,
`services/search-spike/tests/integration/`); cada test limpia sus datos
(constitución §6).
"""
