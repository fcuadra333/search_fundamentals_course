# Sistema de conciliaciones contables asistidas por IA

Trabajamos en español. Código y comentarios en inglés; interfaz y mensajes al
usuario final en español.

## Qué construimos

Una plataforma que ejecuta conciliaciones bancarias y de cuentas de balance
para firmas contables que administran carteras de clientes. Los agentes
resuelven excepciones y entregan trabajo listo para revisión humana.

**Comprador**: firma contable con 30 a 80 clientes en LatAm. No es la startup
que lleva sus propios libros.

**Especificación completa**: `docs/ESPECIFICACION.md`. Leerla antes de tomar
cualquier decisión de arquitectura o de modelo de datos.

## Reglas no negociables

1. **El sistema nunca escribe en el sistema contable del cliente.** Lee,
   propone, un humano aprueba. Integraciones de solo lectura siempre.
2. **Multitenencia desde la primera línea.** El modelo de datos es
   `firma → cliente → entidad → cuenta → período → partida`. Nunca
   `usuario → cuentas`. Esto no se puede agregar después sin reescribir.
3. **Todo emparejamiento registra su procedencia**: método, entradas,
   confianza, marca de tiempo y quién aprobó. Sin esto no hay papeles de
   trabajo, y los papeles de trabajo son el producto.
4. **Reproducibilidad.** Las mismas entradas producen la misma salida. La capa
   determinista es pura. La salida del modelo se persiste; nunca se vuelve a
   derivar en una segunda corrida del mismo período.
5. **Salida del modelo siempre con esquema validado.** Si no valida, se
   reintenta. Nada sin validar llega al usuario.
6. **Ninguna integración se construye para un solo cliente.** Se construye
   cuando tres clientes piden la misma, o cuando uno la paga como proyecto
   aparte.

## Arquitectura de la conciliación

Cinco etapas. El 85% nunca toca un modelo.

1. Ingesta y normalización de formatos
2. Cruce exacto determinista (monto + fecha + referencia)
3. Cruce difuso determinista (ventana de fechas, subcadena, agrupación N:1 y 1:N)
4. Modelo sobre el residuo, por lotes, con candidatos ya recuperados por código
5. Verificación aritmética determinista y cola de excepciones para revisión

El modelo resuelve el 15%, que es donde está el 100% del costo laboral que
reemplazamos. La calidad la garantiza el verificador, no la inteligencia del
modelo.

## Alcance de la versión 1

Solo estas cinco cosas:

- Modelo de datos multicliente
- Ingesta por archivo (Excel y CSV)
- Motor de conciliación de las cinco etapas
- Cola de excepciones con aprobar, anular y reasignar
- Exportación de papeles de trabajo

**Fuera de la v1**: multimoneda, cruce contra XML fiscal, tableros, ciclo de
aprendizaje automático, integraciones de ERP, portal de autoservicio,
facturación, conciliación intercompañía.

Criterio de admisión a la v1: solo entra lo que no se puede agregar después sin
reescribir.

## Modelos y proveedor

- Volumen y clasificación: modelo económico (GPT-5.6 Luna, $0,20/$1,20 por millón)
- Excepciones ambiguas y propuesta de asientos: modelo medio
  (Claude Sonnet 5, $2/$10, o GPT-5.6 Terra, $2/$12)
- No usar modelos frontier. No hay retorno medible en una tarea acotada y
  verificable.
- **La capa de proveedor va abstraída detrás de una interfaz.** Vamos a cambiar
  de modelo y de proveedor. Nada de llamadas directas dispersas por el código.
- Usar API por lotes donde el proveedor la ofrezca: la conciliación es trabajo
  nocturno, no interactivo. 50% de descuento.
- Contexto corto por llamada. Lotes de ~10K tokens. Nunca contextos largos:
  degradan la precisión, cuestan más y disparan el tiempo al primer token.

Costo de referencia estimado: ~$0,56 por entidad al mes, ~$0,28 con lotes,
contra un precio de venta de $299. El costo del modelo es irrelevante frente al
costo de equivocarse.

## Stack (decidido)

Python 3.11 · SQLAlchemy 2.0 sobre SQLite o PostgreSQL · FastAPI con Jinja2
renderizado en el servidor · cola de trabajos en tabla, sin broker · almacén de
archivos direccionado por contenido · openpyxl y reportlab para los papeles.
Sin build de frontend y sin CDN: la aplicación tiene que correr en la máquina
de un cliente sin internet. El detalle y el porqué de cada pieza están en
`docs/DISENO_TECNICO.md`.

## Convenciones

- Cuando un número de costo o rendimiento entre al código o a la documentación,
  indicar si está medido o estimado.
- Cada corrección que hace un contador se registra como dato, no se descarta. Es
  la semilla del ciclo de aprendizaje de la fase 2.
- Preferir mandar a la cola de excepciones antes que adivinar. Un falso positivo
  cuesta minutos de revisión; un emparejamiento incorrecto aprobado cuesta el
  cliente.
- El camino de corrección y anulación es función de primera clase, no un
  formulario secundario. Es la queja documentada del competidor más cercano.
