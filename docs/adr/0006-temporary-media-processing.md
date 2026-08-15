# 0006. Procesamiento temporal de media: no almacenar vídeos, cleanup garantizado

- **Estado**: Aceptada
- **Fecha**: 2026-08-14
- **Spec/Requisitos relacionados**: 001-visual-search-spike · FR-009, FR-018, privacidad (ASSUMPTION-6)

## Contexto

El activo del proyecto es el **índice visual**, no los vídeos. Almacenar vídeos completos
implica coste, riesgo legal y de privacidad. Además, la media que sube el usuario para
buscar es sensible.

## Decisión

- **No** almacenar de forma permanente los vídeos originales ni los frames físicos: el
  pipeline procesa a temporal → extrae frames → calcula pHash/embedding → **borra los
  temporales**. Se persisten solo metadatos, `pHash`, `embedding` y `timestamp`.
- Todo temporal se elimina en `try/finally`, incluso si el job falla (FR-009).
- La **media de consulta** se borra **inmediatamente** tras procesar la búsqueda; `searches`
  no almacena el contenido multimedia (FR-018, privacidad).
- Se añade un barrido periódico de temporales como salvaguarda (fuera del spike si no es
  necesario; documentado).

## Alternativas consideradas

- **Guardar frames/vídeos** para depurar — coste y riesgo legal/privacidad; innecesario
  para validar la hipótesis. Rechazada.

## Consecuencias

- (+) Coste y superficie legal/privacidad mínimos; alineado con compliance UE.
- (−) Depuración visual limitada; se compensa con logs, métricas y fixtures deterministas.
