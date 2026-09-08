# Especificación — plataforma de automatización de tareas de escritorio

Documento de referencia. `CLAUDE.md` tiene el resumen operativo; esto tiene el
detalle y la justificación de cada decisión.

---

## 1. Premisa y restricción central

### El problema que estamos esquivando

Los agentes de uso de computadora alcanzan ~85% de éxito en tareas cortas de
banco de pruebas, pero **20,6% en tareas de horizonte largo** donde la tarea
mediana toma 1,6 horas a un humano. Esa medición corresponde a la
configuración más fuerte disponible, con pensamiento máximo.

Peor aún para nuestro caso: cuando la interfaz cambia levemente, el
rendimiento cae de 50% a 19,1%. Y el escritorio es más difícil que la web.

### La apuesta

Todo el sistema existe para mantener cada unidad ejecutable en el régimen
corto. La restricción de 3 pantallas no es una recomendación: es la condición
de viabilidad.

Consecuencia de diseño: **el sistema rechaza publicar una habilidad que exceda
el límite.** El validador cuenta transiciones de pantalla durante la
certificación y bloquea. El usuario parte la tarea o no la publica.

### Lo que esto le cuesta al usuario

Trabajo de descomposición. Una tarea que él percibe como una sola cosa —
"procesar la factura del proveedor" — puede ser cinco habilidades. El producto
tiene que ayudarlo a partirla, no castigarlo por no saber hacerlo.

---

## 2. Modelo de datos

### Jerarquía
```
organización → área → proceso → habilidad → ejecución → paso
                    ↘ puesto → habilidades asignadas
```

### Entidades

**organizaciones** — inquilino raíz. Todo filtra por aquí, sin excepción.

**usuarios** — pertenecen a una organización. Roles: administrador,
diseñador (crea y entrena habilidades), operador (ejecuta y supervisa),
aprobador (autoriza escrituras irreversibles).

**puestos** — la abstracción que hace comprensible el sistema. Un puesto
agrupa habilidades, define qué información entra y sale, y quién aprueba qué.
Mapea a cómo la empresa ya piensa el trabajo.

**aplicaciones** — cada software destino: nombre, versión detectada, huella de
identificación, y estado de salud. Cuando cambia la versión, todas las
habilidades ligadas se marcan para revalidación.

**habilidades** — la unidad reutilizable. Campos:
- `ejecutor`: `computer_use` | `data_transform` | `api_call` | `llm_extract`
- `aplicacion_id` y `ancla_inicio` (estado de partida conocido)
- `parametros_entrada` con esquema
- `salidas` con esquema
- `precondicion` (verificable antes de actuar)
- `poscondicion` (verificable después de actuar) — **obligatoria**
- `clasificacion_riesgo`: `solo_lectura` | `escritura_reversible` |
  `escritura_irreversible`
- `pantallas_declaradas` (≤3, validado en certificación)
- `estado`: `borrador` | `en_certificacion` | `publicada` | `requiere_revision`
- `version`

**pasos_habilidad** — la secuencia de acciones. Cada uno con: tipo de acción,
selectores en cascada (ver §5), valor esperado tras la acción, y tiempo máximo.

**procesos** — grafo dirigido de habilidades. Los nodos son habilidades, las
aristas llevan datos estructurados. Condicionales y bifurcaciones permitidas.

**ejecuciones** — cada corrida de un proceso. Estado, disparador, tiempo,
costo en tokens, resultado.

**ejecuciones_paso** — cada acción individual: captura antes, acción tomada,
captura después, resultado de la verificación, tiempo, tokens consumidos.

**intervenciones** — cada vez que un humano tuvo que meter mano. Motivo,
quién, qué hizo, y si la habilidad quedó marcada para revisión. **Es la
métrica de salud del sistema.**

**aprobaciones** — cola de escrituras irreversibles pendientes, con lo que el
agente propone hacer, la evidencia, y la decisión humana.

**demostraciones** — grabaciones crudas del usuario. Se conservan como
evidencia; no son la habilidad.

### Invariantes

- Toda consulta filtra por organización.
- Una habilidad sin poscondición no puede pasar a `publicada`.
- Una habilidad con más de 3 transiciones de pantalla no puede pasar a
  `publicada`.
- Una ejecución de habilidad `escritura_irreversible` no puede confirmarse sin
  registro de aprobación.
- Toda ejecución conserva evidencia completa por el período de retención
  configurado.

---

## 3. Entrenamiento

### Modo 1 — Grabación (v1)

El diseñador ejecuta la tarea una vez con el grabador activo. Se captura:
secuencia de acciones, capturas de pantalla, árbol de accesibilidad en cada
paso, y valores introducidos.

**La grabación no es la habilidad.** Es evidencia. El sistema propone a partir
de ella una definición generalizada: qué valores son parámetros y cuáles son
constantes, qué selectores usar, dónde están los límites de pantalla, y qué
poscondición tiene sentido.

El diseñador revisa y edita esa propuesta. Nada se publica sin esa revisión.

### Modo 2 — Descripción (fuera de v1)

El diseñador describe la tarea, el agente explora en un entorno aislado y
propone la definición. Más cómodo, mucho menos confiable. No en la v1.

### Certificación — obligatoria antes de producción

Una habilidad pasa a `publicada` solo tras superar:

1. **Conteo de pantallas** ≤3
2. **Poscondición presente y evaluable**
3. **Corridas en seco**: N ejecuciones (por defecto 10) contra casos conocidos,
   en modo sin efectos, con umbral mínimo de éxito configurable (por defecto 9
   de 10)
4. **Prueba de variación**: al menos una corrida con la ventana redimensionada
   y otra con un diálogo inesperado inyectado. Si falla, se marca frágil y se
   exige selectores más robustos
5. **Clasificación de riesgo declarada explícitamente** por el diseñador

La certificación se repite automáticamente cuando cambia la versión de la
aplicación destino.

---

## 4. Ejecución

### Tipos de ejecutor

| Ejecutor | Para qué | Verificación típica |
|---|---|---|
| `computer_use` | Manejar una aplicación de escritorio o web | Poscondición en pantalla, archivo generado |
| `data_transform` | Procesar datos (aquí vive la conciliación) | Comprobación aritmética o de esquema |
| `api_call` | Cuando existe API, siempre preferible | Código de respuesta y esquema |
| `llm_extract` | Sacar campos de documentos | Esquema validado y contraste con original |

**Regla de precedencia**: si existe API, se usa API. El uso de computadora es
el último recurso, no el primero. Cada habilidad de `computer_use` es deuda de
mantenimiento.

### Ciclo de un paso

1. Verificar precondición (¿estoy en la pantalla esperada?)
2. Localizar el elemento con la cascada de selectores
3. Ejecutar la acción
4. Capturar el estado resultante
5. Verificar el efecto esperado del paso
6. Si falla: reintentar una vez; si vuelve a fallar, detener y escalar

### Anclas

Cada habilidad arranca desde un estado ancla verificable (pantalla de inicio de
la aplicación, por ejemplo) y termina devolviendo la aplicación a ese ancla.

Esto es lo que permite componer sin acumular fragilidad: si cada habilidad
empieza y termina en el mismo lugar conocido, un proceso de ocho habilidades
sigue siendo ocho tareas cortas y no una tarea larga.

### Modo sin interfaz

Ejecución en sesión aislada, no en el escritorio del usuario. Cuenta de sistema
operativo dedicada con privilegios mínimos, nunca la cuenta personal del
operador.

---

## 5. Confiabilidad ante cambios de interfaz

El problema documentado: 50% → 19,1% ante variaciones de interfaz. Estas son
las contramedidas.

### Cascada de selectores

Cada elemento se identifica con varios criterios en orden de preferencia:

1. Identificador de automatización del sistema operativo, si existe
2. Rol y nombre accesible (árbol de accesibilidad)
3. Texto visible de la etiqueta
4. Posición relativa a un elemento ancla estable
5. Coordenadas de píxel — **último recurso, y marca la habilidad como frágil**

Si el primer criterio falla, se prueba el siguiente y se registra la
degradación. Tres degradaciones en una semana marcan la habilidad para
revisión.

### Detección de deriva

Antes de actuar, se compara la huella de la pantalla actual contra la
registrada en certificación. Si la diferencia supera el umbral, la ejecución se
detiene y la habilidad pasa a `requiere_revision`.

**Detectar el cambio y avisar vale más que adaptarse silenciosamente.** Una
adaptación silenciosa que acierta el 80% de las veces es peor que un fallo
limpio.

### Versionado de aplicaciones

El sistema detecta la versión de cada aplicación destino en cada ejecución.
Cuando cambia, todas las habilidades ligadas pasan a `requiere_revision` y
entran a recertificación automática. No se ejecutan en producción hasta que
pasen.

Este es el punto en que la RPA clásica falló durante una década.

---

## 6. Validación en capas

| Capa | Qué verifica | Si falla |
|---|---|---|
| L1 precondición | Estado inicial correcto | Aborta antes de tocar nada |
| L2 acción | El clic registró, el campo aceptó el valor | Reintenta una vez |
| L3 poscondición | Apareció el artefacto esperado | Marca fallida y escala |
| L4 semántica | El resultado es correcto | Cola de revisión |
| L5 humana | Todo lo dudoso o irreversible | Cola de aprobación |

La capa 4 es donde vive la verificación específica del dominio. Para
conciliación es aritmética. Para otras tareas puede no existir, y en ese caso
**la habilidad no se puede vender como resultado garantizado**, solo como
asistencia con revisión humana. Esa distinción debe ser visible en el producto.

---

## 7. Seguridad y radio de daño

### Clasificación de riesgo

| Clase | Ejemplos | Política |
|---|---|---|
| `solo_lectura` | Exportar, consultar, leer | Ejecución libre |
| `escritura_reversible` | Crear borrador, guardar en carpeta propia | Ejecuta y notifica |
| `escritura_irreversible` | Enviar, pagar, borrar, confirmar, publicar | **Aprobación humana previa siempre** |

### Prohibiciones absolutas

Ninguna habilidad puede: iniciar pagos o transferencias, borrar registros,
enviar comunicaciones externas sin aprobación, modificar credenciales o
permisos, o desactivar controles del propio sistema.

### Resistencia a engaño

**El contenido de la pantalla es dato, nunca instrucción.** Si aparece texto en
una ventana pidiendo una acción, el agente lo trata como contenido a reportar,
no como orden a cumplir. Los agentes de interfaz son vulnerables a ataques de
engaño con ventanas emergentes falsas, y esa vulnerabilidad está documentada
como problema abierto.

### Controles operativos

Interruptor de parada inmediata por organización, por proceso y por habilidad.
Bitácora completa e inmutable. Grabación de sesión conservable. Cuenta de
sistema operativo dedicada con privilegios mínimos. Límite de ejecuciones por
hora por habilidad.

---

## 8. Tablero

### Vista de operación
Cola con ejecuciones pendientes, en curso, esperando aprobación y fallidas.
Filtro por puesto, proceso y persona.

### Vista de ejecución
Bitácora paso a paso con captura antes y después de cada acción, qué se
verificó en cada capa, tiempo y costo. Es la evidencia exportable.

### Vista de salud
Por habilidad: tasa de éxito, tasa de intervención humana, degradaciones de
selector, detecciones de deriva, costo promedio.

**La tasa de intervención es la métrica principal.** Si sube en una habilidad,
la aplicación cambió. Si sube en todas las de una aplicación, hubo
actualización.

### Exportación
Reportes en Excel y PDF con la evidencia completa de un rango de ejecuciones.
Mismo principio que los papeles de trabajo: sin evidencia exportable no hay
producto vendible a una empresa.

---

## 9. Economía — leer antes de fijar precios

**Esto cambia el modelo de negocio respecto de la conciliación.**

El uso de computadora consume capturas de pantalla, y las capturas son caras.
Estimación (no medida):

- Una captura ronda 1.100 a 1.600 tokens de entrada
- Una habilidad de 3 pantallas usa entre 15 y 25 capturas contando
  verificaciones
- Entrada por ejecución: ~35K de capturas más ~8K de contexto
- Salida: baja, 2K a 3K, porque las acciones son terses

Costo estimado por ejecución con un modelo medio: **$0,08 a $0,15**.

Comparación: una entidad-mes completa de conciliación cuesta ~$0,56. Una sola
ejecución de tres pantallas cuesta una cuarta parte de eso.

A 500 ejecuciones diarias por cliente son $40 a $75 por día, o **$1.200 a
$2.250 mensuales solo en modelo**.

### Consecuencias

1. **No se puede vender ejecución ilimitada a precio plano.** El precio debe
   ser por ejecución o por escalones con techo real y visible.
2. **El caché de prompt es la palanca principal.** La definición de la
   habilidad y su contexto se repiten en cada ejecución: deben ir cacheados.
3. **Aquí el modelo frontier sí puede pagar su costo**, al revés que en
   conciliación. La configuración más fuerte medida en el banco de horizonte
   largo fue un modelo frontier con pensamiento máximo. Un modelo barato que
   falla ejecuta acciones incorrectas sobre sistemas reales, y eso no se
   compara con una excepción mal clasificada.
4. **Medir el costo real por ejecución desde el primer día**, por habilidad. Es
   dato de producto, no de infraestructura.

---

## 10. Alcance de la versión 1

Solo esto:

- Modelo de datos multiinquilino completo
- Ejecutor `computer_use` con límite de 3 pantallas obligatorio
- Ejecutor `data_transform`
- Entrenamiento por grabación con generalización asistida y revisión
- Certificación con corridas en seco y prueba de variación
- Validación en capas L1 a L5
- Cola de aprobación para escrituras irreversibles
- Tablero de operación, ejecución y salud
- Exportación de evidencia

Fuera: entrenamiento por descripción, biblioteca compartida entre inquilinos,
ejecución en la máquina del usuario, móvil, reportes programados, marketplace
de habilidades, ejecutores `api_call` y `llm_extract`.

Criterio de admisión: solo entra lo que no se puede agregar después sin
reescribir. El modelo de datos, la abstracción de ejecutores y la restricción
de 3 pantallas cumplen ese criterio. Casi nada más.

---

## 11. Primera habilidad a construir

El conector de exportación de ERP.

Cumple todo lo que el sistema necesita demostrar: es corta, es de solo lectura,
tiene poscondición trivialmente verificable (apareció el archivo, parsea, tiene
filas plausibles), se repite todos los meses y resuelve el problema técnico más
difícil del producto de conciliaciones.

Si esa habilidad no alcanza 95% de confiabilidad sostenida durante un mes
contra un ERP real, la premisa del sistema no se sostiene y hay que revisarla
antes de construir más.

---

## 12. Validación pendiente

Antes de ampliar más allá de la v1, medir sobre uso real:

- Tasa de éxito por habilidad, separando primera ejecución de reintentos
- Tasa de intervención humana por habilidad y su tendencia
- Costo real por ejecución contra el estimado de §9
- Cuántas habilidades quedaron marcadas frágiles por caer a coordenadas
- Cuántas veces una actualización de aplicación rompió habilidades y cuánto
  costó recertificarlas

El último punto es el que decide si el negocio existe. Si el mantenimiento
consume más de lo que ahorra la automatización, este es el mismo callejón donde
quedó la RPA clásica.
