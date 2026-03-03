# Esquema del directorio de proyectos (Clockify)

Archivo: `Beeckario/directorios/clockify_proyectos.xlsx`

## Columnas

| Columna | Tipo | Obligatoria | Descripción |
|---|---:|:---:|---|
| Proyecto | string | Sí | Código/nombre del proyecto (ej. `NYB.045`, `AER.MCC.004`). Se usa para resolver el `projectId`. |
| ID | string | Sí | `projectId` de Clockify (24 hex). |
| Cliente | string | No | Se usa para construir la descripción final con el template. |
| Facturable | Si/No | No | Default de `billable` si el usuario no lo especifica. |
| ID_Discovery | string | No | `taskId` para etapa *discovery* en este proyecto. |
| ID_Desarrollo | string | No | `taskId` para etapa *desarrollo* (default si no se especifica). |
| ID_Deployment | string | No | `taskId` para etapa *deployment*. |
| Farming | string | No | `taskId` para preventa tipo *farming*. |
| Hunting | string | No | `taskId` para preventa tipo *hunting*. |

## Reglas de uso

- Si el usuario no especifica facturable, Beeckario toma `Facturable` del Excel.
- Si el usuario no especifica etapa (task), Beeckario asume **Desarrollo** y toma `ID_Desarrollo`.
- Para preventa, Beeckario necesita saber si es **Farming** o **Hunting**; si el texto dice solo “preventa” y existen ambos, preguntará.

## Buenas prácticas
- Mantén el código `Proyecto` consistente (usa `AER.MCC.004` en lugar de variantes).
- Los IDs deben ser los IDs reales de Clockify (24 hex).
- Evita duplicados exactos de `Proyecto` para no volver ambiguo el match.
