# Diseño técnico — Cuadre v1

Documento de decisiones. `docs/ESPECIFICACION.md` dice qué construimos y por
qué; esto dice cómo quedó construido y qué se decidió en cada bifurcación.

---

## 1. Stack (decisión abierta §8.1 de la especificación, ahora cerrada)

Los requisitos eran tres: base de datos relacional con transacciones reales,
trabajos en segundo plano con reintentos, y almacenamiento de archivos con
versionado. «Nada exótico».

| Pieza | Elección | Por qué |
|---|---|---|
| Lenguaje | Python 3.11 | Un solo operador. Excel, PDF y proveedores de modelos tienen bibliotecas maduras. |
| Base de datos | SQLAlchemy 2.0 sobre SQLite o PostgreSQL | El mismo esquema en las dos. SQLite para desarrollo y para el despliegue de un solo nodo; PostgreSQL en el producto alojado. |
| Web | FastAPI + Jinja2 | Renderizado en el servidor: sin build de frontend, sin CDN, sin nada que se rompa en la máquina de un cliente sin internet. La API para la fase 3 ya está en el mismo proceso. |
| Trabajos | Tabla `jobs` + worker propio | Sin broker. Una tabla con reclamo atómico basta para trabajo nocturno y deja el despliegue en un solo artefacto. |
| Archivos | Almacén direccionado por contenido | La clave es el SHA-256 del archivo. Versionado, deduplicación y el hash que el papel de trabajo tiene que citar, todo con la misma decisión. |
| Excel / PDF | openpyxl / reportlab | Sin binarios externos ni servicios de conversión. |

Ninguna dependencia se descarga en tiempo de ejecución. El proveedor de
modelos se importa de forma perezosa: sin claves, el sistema corre entero
contra el proveedor de control determinista.

## 2. Modelo de datos

La jerarquía de la especificación se implementa tal cual. Los nombres de tabla
están en inglés (CLAUDE.md: código en inglés); la correspondencia es directa:

| Especificación | Tabla |
|---|---|
| firmas · usuarios · clientes · entidades · cuentas | `firms` · `users` · `clients` · `entities` · `accounts` |
| periodos · cargas · perfiles_formato | `periods` · `uploads` · `format_profiles` |
| movimientos_banco · asientos | `bank_movements` · `ledger_entries` |
| emparejamientos · excepciones · correcciones · reglas | `matches` · `exceptions` · `corrections` · `rules` |

Dos tablas que la especificación implica sin nombrar:

- **`reconciliations`** — una cuenta bancaria dentro de un período. La jerarquía
  dice `cuenta → período → partida` mientras que `periodos` se describe por
  entidad; esta tabla es esa intersección, y es la que carga los saldos, el
  estado y el papel de trabajo.
- **`runs`** — una ejecución de las cinco etapas. Sin ella no se puede
  reproducir un período: la salida del modelo se guarda contra la corrida que
  la produjo y nunca se vuelve a derivar (regla 4).

### Decisiones de representación

**Montos en unidades mínimas enteras.** Nunca coma flotante. La etapa 5 compara
al centavo; con `float` la respuesta dependería de la máquina. El costo de los
modelos usa la misma idea: millonésimas de dólar, enteras.

**Un solo convenio de signo en los dos lados.** Positivo = el efectivo sube.
En el estado de cuenta un crédito sube; en el mayor un débito a la cuenta de
efectivo sube. La etapa 1 lleva ambos al mismo convenio, y por eso las etapas
2 y 3 pueden comparar los dos lados directamente en vez de tener dos rutas.

**Las columnas de enumeración devuelven el miembro, no la cadena.** Se guardan
como texto (mismo esquema en SQLite y PostgreSQL) pero con coerción de ida y
vuelta. Sin eso, un objeto recién leído de la base trae `str` y uno en memoria
trae el miembro: `estado is Estado.ABIERTA` sería verdadero o falso según de
dónde vino la fila.

### Invariantes y cómo se sostienen

| Invariante | Dónde vive | Prueba |
|---|---|---|
| Toda consulta filtra por firma | `tenancy.scoped()` / `get_scoped()` | `test_tenancy.py`, incluida una prueba que recorre el metadato y exige `firm_id` en toda tabla alcanzable |
| Un período cerrado no admite escrituras | `require_writable()` en cada ruta que escribe | `test_tenancy.py`, `test_review.py` |
| Un movimiento en un solo emparejamiento aprobado | `match_id` en la línea + verificación de la etapa 5 | `test_engine_deterministic.py` |
| Emparejamientos aprobados + excepciones abiertas = el período | Etapa 5 | `test_runner.py::test_every_line_ends_matched_or_in_the_queue` |

Un identificador de otra firma se reporta como «no encontrado»: que exista es
información que no le debemos a quien pregunta.

## 3. El motor

### Etapa 1 — ingesta

El problema técnico más difícil del producto. Detección por puntaje de dos
señales: el nombre del encabezado contra sinónimos, y el contenido de la
columna (¿parsea como fecha? ¿como monto?). Se propone, el usuario confirma una
vez, y queda un `format_profile` por banco y cliente. La segunda carga de ese
banco no pregunta nada.

Detalles que cuestan un cliente si se hacen mal:

- **Una columna vacía es «sin evidencia», no «columna mala».** Un extracto sin
  créditos en el período tiene la columna Créditos vacía; penalizarla rompe el
  par débito/crédito e invierte el signo de todo el archivo.
- **Los números de fila son los del archivo físico.** Las líneas en blanco se
  descartan antes de parsear; sin llevar la cuenta, un rechazo mandaría al
  usuario a mirar una fila que no es.
- **Nada se descarta en silencio.** Cada fila ilegible se rechaza con su motivo
  y se muestra.
- **Una referencia lleva dígitos.** Sin esa condición los patrones capturan la
  palabra siguiente: «TRANSFERENCIA ACH 4471» devolvía `ERENCIA`. Lo encontró
  el banco de regresión, no una revisión de código.

### Etapas 2, 3 y 5 — deterministas y puras

Sin sesión, sin reloj, sin red. Reciben listas de valores y devuelven listas de
valores. Se pueden probar sin base de datos y dos corridas dan lo mismo.

- El monto **nunca** tiene tolerancia. La fecha y la descripción sí. Una
  conciliación que acepta un monto aproximado no es una conciliación.
- El desfase de un día se aplica **a un solo lado**. Ensanchar los dos convierte
  ±1 día en ±2 sin que nadie lo note.
- Los empates se resuelven por identificador, de modo que el resultado no
  depende del orden en que la base devolvió las filas.
- La agrupación N:1 y 1:N es exhaustiva sobre un conjunto acotado por
  configuración: misma respuesta siempre, costo acotado.
- Cuando dos candidatos son igual de plausibles y nada los distingue, el motor
  **no elige**. Va a la cola.

La etapa 5 es la identidad de cuatro columnas:

```
saldo banco  + depósitos en tránsito − cheques en circulación   = ajustado banco
saldo libros + NC no registradas     − ND no registradas        = ajustado libros
```

Cuadra si los dos ajustados coinciden. La identidad se sostiene con partidas
arrastradas de períodos anteriores porque las partidas arrastradas entran a los
conjuntos de la etapa 5 igual que las del período: un cheque girado en enero y
cobrado en febrero se empareja cruzando el límite del período, y la excepción
de enero se cierra en ese momento.

### Etapa 4 — el modelo

Por lote: hasta quince excepciones, cada una con cinco a ocho candidatos ya
recuperados **por código**. El modelo nunca ve el libro completo. Lo que
devuelve se valida antes de convertirse en propuesta:

1. Los identificadores tienen que ser de la lista que le dimos.
2. Los dos lados tienen que sumar exactamente lo mismo.
3. Ninguna partida puede estar ya emparejada en el mismo lote.

Lo que no pasa se registra con su motivo y va a la cola. Todo emparejamiento
del modelo queda **propuesto**: lo aprueba una persona. Los emparejamientos
deterministas los aprueba el sistema — son reglas auditables y reversibles — y
la conciliación completa la firma el revisor de todas formas.

## 4. Capa de modelos

Una sola interfaz. Ningún módulo fuera de `llm/` importa un SDK de proveedor.

- Salida siempre contra un esquema cerrado (`additionalProperties: false`, todos
  los campos requeridos). Si no valida, se reintenta con el error en el prompt.
  Si se acaban los reintentos, el lote entero va a la cola: nunca una
  adivinanza.
- Tokens, costo, latencia, intentos y la respuesta cruda quedan en
  `model_calls`, una fila por llamada. El costo se calcula con la tarifa
  publicada de cada modelo, en millonésimas de dólar enteras.
- Ruteo de dos niveles, sin modelos frontier. La API por lotes se usa donde el
  proveedor la ofrece: la conciliación es trabajo nocturno y son 50% menos.
- El proveedor de control (`stub`) es determinista, gratis y no usa red. Es lo
  que corren las pruebas, la demostración y un despliegue en las instalaciones
  del cliente antes de conectar nada. También es el brazo de control del banco
  de regresión: lo que un modelo real cobre, tiene que ganárselo contra esto.

## 5. Papeles de trabajo

Excel con una hoja de conciliación por cuenta más las hojas de soporte, y un
PDF de resumen firmado y fechado. Cada exportación lleva:

saldos y la conciliación entre ellos · cada emparejamiento con método,
confianza, quién lo aprobó y cuándo · cada excepción con su antigüedad y el
período en que apareció · cada corrección humana con su motivo · el SHA-256 de
cada archivo de origen · el uso y costo de modelos.

Todo se deriva de la base y de nada más, así que una exportación hecha en
noviembre reproduce enero. La **huella** del papel es un hash sobre los saldos,
los emparejamientos y los hashes de origen: dos exportaciones del mismo período
aprobado dan la misma huella, y si se mueve, se movió el dato.

## 6. Corrección y aprendizaje

El camino de anulación es función de primera clase. Anular exige un motivo.
Cada anulación y cada emparejamiento manual escribe en `corrections` —qué
propuso el sistema, qué quedó, por qué— y nada de eso se descarta: es el dato
de entrenamiento del ciclo de la fase 2. Un emparejamiento manual 1:1 además
deriva una `rule` para ese cliente, que en la v1 **se registra y se muestra
pero no se aplica sola**.

## 7. Lo que quedó fuera, a propósito

Multimoneda, cruce contra XML fiscal, tableros analíticos, ciclo de aprendizaje
automático, integraciones de ERP, portal de autoservicio, facturación e
intercompañía. Criterio de admisión: solo entra a la v1 lo que no se puede
agregar después sin reescribir.

Lo que sí entró por ese criterio aunque no se venda todavía: multitenencia,
emparejamientos N:M, procedencia en cada emparejamiento, estado persistente por
partida y por período, memoria de reglas por cliente, y las herramientas como
funciones separadas en vez de un prompt monolítico. Con esa base, la
orquestación de la fase 3 es una suma; sin ella, es una reescritura.

## 8. Números

Todos los de rendimiento están **medidos** contra el mes de demostración y el
banco de regresión, con el proveedor de control. Ninguno viene de un modelo
real todavía.

| Número | Valor | Origen |
|---|---|---|
| Emparejamiento automático en el mes de demostración | 76,2% | medido |
| Aciertos del banco de regresión | 90,0% | medido, proveedor de control |
| Emparejamientos incorrectos | 0,0% | medido, proveedor de control |
| Partidas a revisión manual | 45,0% | medido, proveedor de control |
| Meta de la especificación para revisión manual | < 25% | objetivo, sin medir |
| Costo por entidad al mes | ~US$ 0,56 (~0,28 por lotes) | estimado en la especificación, sin medir |

Las tarifas de `claude-sonnet-5` y `claude-haiku-4-5` en `llm/pricing.py` son
tarifas publicadas. Las de los modelos GPT-5.6 vienen de la especificación y
están marcadas como estimadas en el propio código.

## 9. Pendiente antes de vender

Lo que dice la especificación: que una firma contable procese un mes real
completo y medir cuántas partidas terminaron en revisión manual. Si supera el
25%, hay que revisar las etapas 2 y 3 antes de seguir.

Además, en orden:

1. Reemplazar los 200 casos sintéticos del banco por excepciones reales
   anonimizadas, archivo por archivo.
2. Correr el banco contra `claude-sonnet-5` y contra el modelo económico y
   comparar contra la línea base del proveedor de control.
3. Migraciones con Alembic. La v1 crea el esquema directamente; el primer
   cliente en producción exige historial de migraciones.
4. Cifrado en reposo del almacén de archivos para el despliegue en las
   instalaciones del cliente.
