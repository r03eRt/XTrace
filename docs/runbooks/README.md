# Runbooks

Procedimientos operativos: arranque local, restauración de BD, migraciones e
incidentes. Los procedimientos específicos de esta feature están en
[`temporal-refinement.md`](temporal-refinement.md).

## Reglas comunes

- Trabajar con una rama y una spec aprobada.
- No usar `supabase db reset` sobre datos que deban conservarse.
- No poner secretos, DSN, capturas, bytes de assets ni informes de benchmark dentro
  del checkout.
- Mantener la API en `127.0.0.1` salvo un despliegue explícitamente revisado.
