# Especificación — sistema de conciliaciones contables

Documento de referencia. `CLAUDE.md` tiene el resumen operativo; esto tiene el
detalle y el porqué de cada decisión.

---

## 1. Contexto de negocio

### Operador
Fernando, en Nicaragua. S-LLC en Nuevo México con EIN, útil para Stripe y para
contratar en dólares.

### Problema que resolvemos
Una conciliación bancaria de 8 cuentas consume entre 3 y 5 días de un analista
al mes. El emparejamiento por reglas resuelve el 85% desde hace veinte años y
está en todos los ERP. **El analista gasta sus días íntegros en el 15% restante.**
Ese 15% es el producto.

### Comprador
Firma contable o BPO contable con 30 a 80 clientes, en LatAm. Se elige ese
comprador porque una sola venta cubre decenas de entidades y porque la firma ya
tiene la relación de confianza con el cliente final.

La empresa mediana individual es comprador secundario, con el mismo producto.

### Precio de referencia

| Plan | Alcance | Precio/mes |
|---|---|---|
| Starter | 1 entidad, hasta 3 cuentas | $99 |
| Business | 1 entidad, hasta 12 cuentas | $299 |
| Firma | hasta 15 entidades | $890 |
| Firma Plus | hasta 50 entidades | $2.400 |

Anclaje de venta: el trabajo que reemplaza cuesta ~$250 mensuales de salario a
$8/hora. No se cobra por transacción individual (factura impredecible, castiga
al mejor cliente); se cobra por escalones de volumen con factura plana dentro
del escalón.

### Por qué no competimos de frente
Basis levantó $138M y atiende al 30% de las 25 firmas más grandes de EE.UU.
Puzzle tiene ~$50M y 7.000 clientes. No se les gana en ingeniería de agentes ni
en capital.

El hueco real y documentado por reseñas independientes:
- Basis no cierra retorno para firmas que facturan menos de $1M anuales
- Puzzle es solo EE.UU., sin multimoneda, con integraciones bancarias limitadas
  fuera del ecosistema fintech estadounidense, y no está diseñado para gestión
  de práctica multicliente
- Ninguno resolvió los papeles de trabajo auditables

Nuestra ventaja es la ingesta de datos que ellos no pueden leer: bancos sin
feed automatizado, formatos locales, multimoneda, español.

---

## 2. Modelo de datos

La decisión más costosa de revertir. Se define completa antes de escribir
cualquier otra cosa.

### Jerarquía
```
firma → cliente → entidad → cuenta → período → partida
```

Una firma tiene muchos clientes. Un cliente puede tener varias entidades
legales. Una entidad tiene cuentas bancarias y cuentas contables. El trabajo se
organiza por período.

### Tablas principales

**firmas** — el inquilino raíz. Todo consulta filtrado por aquí.

**usuarios** — pertenecen a una firma, con rol: administrador, preparador,
revisor. El revisor es quien puede aprobar.

**clientes** — pertenecen a una firma.

**entidades** — pertenecen a un cliente. Moneda funcional, catálogo de cuentas,
calendario de cierre.

**cuentas** — bancarias y contables, pertenecen a una entidad. Para las
bancarias: banco, número, moneda, y el `perfil_formato` aprendido.

**periodos** — por entidad. Mes y año, con estado: abierto, en proceso, en
revisión, cerrado. Un período cerrado es inmutable.

**cargas** — cada archivo subido. Tipo (estado de cuenta, mayor, auxiliar),
hash del contenido, quién lo subió, cuándo, y el perfil de formato aplicado. El
hash detecta recargas del mismo archivo.

**movimientos_banco** — líneas del estado de cuenta ya normalizadas. Fecha,
monto, signo, descripción cruda, referencia extraída, carga de origen.

**asientos** — entradas del mayor o auxiliar, misma estructura.

**emparejamientos** — el corazón. **Debe ser N:M**, no 1:1. Un depósito puede
cancelar cinco facturas; un pago puede ser parcial sobre dos. Un modelo 1:1
falla en la primera semana con datos reales y es caro de arreglar después.

Campos: conjunto de movimientos, conjunto de asientos, método (exacto, difuso,
modelo, manual), confianza, marca de tiempo, estado (propuesto, aprobado,
rechazado), quién aprobó y cuándo.

**excepciones** — lo que no se emparejó. Estado, antigüedad en días, revisor
asignado, motivo, y arrastre entre períodos. Una partida sin conciliar en marzo
sigue viva y rastreada en junio.

**correcciones** — bitácora de cada anulación humana sobre una propuesta del
sistema. Qué propuso, qué corrigió el humano, por qué. **Nunca se descarta**:
es el dato de entrenamiento del ciclo de aprendizaje de la fase 2.

**reglas** — reglas de emparejamiento por cliente, derivadas de correcciones.
En v1 se registran pero no se aplican automáticamente.

**perfiles_formato** — mapeo de columnas aprendido por banco y por cliente. Se
confirma una vez y se reutiliza.

### Invariantes
- Toda consulta filtra por firma. Sin excepción.
- Un período cerrado no admite escrituras.
- Un movimiento solo puede estar en un emparejamiento aprobado a la vez.
- Sumatoria de emparejamientos aprobados más excepciones abiertas debe igualar
  el total del período. Esta es la verificación de la etapa 5.

---

## 3. Motor de conciliación

### Etapa 1 — Ingesta y normalización
Entrada: Excel o CSV. Detección de columnas asistida por modelo, propuesta al
usuario, confirmación una sola vez, y persistencia en `perfiles_formato`.

Las cargas siguientes de ese banco y cliente son automáticas. **Este es el
problema técnico más difícil del producto**, más que la conciliación en sí. Si
el usuario sube un archivo y el sistema no lo entiende, se va y no vuelve.

Normalización: fechas, separadores decimales, signos, monedas, referencias.

### Etapa 2 — Cruce exacto (determinista)
Monto idéntico, fecha idéntica o dentro de un día, referencia coincidente.
Resuelve ~75%. Costo cero. Debe ser una función pura y testeable.

### Etapa 3 — Cruce difuso (determinista)
Monto exacto con ventana de fechas de ±5 días. Referencia como subcadena.
Agrupación N:1 y 1:N por suma de montos. Resuelve ~10% adicional. Costo cero.

### Etapa 4 — Resolución de excepciones (modelo)
Sobre el residuo, ~15%.

Por cada lote: 15 excepciones con 5 a 8 candidatos cada una, **recuperados por
código**, más las reglas del cliente y ejemplos históricos. El modelo elige o
declara que no hay coincidencia. Nunca se le pasa el libro completo.

Salida en JSON con esquema estricto: identificadores de movimiento y asiento,
confianza, y justificación en una frase. Validación de esquema con reintento.

Presupuesto: ~10K tokens por llamada. Contexto corto por diseño.

### Etapa 5 — Verificación (determinista)
La conciliación cuadra o no cuadra. Es aritmética, no criterio.

Si no cuadra, se reintenta la etapa 4 sobre lo que falló. Hasta tres pasadas.
Lo que sobrevive va a la cola de excepciones.

**Esta etapa es la que permite vender un resultado garantizado con un modelo
mediano.** No se elimina ni se hace opcional.

### Etapa 6 — Cola de excepciones
Lo que queda para el humano. Ordenada por monto y antigüedad. Cada partida
muestra los candidatos que el sistema consideró y por qué los descartó.

Acciones: aprobar, anular con emparejamiento manual, marcar como partida
conciliatoria legítima, diferir al período siguiente.

Toda anulación escribe en `correcciones`.

---

## 4. Papeles de trabajo

El entregable real. En contabilidad la respuesta no es el producto: **la
evidencia lo es.** Un auditor externo pide papeles de trabajo, y una
transcripción de chat no lo es.

Cada exportación incluye, por período y entidad:
- Saldo según banco, saldo según libros, y la conciliación entre ambos
- Cada emparejamiento con su método, confianza, quién lo aprobó y cuándo
- Cada excepción abierta con su antigüedad y explicación
- Cada corrección humana sobre una propuesta del sistema
- Hash de los archivos de origen

Formato: Excel con una hoja por cuenta, más un PDF de resumen firmado con
fecha. Debe poder reconstruirse idéntico desde la base en cualquier momento
posterior. De ahí la regla de reproducibilidad.

---

## 5. Capa de modelos

### Enrutamiento
- Clasificación y casos simples: modelo económico
- Excepciones ambiguas y propuestas de asiento: modelo medio
- Sin modelos frontier

### Interfaz obligatoria
Toda llamada pasa por una interfaz única con: modelo, esquema de salida,
política de reintento, y registro de tokens y costo por llamada. Vamos a
cambiar de proveedor. El resto del código no debe enterarse.

### Por lotes
La conciliación corre de noche. Donde el proveedor ofrezca API por lotes, se
usa: 50% de descuento, y la latencia no importa.

### Banco de pruebas de regresión
**Requisito de construcción, no opcional.** 200 excepciones reales con la
respuesta correcta conocida. Se corre contra cualquier endpoint o modelo
candidato y mide tasa de acierto.

Sirve para tres cosas: elegir modelo, detectar degradación cuando un proveedor
cambia su cuantización sin avisar, y validar que una migración a hardware
propio no bajó la calidad.

La métrica que no puede moverse entre despliegues no es el modelo: **es la tasa
de partidas enviadas a revisión manual.** Eso es lo que el cliente percibe.

---

## 6. Ingesta y despliegue

### v1: solo archivos
Carpeta vigilada o carga web. Cero acceso al ERP del cliente, cero objeción de
seguridad, cero semanas de integración. El midmarket latinoamericano vive en
Excel.

### Fase 2: iniciativa del agente
El quiebre hacia orquestación autónoma no es una API, es quién arranca el
proceso. El agente despierta el día 3 del cierre, mira la carpeta, y si falta
el estado de cuenta manda el correo pidiéndolo. Si al día 5 sigue faltando,
escala.

Eso requiere una sola integración de correo, no integraciones de ERP.

### Fase 3: agente conector
Programa pequeño en la máquina del cliente que exporta por calendario y empuja
los archivos. Más barato que una integración por API, funciona con ERP locales
sin API, y el área de sistemas lo acepta más fácil que abrir el ERP a internet.

### Despliegue on-premise
Para el cliente que exige privacidad total. Un solo artefacto: un contenedor
con modelo, aplicación y verificador, idéntico en todas partes. Si un cliente
necesita algo que la imagen no hace, entra al producto para todos o no entra.

Piso de precio: $800 mensuales. Por debajo, la fragmentación operativa come el
margen.

Modelo abierto para ese caso: Qwen3.8-27B en INT8 W8A16, no en 4 bits. La
fidelidad medida de ese checkpoint es 0,000641 de KLD y 99,19% de coincidencia.
Corre en 2× RTX 3090 o en una RTX PRO 6000 de 96GB.

Requisito contractual: el cliente envía métricas agregadas (tasa de
emparejamiento, excepciones, reintentos) aunque nunca veamos un dato. Sin eso
nos enteramos de una degradación cuando cancela.

---

## 7. Hoja de ruta

**Peldaño 1 (v1)** — Resolución de excepciones por lote. Seguro, vendible,
poco agéntico. Paga las cuentas y valida el mercado.

**Peldaño 2** — Recuperación de documentos de respaldo y ciclo de aprendizaje
por cliente. Mes 1: 15% de excepciones. Mes 6: 4%. **Ahí está el foso**: un
competidor que arranca mañana está en 15% mientras nosotros estamos en 4%.

**Peldaño 3** — Orquestación autónoma del cierre. No "conciliá las cuentas"
sino "cerrá el mes": junta datos, concilia, detecta devengos faltantes,
persigue por correo a los humanos que deben información, arma asientos de
ajuste y reporta estado.

Se vende el peldaño 1, pero se construye la infraestructura del peldaño 3
desde ahora: estado persistente por partida y por período, memoria de reglas
por cliente, y herramientas como funciones separadas en vez de un prompt
monolítico. Con esa base, migrar es agregar orquestación. Sin ella es
reescribir.

---

## 8. Decisiones abiertas

- Stack técnico. Requisitos: base de datos relacional con transacciones reales,
  trabajos en segundo plano con reintentos, almacenamiento de archivos con
  versionado. Nada exótico.
- Proveedor de modelos: directo con cada laboratorio o vía agregador. Si es
  agregador, fijar precisión a fp8 o int8 y usar el modo de ruteo de mayor
  precisión en llamadas a herramientas.
- Pasarela de pago: Tilopay para Centroamérica, dLocal Go para el resto de
  LatAm, Stripe para B2B en dólares. Las tarjetas corporativas latinoamericanas
  rechazan cobros internacionales, así que la adquirencia local no es
  alternativa a Stripe sino complemento.

## 9. Validación pendiente

Antes de construir más allá de la v1: conseguir que una firma contable procese
un mes real completo con el sistema y medir cuántas partidas terminaron en
revisión manual. Si supera el 25%, la propuesta de valor no se sostiene y hay
que revisar las etapas 2 y 3 antes de seguir.
