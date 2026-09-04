# Cuadre

Conciliaciones bancarias y de cuentas de balance para firmas contables que
administran carteras de clientes. Cinco etapas: el 85% se resuelve sin tocar un
modelo, el modelo trabaja sobre el 15% restante, y un verificador aritmético
decide si el resultado se entrega o vuelve a la cola.

El entregable no es la respuesta: son los papeles de trabajo.

- **Especificación de negocio**: [`docs/ESPECIFICACION.md`](docs/ESPECIFICACION.md)
- **Diseño técnico y decisiones**: [`docs/DISENO_TECNICO.md`](docs/DISENO_TECNICO.md)
- **Reglas no negociables**: [`CLAUDE.md`](CLAUDE.md)

## Levantarlo

```bash
pip install -r requirements-dev.txt

python scripts/seed_demo.py --reset     # firma de demostración con un mes real
PYTHONPATH=src python -m uvicorn cuadre.web.app:app --reload --port 8000
PYTHONPATH=src python -m cuadre.jobs.worker     # trabajo nocturno, en otra terminal
```

En <http://localhost:8000> con cualquiera de estos usuarios, contraseña
`cuadre2026`:

| Usuario | Rol | Puede |
|---|---|---|
| `socio@alvarez.ni` | Administrador | Administrar la firma y reabrir períodos |
| `preparador@alvarez.ni` | Preparador | Cargar archivos, conciliar, emparejar |
| `revisor@alvarez.ni` | Revisor | Aprobar conciliaciones y cerrar períodos |

Quien prepara no aprueba: la firma exige dos personas distintas.

## Verificarlo

```bash
python -m pytest -q                                     # 139 pruebas
python bench/run_bench.py --max-false-positive 0.02      # banco de regresión
```

El banco de regresión es requisito de construcción, no un extra. Corre 200
excepciones con la respuesta conocida contra cualquier proveedor y reporta tres
números: emparejamientos incorrectos (el que cuesta el cliente), tasa de
revisión manual (lo que percibe el cliente) y aciertos (para elegir modelo).

Línea base **medida** con el proveedor de control determinista, sin llamar a
ningún modelo: 90,0% de aciertos, 0,0% de emparejamientos incorrectos, 45,0% de
partidas enviadas a revisión manual. Ese 45% es el piso que un modelo real
tiene que bajar; la especificación pide llegar por debajo del 25%.

## Cómo está armado

```
src/cuadre/
  models.py          modelo relacional: firma → cliente → entidad → cuenta → período → partida
  tenancy.py         toda consulta filtra por firma; período cerrado no admite escrituras
  ingest/            etapa 1: leer Excel/CSV, proponer el mapeo, normalizar y guardar el perfil
  engine/
    stage2_exact.py  etapa 2: monto + fecha + referencia          (puro)
    stage3_fuzzy.py  etapa 3: ventana, subcadena, agrupación N:M  (puro)
    stage4_model.py  etapa 4: el modelo sobre el residuo, con candidatos ya recuperados
    stage5_verify.py etapa 5: la conciliación cuadra o no cuadra  (puro)
    runner.py        orquesta las cinco, persiste y arrastra partidas entre períodos
  llm/               una sola interfaz de proveedor: esquema validado, reintento, costo por llamada
  review.py          cola de excepciones, anulaciones, correcciones y aprobación
  workpapers/        Excel y PDF reconstruibles idénticos desde la base
  jobs/              cola de trabajos en base de datos, con reintentos
  web/               interfaz en español, sin build ni CDN
bench/               banco de regresión: 200 excepciones con respuesta conocida
```

## Configuración

Todo por variable de entorno; los valores por defecto sirven para desarrollo.

| Variable | Por defecto | Para qué |
|---|---|---|
| `CUADRE_DATABASE_URL` | SQLite en `var/` | PostgreSQL en producción |
| `CUADRE_SECRET_KEY` | de desarrollo | firma de la sesión — **cambiar** |
| `CUADRE_BLOB_DIR` | `var/blobs` | archivos originales, direccionados por hash |
| `CUADRE_LLM_PROVIDER` | `stub` | `anthropic`, `openai` o `stub` (sin red) |
| `CUADRE_MODEL_MID` | `claude-sonnet-5` | excepciones ambiguas |
| `CUADRE_MODEL_ECONOMY` | `claude-haiku-4-5` | volumen y clasificación |
| `CUADRE_BATCH_API` | `false` | usa la API por lotes: mitad de precio |

Sin claves configuradas el sistema corre completo con el proveedor de control
determinista: útil para la demostración, para las pruebas y para un despliegue
en las instalaciones del cliente sin salida a internet.
