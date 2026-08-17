# Feature Specification: Muestreo adaptativo de frames

**Feature Branch**: `feature/005-adaptive-frame-sampling`

**Created**: 2026-08-17

**Status**: APPROVED

**Input**: User description: "Distribuir los frames a lo largo de cada vídeo según su
duración, usando menos en vídeos cortos y un máximo global de 8, para mejorar la
identificación y evitar timestamps muy alejados por falta de cobertura. La prueba debe
poder aplicarse de forma reproducible al corpus local y a los assets públicos permitidos
de proveedores reales."

## User Scenarios & Testing _(mandatory)_

### User Story 1 - Cobertura temporal proporcional (Priority: P1)

El operador quiere que los frames representativos cubran la duración completa de cada
vídeo de forma equilibrada, de modo que una captura intermedia no quede obligada a
coincidir con un único frame situado varios minutos antes o después.

**Why this priority**: Una distribución temporal insuficiente puede identificar el vídeo
correcto pero devolver un timestamp poco útil, que es el problema observado en la prueba
real.

**Independent Test**: Indexar vídeos de varias duraciones, inspeccionar sus conteos y
timestamps, y comprobar que la cantidad crece con la duración hasta el máximo y que los
puntos quedan repartidos por todo el vídeo.

**Acceptance Scenarios**:

1. **Given** dos vídeos con duración fiable y distinta, **When** se calcula su muestreo
   representativo, **Then** el vídeo corto recibe menos frames que el largo y ninguno
   supera el máximo global de 8.
2. **Given** un vídeo con duración fiable y al menos 8 imágenes permitidas utilizables,
   **When** se genera su índice base, **Then** sus 8 puntos representan intervalos
   distribuidos uniformemente desde el comienzo hasta el final del contenido útil.
3. **Given** una captura próxima al centro de un intervalo muestreado, **When** se busca
   en el corpus reindexado, **Then** el resultado conserva el vídeo correcto y devuelve
   el timestamp del frame representativo más próximo disponible.

---

### User Story 2 - Respetar los assets realmente disponibles (Priority: P1)

El operador quiere incorporar proveedores que ofrecen cantidades y tipos de assets
distintos sin fabricar frames, timestamps ni acceso al vídeo completo.

**Why this priority**: XTrace solo puede usar contenido legal y assets públicos
permitidos; la política de muestreo no puede forzar descargas o eludir límites de una
fuente.

**Independent Test**: Procesar una fuente que exponga menos imágenes utilizables que el
objetivo y comprobar que solo se indexan las disponibles, con su conteo y timestamps
reales o aproximados claramente conservados.

**Acceptance Scenarios**:

1. **Given** una fuente que solo ofrece menos assets permitidos que el objetivo, **When**
   se indexa el vídeo, **Then** se usan únicamente los assets disponibles y se conserva
   el conteo real.
2. **Given** assets duplicados o posiciones repetidas, **When** se prepara el muestreo,
   **Then** no se inflan artificialmente el conteo ni la cobertura temporal.
3. **Given** una fuente sin duración o posiciones fiables, **When** se indexan sus assets,
   **Then** se conserva la mejor información disponible sin inventar precisión temporal.

---

### User Story 3 - Reindexación local reproducible (Priority: P2)

El operador quiere repetir la prueba sobre todo el corpus actual —vídeos locales y vídeos
web ya admitidos— sin añadir contenido nuevo y sin depender de pasos manuales no
documentados.

**Why this priority**: La prueba local ya ha mostrado una mejora, pero debe poder
repetirse y verificarse antes de adoptar el comportamiento para catálogos mayores.

**Independent Test**: Ejecutar dos veces la reindexación del mismo corpus, comprobar que
el resultado es estable y que todos los vídeos terminan en un estado coherente sin
duplicar frames.

**Acceptance Scenarios**:

1. **Given** el corpus actual ya indexado, **When** el operador solicita una reindexación
   adaptativa completa, **Then** se recalculan sus frames sin añadir vídeos ajenos al
   corpus seleccionado.
2. **Given** una reindexación completada, **When** se repite con la misma entrada y
   política, **Then** los conteos y timestamps resultantes son equivalentes y no aparecen
   duplicados.
3. **Given** que un vídeo falla durante el proceso, **When** finaliza el lote, **Then** el
   fallo queda identificado sin dejar como correcto un índice parcial de ese vídeo ni
   impedir el procesamiento independiente del resto.

---

### User Story 4 - Comparar calidad y coste (Priority: P2)

El operador quiere decidir con evidencias si el índice base adaptativo mantiene la
capacidad de identificar vídeos y mejora la utilidad temporal frente al corpus anterior.

**Why this priority**: Reducir embeddings ahorra coste, pero no debe aceptarse si degrada
de forma material la identificación del vídeo.

**Independent Test**: Ejecutar un conjunto de consultas con origen y timestamp conocidos
sobre el muestreo adaptativo y sobre una referencia más densa, comparando identificación,
error temporal y frames procesados.

**Acceptance Scenarios**:

1. **Given** un conjunto representativo de consultas con verdad conocida, **When** se
   comparan las políticas, **Then** el informe presenta por separado acierto del vídeo,
   error temporal y número de frames.
2. **Given** resultados por varias fuentes y duraciones, **When** se genera el informe,
   **Then** se pueden detectar degradaciones ocultas por fuente o tramo de duración.
3. **Given** que el umbral de calidad no se cumple, **When** concluye la evaluación,
   **Then** la política no se declara lista para sustituir el comportamiento anterior.

### Edge Cases

- Vídeo de duración cero, negativa, ausente o incoherente.
- Vídeos extremadamente cortos que solo necesitan un frame representativo.
- Menos assets permitidos que el objetivo calculado.
- Storyboards sin timestamps explícitos o con posiciones desordenadas, repetidas o fuera
  de rango.
- Assets inaccesibles, corruptos, duplicados o no válidos como imagen.
- Duración conocida que cambia entre ingestas del mismo vídeo.
- Reindexación interrumpida, parcial o repetida.
- Vídeo eliminado o marcado como no permitido entre la ingesta y la reindexación.

## Requirements _(mandatory)_

### Functional Requirements

- **FR-001**: El sistema MUST calcular una cantidad de frames representativos proporcional
  a la duración del vídeo, con menos frames para vídeos cortos y más para vídeos largos.
- **FR-002**: El índice global base MUST usar como máximo 8 frames representativos por
  vídeo, conforme a ADR-0013.
- **FR-003**: Cuando exista duración fiable, los timestamps seleccionados MUST distribuirse
  uniformemente a lo largo del contenido útil del vídeo y evitar concentrarse en un solo
  tramo.
- **FR-004**: La política MUST producir al menos un frame cuando exista al menos un asset
  visual permitido y utilizable.
- **FR-005**: Si una fuente ofrece menos assets permitidos que el objetivo calculado, el
  sistema MUST indexar solo los disponibles y MUST conservar su conteo real.
- **FR-006**: El sistema MUST eliminar duplicados de contenido o posición antes de contar
  la cobertura representativa.
- **FR-007**: El sistema MUST NOT fabricar timestamps ni presentar una precisión mayor que
  la respaldada por la fuente.
- **FR-008**: La feature MUST aplicarse tanto a vídeos locales autorizados como a assets
  públicos permitidos de proveedores habilitados, sin descargar vídeos web completos de
  forma permanente.
- **FR-009**: El operador MUST poder reindexar un corpus seleccionado usando la política
  adaptativa de forma explícita, repetible e idempotente.
- **FR-010**: Una reindexación MUST reemplazar de forma coherente la representación previa
  de cada vídeo y MUST NOT dejar como válido un conjunto parcial cuando ese vídeo falla.
- **FR-011**: El procesamiento por lotes MUST aislar los fallos por vídeo y MUST informar
  conteos de completados, omitidos y fallidos.
- **FR-012**: El operador MUST poder comparar el muestreo adaptativo con una referencia
  más densa usando las mismas consultas conocidas.
- **FR-013**: La comparación MUST medir como mínimo acierto Top-1 y Top-5 del vídeo, error
  temporal absoluto, frames procesados y resultados segmentados por fuente y duración.
- **FR-014**: La política global adaptativa MUST NOT sustituir el comportamiento de
  referencia hasta que el benchmark cumpla los criterios de éxito de esta spec.
- **FR-015**: La consulta MUST continuar devolviendo el timestamp del mejor frame realmente
  indexado; el sistema MUST NOT interpolar un timestamp inexistente.

### Security and Compliance Requirements

- **SEC-001**: El muestreo MUST procesar únicamente contenido local autorizado y assets
  públicos permitidos por fuentes habilitadas.
- **SEC-002**: La feature MUST NOT eludir CAPTCHA, paywalls, DRM, autenticación, controles
  anti-bot ni restricciones declaradas por la fuente.
- **SEC-003**: La feature MUST NOT introducir reconocimiento facial ni análisis destinado
  a identificar personas.
- **SEC-004**: Los vídeos web completos MUST NOT almacenarse permanentemente como parte del
  muestreo o la reindexación.
- **SEC-005**: Una reindexación MUST NOT debilitar las políticas de acceso ni exponer la
  base de datos o los assets fuera de los entornos autorizados.

### Key Entities

- **Política de muestreo**: Reglas aprobadas que convierten duración y disponibilidad de
  assets en un objetivo de frames y una distribución temporal.
- **Frame representativo**: Imagen realmente disponible y utilizable asociada a un vídeo,
  con posición temporal y procedencia cuando sean conocidas.
- **Resultado de reindexación**: Estado por vídeo y resumen del lote, incluyendo frames
  efectivos, omitidos y fallos.
- **Caso de benchmark**: Consulta con vídeo de origen, fuente y timestamp de referencia
  conocidos.

## Success Criteria _(mandatory)_

### Measurable Outcomes

- **SC-001**: El 100 % de los vídeos reindexados tiene entre 1 y 8 frames efectivos cuando
  dispone de al menos un asset válido; ningún vídeo supera 8.
- **SC-002**: El 100 % de los vídeos con duración fiable y suficientes assets presenta
  frames distribuidos por toda su duración sin posiciones duplicadas.
- **SC-003**: Dos ejecuciones consecutivas sobre el mismo corpus producen los mismos
  conteos y timestamps representativos para el 100 % de los vídeos que no hayan cambiado.
- **SC-004**: El benchmark adaptativo pierde como máximo 5 puntos porcentuales de Top-5
  respecto a la referencia densa y mantiene al menos un 80 % de Top-5 global.
- **SC-005**: En el conjunto de validación temporal, el error absoluto mediano no supera
  la mitad del intervalo medio entre frames del vídeo evaluado.
- **SC-006**: El informe cubre como mínimo vídeos locales y una fuente web permitida, e
  incluye resultados separados para cada fuente y al menos tres tramos de duración.
- **SC-007**: El 100 % de los fallos individuales queda identificado y ningún vídeo fallido
  aparece como correctamente reindexado con un conjunto parcial.
- **SC-008**: La política adaptativa reduce al menos un 70 % los frames del índice base
  frente a una referencia fija de 30 frames por vídeo cuando existen suficientes assets.

## Assumptions

- ADR-0013 fija 8 como máximo del índice global; configuraciones más densas se usan solo
  como referencia de benchmark o refinamiento posterior.
- Los vídeos cortos usarán menos de 8 frames según umbrales definidos durante la
  planificación técnica y validados por los escenarios de esta spec.
- El corpus inicial de prueba es el ya autorizado: vídeos locales del operador y vídeos
  web previamente ingeridos desde fuentes habilitadas.
- La precisión temporal seguirá limitada por los assets que cada fuente permita obtener.
- El refinamiento temporal bajo demanda de candidatos pertenece a una feature posterior;
  esta feature prepara y valida únicamente el índice global base.

## Dependencies

- ADR-0013: índice global multi-proveedor con 8 frames por vídeo.
- Spec 001: pipeline de búsqueda visual y benchmark.
- Spec 002: adapters, assets visuales permitidos e ingesta web.
- Spec 003: búsqueda usable para la validación manual del operador.

## Approval

**Estado**: `APPROVED` — aprobada por el humano responsable el 2026-08-17 mediante la
frase exacta **`Especificación aprobada`**. Se habilitan la planificación técnica y la
descomposición en tareas.
