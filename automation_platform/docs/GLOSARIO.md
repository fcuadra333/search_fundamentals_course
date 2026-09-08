# Glosario — español de la especificación, inglés del código

La especificación está escrita en español y el código en inglés. Esta tabla es
normativa: si aparece un concepto nuevo, se agrega acá **antes** de nombrarlo en
el código, y el nombre visible al usuario sale del catálogo de i18n, nunca del
identificador.

## Entidades

| Especificación | Código / tabla | Notas |
|---|---|---|
| organización | `organizations` | inquilino raíz; todo filtra por acá |
| usuario | `users` | roles abajo |
| área | `areas` | |
| puesto | `job_roles` | agrupa habilidades, define entradas/salidas y quién aprueba |
| aplicación | `applications` | software destino, con versión detectada y salud |
| habilidad | `skills` | la unidad reutilizable, ≤3 pantallas |
| pasos de habilidad | `skill_steps` | acción, cascada de selectores, valor esperado |
| proceso | `processes`, `process_nodes`, `process_edges` | grafo de habilidades |
| ejecución | `executions` | una corrida |
| ejecución de paso | `step_executions` | evidencia antes/después por acción |
| intervención | `interventions` | métrica de salud del sistema |
| aprobación | `approvals` | cola de escrituras irreversibles |
| demostración | `demonstrations` | grabación cruda, evidencia, no es la habilidad |
| propuesta generalizada | `skill_proposals` | requiere revisión humana |
| certificación | `certifications` | resultado de las cinco verificaciones |
| degradación de selector | `selector_degradations` | tres en una semana → revisión |
| detección de deriva | `drift_detections` | huella de pantalla contra la certificada |
| bitácora | `audit_log` | append-only, con disparadores que lo impiden alterar |

## Campos de habilidad

| Especificación | Código |
|---|---|
| ejecutor | `executor` |
| aplicación_id | `application_id` |
| ancla_inicio | `start_anchor` |
| parámetros_entrada | `input_schema_json` |
| salidas | `output_schema_json` |
| precondición | `precondition` |
| poscondición | `postcondition` |
| clasificación_riesgo | `risk_classification` |
| pantallas_declaradas | `declared_screens` |
| estado | `status` |
| versión | `version` |
| (verificación semántica L4) | `semantic_check` |

## Valores

| Especificación | Código |
|---|---|
| `computer_use` / `data_transform` / `api_call` / `llm_extract` | iguales |
| `solo_lectura` | `read_only` |
| `escritura_reversible` | `reversible_write` |
| `escritura_irreversible` | `irreversible_write` |
| `borrador` | `draft` |
| `en_certificacion` | `in_certification` |
| `publicada` | `published` |
| `requiere_revision` | `needs_review` |
| administrador / diseñador / operador / aprobador | `admin` / `designer` / `operator` / `approver` |
| medido / estimado | `measured` / `estimated` |

## Capas de validación

| Especificación | Código |
|---|---|
| L1 precondición | `ValidationLayer.L1_PRECONDITION` |
| L2 acción | `ValidationLayer.L2_ACTION` |
| L3 poscondición | `ValidationLayer.L3_POSTCONDITION` |
| L4 semántica | `ValidationLayer.L4_SEMANTIC` |
| L5 humana | `ValidationLayer.L5_HUMAN` |

## Cascada de selectores

| Nivel | Especificación | Código |
|---|---|---|
| 1 | identificador de automatización | `SelectorTier.AUTOMATION_ID` |
| 2 | rol y nombre accesible | `SelectorTier.ACCESSIBLE_ROLE_NAME` |
| 3 | texto visible de la etiqueta | `SelectorTier.VISIBLE_LABEL` |
| 4 | posición relativa a un ancla | `SelectorTier.RELATIVE_TO_ANCHOR` |
| 5 | coordenadas de píxel (marca frágil) | `SelectorTier.PIXEL_COORDINATES` |
