# Constitución del Proyecto

> Fuente de verdad no negociable para todos los agentes (Codex, Claude, ChatGPT,
> DeepSeek, Qwen y cualquier otro agente de VS Code). El nombre del proveedor no
> altera el proceso: todos respetan exactamente las mismas puertas de calidad.

**Estado**: `ACTIVE` · **Versión**: 1.0.0 · **Ratificada**: 2026-08-05 · **Última enmienda**: 2026-08-05

Esta constitución prevalece sobre cualquier otra práctica. Toda enmienda requiere
documentación, aprobación humana explícita y plan de migración.

---

## 1. Spec-first (NO NEGOCIABLE)

- Ninguna funcionalidad se implementa sin una spec aprobada.
- La spec define **qué** debe suceder y **por qué**.
- El plan técnico define **cómo** se implementará.
- Un cambio funcional requiere actualizar **primero** la spec.
- El código nunca sustituye a la especificación.
- Cuando código y spec difieran, la tarea se **bloquea** hasta resolver la discrepancia.

## 2. Aprobación humana

Estados posibles de una spec:

`DRAFT` → `CLARIFICATION_REQUIRED` → `READY_FOR_REVIEW` → `APPROVED` → `IMPLEMENTING` → `IMPLEMENTED` → `DEPRECATED`

- **Solo el humano responsable** puede cambiar una spec a `APPROVED`.
- La frase necesaria y exacta es: **`Especificación aprobada`**.
- Ningún agente puede interpretar silencio, ausencia de comentarios o una respuesta
  ambigua como aprobación.

## 3. Trazabilidad

Toda modificación funcional debe poder relacionarse con: una spec · uno o varios
requisitos · criterios de aceptación · tareas · tests · commits · un pull request.

Identificadores estables de requisito:

| Prefijo    | Tipo                          |
| ---------- | ----------------------------- |
| `FR-001`   | Requisito funcional           |
| `NFR-001`  | Requisito no funcional        |
| `SEC-001`  | Requisito de seguridad        |
| `DATA-001` | Requisito de datos            |
| `UX-001`   | Requisito de experiencia (UX) |

Los tests relevantes deben indicar qué requisito o criterio de aceptación validan.

## 4. Pull requests

- Cada feature tendrá una rama y un PR independientes.
- No mezclar funcionalidades no relacionadas.
- Los arreglos descubiertos durante una feature se incluyen solo si son
  imprescindibles para ella; el resto se convierte en nueva tarea o issue.
- **Nadie** hace push directo a `main`. Producción se despliega exclusivamente desde `main`.
- Cada PR debe tener su despliegue Preview de Vercel.
- El PR no puede fusionarse con CI fallido.
- El implementador **no puede aprobar su propio trabajo**.

## 5. Desarrollo multiagente

- Todos los agentes leen primero `AGENTS.md`.
- Todos cargan las skills aplicables antes de modificar archivos.
- Un agente trabaja únicamente sobre la tarea asignada.
- Dos agentes no pueden editar simultáneamente los mismos archivos.
- Las tareas paralelas deben estar expresamente marcadas como paralelizables.
- El orquestador asigna tareas y resuelve dependencias; los trabajadores no
  modifican la planificación global.
- Cada agente deja un handoff estructurado.
- La revisión la realiza un agente **diferente** al implementador y, siempre que sea
  posible, con un modelo o proveedor diferente.

## 6. Testing

- Test-first en lógica de negocio, validaciones y regresiones.
- No escribir tests que solo repliquen la implementación.
- No eliminar, omitir o debilitar tests para obtener CI verde.
- Todo bug corregido incluye un test de regresión.
- Los flujos críticos tienen tests E2E.
- Los E2E se implementan **exclusivamente** con WebdriverIO (sufijo `.e2e.ts`),
  en Chrome y headless en CI. Prohibido Playwright/Cypress salvo enmienda aprobada.
- Guardar logs, capturas y evidencias en fallo.
- Los tests no dependen del orden de ejecución; cada test prepara y limpia sus datos.
- No usar datos reales de producción.

## 7. Seguridad y Supabase

- Row Level Security habilitado por defecto en tablas accesibles desde clientes.
- Toda política RLS tiene tests positivos y negativos.
- Migraciones versionadas en Git. Nada de cambios manuales en producción salvo
  emergencia documentada. Sin migraciones destructivas sin plan de recuperación.
- Tipos TypeScript generados desde el esquema de Supabase.
- Sin claves privadas, tokens o secretos en el repositorio.
- Entornos local, preview y producción separados. Nunca exponer la instancia local.
- `service_role` solo en código exclusivamente de servidor.
- Validar permisos en servidor, no únicamente en la interfaz.

## 8. Calidad

Antes de completar una tarea deben pasar, en este orden: formato · lint · typecheck ·
tests unitarios · tests de componentes · tests de base de datos · tests E2E · build de producción.

Comandos estándar:

```bash
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm test:db
pnpm test:e2e
pnpm build
pnpm verify   # ejecuta todas las comprobaciones obligatorias en orden
```

## 9. Dependencias

- Usar siempre la última versión estable compatible de cada dependencia.
- Prohibidas versiones experimentales, beta, canary o RC salvo aprobación explícita.
- No añadir librerías si la plataforma o el framework ya proveen la funcionalidad.

## 10. Gobernanza

- Esta constitución supera cualquier otra práctica.
- Toda ampliación de alcance debe ser explícita; nunca silenciosa.
- Ante conflicto entre una petición y la constitución, la tarea se **bloquea** y se
  eleva al humano responsable.
- Las enmiendas requieren documentación, aprobación (`Especificación aprobada` o
  equivalente explícito) e incremento de versión (SemVer).

**Version**: 1.0.0 | **Ratified**: 2026-08-05 | **Last Amended**: 2026-08-05
