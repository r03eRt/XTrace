# Feature Specification: Refinamiento temporal bajo demanda

**Feature Branch**: `feature/006-temporal-refinement`

**Created**: 2026-08-18

**Status**: APPROVED

**Input**: User direction: "El índice base identifica el vídeo, pero el timestamp puede
ser demasiado impreciso. Refinar solo los candidatos principales con más assets públicos
permitidos para acercar la posición temporal sin inflar el índice global ni descargar
vídeos completos."

## Objetivo

Mejorar el timestamp aproximado devuelto por XTrace después de una primera búsqueda
barata sobre el índice global adaptativo. El refinamiento debe aplicarse únicamente a
los candidatos principales, usar exclusivamente assets legalmente accesibles y dejar
claro cuándo la fuente no permite una precisión mayor.

## Alcance

- Refinar una búsqueda de imagen que ya produjo candidatos en el índice base.
- Obtener y evaluar assets visuales adicionales de los candidatos principales cuando la
  fuente los exponga públicamente y el adapter esté habilitado.
- Devolver un timestamp refinado, su procedencia y el estado del refinamiento.
- Mantener el índice global base limitado por la política aprobada de 8 frames objetivo.
- Medir calidad, coste, latencia y degradaciones por fuente y duración.

## Fuera de alcance

- Cambiar el máximo o el default del índice global base.
- Descargar o conservar vídeos web completos.
- Saltar CAPTCHA, paywalls, DRM, autenticación, anti-bot u otras restricciones.
- Reconocimiento facial o identificación biométrica.
- Añadir nuevos proveedores o adapters sin una revisión independiente de legalidad y
  términos de servicio.
- Búsqueda por clip y consistencia temporal entre múltiples frames de una consulta.
- Inventar, interpolar o presentar como exacto un timestamp que la fuente no respalda.

## Actores

- **Invitado**: realiza una búsqueda y recibe un timestamp aproximado mejorado cuando es
  posible.
- **Administrador**: configura límites operativos, consulta métricas y revisa fallos.
- **Adapter/worker**: expone assets permitidos, respeta los límites de la fuente y
  materializa el resultado del refinamiento.

## User Scenarios & Testing

### User Story 1 - Timestamp más preciso para el candidato principal (Priority: P1)

Una persona sube una imagen. XTrace identifica primero los vídeos candidatos con el
índice global y, solo para los candidatos principales, busca assets adicionales para
acercar el timestamp de la escena.

**Why this priority**: Identificar el vídeo correcto sin localizar razonablemente la
escena no resuelve el problema principal observado en vídeos largos.

**Independent Test**: Ejecutar una consulta con vídeo y timestamp de referencia conocidos
en un corpus que tenga assets adicionales permitidos y comparar el error del primer pase
con el error tras el refinamiento.

**Acceptance Scenarios**:

1. **Given** una consulta cuyo primer pase identifica un candidato y la fuente ofrece
   assets adicionales permitidos, **When** se ejecuta el refinamiento, **Then** el
   resultado conserva el vídeo correcto y devuelve el timestamp del mejor asset
   realmente evaluado.
2. **Given** un candidato cuyo primer timestamp está lejos de la verdad conocida,
   **When** el refinamiento encuentra un asset más próximo, **Then** el error temporal
   disminuye y la respuesta indica que el timestamp procede del refinamiento.
3. **Given** que ningún asset adicional mejora la evidencia, **When** finaliza el
   refinamiento, **Then** se conserva el mejor resultado anterior y se informa de que no
   hubo mejora.

### User Story 2 - Degradación segura cuando la fuente no permite refinar (Priority: P1)

El operador necesita que una fuente con pocos thumbnails, errores transitorios o
restricciones declaradas siga produciendo un resultado honesto sin que el sistema
intente eludirlas.

**Why this priority**: La disponibilidad de assets depende del proveedor y no puede
convertirse en una descarga encubierta del vídeo.

**Independent Test**: Simular una fuente sin assets adicionales, una respuesta 403/404 y
un timeout; comprobar que la respuesta sigue siendo válida, no inventa timestamps y no
deja temporales ni jobs ambiguos.

**Acceptance Scenarios**:

1. **Given** que la fuente solo expone los assets ya indexados, **When** se solicita el
   refinamiento, **Then** se devuelve el resultado del primer pase con estado
   `unavailable` o equivalente y sin cambiar el índice base.
2. **Given** un error temporal o un límite de la fuente, **When** falla la obtención de
   assets adicionales, **Then** el resultado conserva el candidato del primer pase y
   registra el motivo sin reintentar más allá del presupuesto permitido.
3. **Given** que un asset adicional es ilegible, duplicado o no tiene posición fiable,
   **When** se procesa, **Then** se descarta ese asset y no se presenta una precisión
   superior a la evidencia restante.

### User Story 3 - Refinamiento acotado y observable (Priority: P2)

El administrador quiere limitar el coste del refinamiento y saber cuándo se ha usado,
cuántos assets se han evaluado y qué mejora ha producido por fuente y duración.

**Why this priority**: Sin límites y métricas, un refinamiento correcto para un vídeo
puede hacer inviable el catálogo multi-proveedor.

**Independent Test**: Ejecutar búsquedas con distintos números de candidatos y presupuestos
de assets, y comprobar que los límites se respetan y las métricas son reproducibles.

**Acceptance Scenarios**:

1. **Given** una búsqueda con más candidatos que el límite configurado, **When** se
   solicita el refinamiento, **Then** solo se procesan los candidatos principales según
   el límite vigente.
2. **Given** un presupuesto de assets o tiempo agotado, **When** continúa la búsqueda,
   **Then** se devuelve el mejor resultado disponible y se marca el refinamiento como
   limitado, sin bloquear indefinidamente la consulta.
3. **Given** varias búsquedas sobre la misma fuente y vídeo, **When** se consultan las
   métricas, **Then** se pueden distinguir primer pase, refinamiento, assets evaluados,
   mejora temporal y fallos sin almacenar la imagen de consulta más allá de su TTL.

## Edge Cases

- El primer pase no devuelve candidatos con score suficiente.
- Hay empate entre candidatos principales.
- La fuente cambia o elimina el storyboard entre el primer pase y el refinamiento.
- Los assets adicionales tienen timestamps repetidos, desordenados, fuera de rango o
  ausentes.
- El vídeo es muy corto y un único asset ya cubre toda la precisión razonable.
- El vídeo dura más que el límite operativo de refinamiento.
- El CDN devuelve 403, 404, rate limit, contenido HTML o una imagen corrupta.
- El refinamiento se cancela, expira o se repite de forma concurrente.
- El vídeo queda marcado como no disponible o no permitido mientras se refina.
- El proveedor solo ofrece un asset adicional, aunque el objetivo lógico sea mayor.

## Requirements

### Functional Requirements

- **FR-001**: El sistema MUST ejecutar automáticamente el refinamiento después de un
  primer pase de búsqueda sobre el índice global y MUST conservar su resultado como
  fallback.
- **FR-002**: El sistema MUST refinar por defecto los 3 candidatos principales y MUST
  admitir un máximo absoluto configurable de 5 candidatos.
- **FR-003**: El sistema MUST usar únicamente assets visuales públicos, permitidos y
  servidos por un adapter habilitado para la fuente del candidato.
- **FR-004**: El sistema MUST evaluar hasta 30 assets adicionales por candidato como
  máximo de referencia y MUST permanecer dentro de los límites de tiempo, tamaño y rate
  limit aprobados para la fuente.
- **FR-005**: El sistema MUST conservar el timestamp y la procedencia del mejor asset
  realmente evaluado, incluyendo si procede del primer pase o del refinamiento.
- **FR-006**: El sistema MUST NOT interpolar ni fabricar timestamps cuando la fuente no
  proporciona una posición temporal respaldada.
- **FR-007**: El sistema MUST descartar assets inaccesibles, corruptos, duplicados o sin
  posición utilizable sin invalidar el resultado del primer pase.
- **FR-008**: Si el refinamiento no mejora o empeora la evidencia visual, o no está
  disponible, el sistema MUST devolver el mejor resultado del primer pase y un estado
  explicativo.
- **FR-009**: El refinamiento MUST NOT modificar ni ampliar permanentemente el índice
  global base durante una búsqueda de usuario.
- **FR-010**: Los temporales de consulta y assets MUST eliminarse al finalizar o fallar
  el refinamiento, respetando el TTL aprobado para búsquedas de usuario.
- **FR-011**: El sistema MUST informar de candidatos procesados, assets evaluados,
  descartes, errores, duración del refinamiento y mejora temporal obtenida.
- **FR-012**: El operador MUST poder desactivar o limitar el refinamiento por fuente,
  entorno o presupuesto sin cambiar el índice base.
- **FR-013**: Una repetición idempotente del mismo refinamiento MUST producir un resultado
  equivalente mientras no cambien los assets públicos ni la política vigente.
- **FR-014**: La comparación de adopción MUST usar las mismas consultas con verdad
  temporal independiente para primer pase y refinamiento, y MUST segmentar por fuente y
  duración.

### Non-Functional Requirements

- **NFR-001**: El primer pase MUST seguir siendo utilizable aunque el refinamiento esté
  desactivado, limitado o no disponible.
- **NFR-002**: El refinamiento MUST tener un límite de 10 segundos por búsqueda y de
  3 segundos por candidato, y MUST fallar de forma controlada al agotarlo.
- **NFR-003**: Las métricas MUST permitir comparar coste de assets, embeddings, latencia,
  Top-1/Top-5 y error temporal entre ambos pases.
- **NFR-004**: El resultado MUST ser trazable a una fuente, vídeo, asset y timestamp sin
  exponer secretos ni bytes de vídeo completo.

### Security and Compliance Requirements

- **SEC-001**: El sistema MUST respetar allowlists, robots, términos de servicio,
  rate limits y restricciones declaradas por cada fuente.
- **SEC-002**: El sistema MUST NOT saltar CAPTCHA, paywalls, DRM, autenticación,
  anti-bot ni controles de acceso.
- **SEC-003**: El sistema MUST NOT descargar ni conservar vídeos web completos como parte
  del refinamiento.
- **SEC-004**: El sistema MUST NOT realizar reconocimiento facial ni identificación
  biométrica.
- **SEC-005**: Las consultas y assets temporales MUST limpiarse conforme a la política
  de retención y no deben entrar en Git, logs públicos ni datasets de entrenamiento.

### Data Requirements

- **DATA-001**: Cada resultado refinado MUST conservar vídeo, fuente, timestamp, asset de
  evidencia, estado del refinamiento y si el timestamp es del primer pase o del segundo.
- **DATA-002**: Los errores de fuente, límites y descartes MUST quedar diferenciados de
  un resultado sin mejora.
- **DATA-003**: Las métricas MUST poder agruparse por fuente, duración, candidato y
  política sin almacenar permanentemente la imagen de consulta.

### UX Requirements

- **UX-001**: La interfaz MUST distinguir visualmente un timestamp del índice base de uno
  refinado, sin presentarlo como exacto si la evidencia es aproximada.
- **UX-002**: Si no se puede refinar, la interfaz MUST mostrar el resultado base y un
  mensaje comprensible de disponibilidad, sin exponer detalles internos innecesarios.
- **UX-003**: La interfaz MUST evitar que el usuario interprete un fallo de refinamiento
  como ausencia del vídeo cuando el primer pase sí lo encontró.

## Key Entities

- **Solicitud de refinamiento**: petición asociada a una búsqueda y a un conjunto
  limitado de candidatos, con estado, presupuesto y expiración.
- **Evidencia refinada**: asset público evaluado, vídeo, posición temporal, procedencia y
  resultado de similitud.
- **Resultado de refinamiento**: candidato final, timestamp, estado (`improved`,
  `unchanged`, `unavailable`, `limited` o equivalente) y métricas resumidas.
- **Política de presupuesto**: límites aprobados por fuente y entorno para candidatos,
  assets, bytes, tiempo y reintentos.

## Success Criteria

### Measurable Outcomes

- **SC-001**: En un benchmark pareado con verdad temporal independiente, el refinamiento
  reduce o mantiene el error temporal absoluto en al menos el 80 % de las consultas con
  assets adicionales disponibles.
- **SC-002**: El refinamiento no reduce el Top-5 global más de 5 puntos porcentuales
  frente al primer pase.
- **SC-003**: El benchmark cubre al menos 30 consultas positivas únicas, una fuente local
  y una web permitida, y los tramos de duración `<5m`, `5-15m` y `>15m` cuando existan
  casos válidos.
- **SC-004**: El 100 % de los refinamientos agotados, fallidos o no disponibles conserva
  un resultado válido del primer pase o comunica que no había candidato válido.
- **SC-005**: El 100 % de los timestamps presentados puede trazarse a un asset realmente
  evaluado o al frame del índice base correspondiente.
- **SC-006**: El refinamiento no persiste vídeos completos ni aumenta de forma permanente
  el número de frames del índice global durante una búsqueda.
- **SC-007**: Las métricas informan de coste, latencia, cobertura y mejora temporal por
  fuente y tramo de duración sin incluir secretos ni bytes de consulta.
- **SC-008**: El primer pase mantiene su contrato y sigue funcionando cuando el
  refinamiento se desactiva o la fuente rechaza assets adicionales.

## Assumptions

- ADR-0013 mantiene el índice base global con un objetivo máximo de 8 frames por vídeo.
- El refinamiento se aplicará inicialmente solo a fuentes y adapters ya habilitados.
- Los assets adicionales pueden ser storyboards, thumbnails u otros recursos visuales
  públicos permitidos, según cada fuente.
- Las búsquedas de usuario mantienen su política de temporales y TTL existente.
- El benchmark usará capturas y timestamps anotados de forma independiente; las
  posiciones derivadas del mismo thumbnail no se considerarán verdad temporal.

## Dependencies

- Spec 001: spike de búsqueda visual y contratos de resultados.
- Spec 002: adapters, allowlists y assets visuales permitidos.
- Spec 003: API y experiencia de búsqueda.
- Spec 005: muestreo adaptativo del índice base.
- ADR-0013: índice global multi-proveedor con 8 frames objetivo.
- Benchmark dense/adaptive con cobertura local y web suficiente para establecer la
  línea base.

## Risks

- Algunas fuentes no ofrecen assets adicionales o los retiran dinámicamente.
- El coste y la latencia pueden crecer si el límite de candidatos no es estricto.
- Un timestamp de storyboard puede ser aproximado aunque el asset sea visualmente útil.
- El refinamiento puede mejorar el timestamp sin mejorar el Top-1 del vídeo; ambas
  métricas deben analizarse por separado.

## Historial de decisiones

- **2026-08-18**: El humano responsable confirmó una ronda completa de decisiones:
  refinamiento automático tras el primer pase; 3 candidatos por defecto y 5 como máximo;
  hasta 30 assets adicionales por candidato; conservar el resultado base si la evidencia
  visual refinada empeora; límite de 10 segundos por búsqueda y 3 segundos por candidato,
  sin persistir caché de imágenes.

## Approval

**Estado**: `APPROVED`.

**Aprobación humana**: `Especificación aprobada` — 2026-08-18.

Las decisiones funcionales están resueltas y esta especificación queda habilitada para
planificación técnica. La implementación seguirá requiriendo tareas `READY`, el flujo
`task-execution` y revisión independiente.
