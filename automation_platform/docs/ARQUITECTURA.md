# Arquitectura — dónde vive cada regla

Mapa entre la especificación y el código. Si una regla de la especificación no
aparece acá, o no está implementada o está mal ubicada.

## Recorrido de una ejecución

```
web/views.py            el operador aprieta "Ejecutar"
  └─ engine/runner.py   interruptor de parada → límite por hora → ¿aprobación?
       ├─ security/killswitch.py     organización / proceso / habilidad
       ├─ security/rate_limit.py     techo de corridas por hora (solo producción)
       ├─ engine/executors/…         computer_use | data_transform
       │    ├─ engine/conditions.py  precondición y poscondición (evaluador seguro)
       │    ├─ security/screen_content.py  el texto de pantalla es dato
       │    ├─ engine/drift.py       huella actual contra la certificada
       │    ├─ engine/selectors.py   cascada de 5 niveles + degradación
       │    └─ engine/validation.py  capas L1 a L5
       ├─ engine/cost.py             tokens y costo, medido o estimado
       └─ db/repositories.py         evidencia paso a paso, intervenciones, bitácora
```

## Dónde está cada regla no negociable

| Regla (CLAUDE.md) | Dónde vive |
|---|---|
| 1. Habilidades sin estado de pantalla | `engine/executors/computer_use.py`: verifica el ancla al empezar y al terminar; `engine/runner.py::_map_inputs` sólo deja pasar datos estructurados por las aristas |
| 2. Toda habilidad declara poscondición verificable | disparador `trg_publish_requires_postcondition`; `certification/certifier.py` exige además que sea **evaluable** |
| 3. Falla cerrada | `engine/executors/computer_use.py::_abort`; una condición que no se puede evaluar cuenta como no cumplida (`engine/validation.py`) |
| 4. El agente nunca obedece la pantalla | `security/screen_content.py` — clasifica y reporta, y no expone ninguna función que convierta texto en acción |
| 5. Escrituras irreversibles con aprobación previa | `engine/runner.py::run_skill` (pasada de vista previa) + disparador `trg_irreversible_requires_approval` |
| 6. Árbol de accesibilidad antes que píxeles | `engine/selectors.py`: la cascada está ordenada y el nivel 5 marca frágil |
| 7. Cambio de versión → revalidación | `engine/runner.py::_check_application_version` + `certification/certifier.py::recertify_application` |

## Las cuatro invariantes del modelo de datos

Están en `db/schema.sql` como disparadores, además de los chequeos que hace el
código antes con mejores mensajes:

- `trg_publish_requires_postcondition`
- `trg_publish_screen_budget` (junto al `CHECK (declared_screens BETWEEN 1 AND 3)`)
- `trg_publish_requires_certification`
- `trg_irreversible_requires_approval`
- `trg_audit_no_update` / `trg_audit_no_delete`

Y la quinta, "toda consulta filtra por organización", es estructural: cada tabla
declara `UNIQUE (organization_id, id)` y las hijas apuntan a la madre con clave
foránea compuesta, así que una fila no puede referenciar otra organización. Como
segunda línea, `db/database.py` rechaza cualquier sentencia que toque una tabla
de inquilino sin nombrar `organization_id`.

## El límite de 3 pantallas, en tres lugares

1. `CHECK (declared_screens BETWEEN 1 AND 3)` en la tabla.
2. `certification/certifier.py` cuenta las transiciones reales y bloquea.
3. `engine/executors/computer_use.py` corta la ejecución si una corrida cruza una
   cuarta pantalla: la habilidad en producción ya no es la que se certificó.

Y cuando una grabación se pasa del límite, `training/generalizer.py::split_plan`
propone cómo partirla. El producto ayuda a partir la tarea, no castiga por no
saber hacerlo.

## Qué reemplazar para llevarlo a producción

`engine/drivers.py` define `DesktopDriver`. En este repositorio la
implementación es `SimulatedDesktopDriver`, que maneja las aplicaciones de
`simulation.py`. Un despliegue real implementa las mismas cinco operaciones
contra UI Automation (Windows), AT-SPI (Linux) o AX (macOS) y no cambia nada
por encima de esa interfaz.

Lo mismo con el costo: `engine/cost.py` trae una tabla de precios de marcador de
posición, y todo lo que se derive de ella se informa como **estimado** hasta que
el operador cargue los precios reales.
