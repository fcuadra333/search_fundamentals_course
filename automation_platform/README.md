# Plataforma de automatización de tareas de escritorio

Un operador de empresa define, entrena y ejecuta las tareas repetitivas de su
trabajo diario. Los agentes corren sin interfaz visible; las personas supervisan
desde un tablero y aprueban lo irreversible.

La interfaz es **bilingüe español / inglés** y el modelo de datos es
**multiinquilino**: cada organización ve sólo lo suyo, y eso lo garantiza el
esquema, no la buena voluntad de cada consulta.

- Resumen operativo y reglas no negociables: [`CLAUDE.md`](CLAUDE.md)
- Especificación completa: [`docs/ESPECIFICACION.md`](docs/ESPECIFICACION.md)
- Dónde vive cada regla: [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md)
- Español de la especificación ↔ inglés del código: [`docs/GLOSARIO.md`](docs/GLOSARIO.md)

## La restricción que define todo

**Ninguna habilidad puede abarcar más de 3 pantallas.**

Los agentes de uso de computadora logran ~85% en tareas cortas y 20,6% en tareas
de horizonte largo. Todo el diseño existe para mantener cada unidad ejecutable
del lado corto de esa línea. El sistema **rechaza publicar** una habilidad que
exceda el límite, y cuando una grabación se pasa, el producto propone cómo
partirla.

## Cómo probarlo

```bash
make install     # única dependencia: Flask
make demo        # base de ejemplo con dos inquilinos, ya certificada y ejecutada
make run         # tablero en http://localhost:4000
make test        # 147 pruebas
```

La base de demostración trae dos organizaciones que no comparten nada:

- **Acme Servicios Contables** (español): concilia el mayor del ERP contra el
  extracto bancario y envía el informe. Cuatro habilidades, un proceso de tres
  nodos, y una escritura irreversible esperando aprobación.
- **Beta Field Services** (inglés): exporta el informe semanal de tickets.

Entrá con cualquier usuario de la lista; el rol cambia lo que podés hacer
(publicar es del diseñador, aprobar es del aprobador). El selector `ES / EN`
arriba a la derecha cambia el idioma de todo, incluidas las exportaciones.

## Qué hay adentro

| Pieza | Dónde | Qué resuelve |
|---|---|---|
| Modelo multiinquilino | `db/schema.sql`, `db/repositories.py` | aislamiento por esquema, invariantes por disparador |
| Ejecutor de uso de computadora | `engine/executors/computer_use.py` | ciclo de paso, anclas, límite de 3 pantallas |
| Ejecutor de transformación | `engine/executors/data_transform.py` | acá vive la conciliación, con verificación aritmética |
| Cascada de selectores | `engine/selectors.py` | 5 niveles, degradación registrada, píxeles marcan frágil |
| Detección de deriva | `engine/drift.py` | la pantalla cambió: detener y avisar, nunca adaptarse callado |
| Validación en capas | `engine/validation.py` | L1 a L5 con la política de falla de cada una |
| Certificación | `certification/certifier.py` | pantallas, poscondición, 10 corridas en seco, prueba de variación, riesgo declarado |
| Entrenamiento por grabación | `training/` | la grabación es evidencia; la propuesta la revisa una persona |
| Seguridad | `security/` | riesgo, prohibiciones absolutas, pantalla como dato, paradas, límite por hora |
| Costo | `engine/cost.py` | tokens por ejecución, siempre marcados medido o estimado |
| Evidencia exportable | `export/` | Excel y PDF reales, sólo con biblioteca estándar |
| Tablero | `web/` | operación, ejecución con evidencia paso a paso, y salud |

## Lo que no es

El controlador de escritorio real no está: `engine/drivers.py` define la
interfaz y `simulation.py` trae aplicaciones guionadas que la implementan, que
es lo que usan las corridas en seco, la certificación, la demostración y las
pruebas. Un despliegue real implementa las mismas cinco operaciones contra UI
Automation, AT-SPI o AX y no cambia nada por encima de esa interfaz.

Fuera del alcance de la v1, por decisión de la especificación: entrenamiento por
descripción en lenguaje natural, biblioteca compartida entre inquilinos,
ejecución en la máquina del usuario, móvil, reportes programados, marketplace, y
los ejecutores `api_call` y `llm_extract` — que existen en el modelo de datos
porque la abstracción tenía que estar bien desde el principio, pero pedirlos
levanta un error en lugar de fingir.

## Antes de fijar precios

Leer §9 de la especificación. Una ejecución de tres pantallas cuesta entre
US$ 0,08 y 0,15 estimados, así que no se puede vender ejecución ilimitada a
precio plano. La plataforma mide el costo real por ejecución desde el primer
día, y cada número que muestra dice si está **medido** o **estimado**.
