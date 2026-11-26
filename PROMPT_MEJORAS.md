# PROMPT MAESTRO: REFACTOR, MEJORAS Y ARQUITECTURA CLOUD PARA BUSCADOR DE VUELOS

> **INSTRUCCIONES PARA LA IA:**  
> Actúa como un **Ingeniero de Software Senior y Arquitecto Full-Stack**. Tu tarea es refactorizar, modernizar y desplegar el proyecto `buscador-vuelos`. Sigue estrictamente los lineamientos técnicos, la estructura de carpetas propuesta y los criterios de aceptación detallados a continuación. No omitas código ni utilices placeholders incompletos.

---

## 1. CONTEXTO Y DIAGNÓSTICO DEL PROYECTO ACTUAL

El repositorio actual es un comparador de vuelos automatizado escrito en Python que consulta precios mediante scraping con `fast-flights` (Google Flights), guarda el historial en SQLite y genera un reporte HTML interactivo con `Chart.js`.

### Archivos actuales en la raíz del proyecto:
- `buscar_vuelos.py`: Script monolítico (CLI con `argparse`, motor de consultas, base de datos SQLite, generador de reporte).
- `config.yaml`: Archivo de configuración con parámetros de búsqueda (`salida_desde`, `salida_hasta`, aeropuertos, alertas).
- `plantilla_reporte.html`: Template HTML con JS inyectado que renderiza el dashboard y gráficos de `Chart.js`.
- `historial.db`: Base de datos SQLite con tablas `corridas` y `precios`.
- `estado.json`: Guarda la fecha de la última corrida diaria.
- `vuelos.log`: Registro de eventos de ejecución y errores.
- `ejecutar_ahora.bat`, `instalar.bat`, `instalar.ps1`, `quitar_del_inicio.bat`: Automatización local para Windows.
- `requirements.txt`: Dependencias (`fast-flights`, `pyyaml`).

---

## 2. PROBLEMAS DETECTADOS QUE DEBES RESOLVER

1. **Límites de fechas ineficientes:**  
   Actualmente barre ventanas genéricas como `salida_desde: "+20"` a `salida_hasta: "+330"`, disparando cientos de consultas para meses donde el usuario no tiene vacaciones. Se necesita un rango estricto configurable (ej. solo Enero y Febrero).
2. **Los reportes se sobreescriben:**  
   Si se ejecuta `--busqueda "China"`, la función `generar_reporte()` sobreescribe `reporte.html` únicamente con los datos de "China", borrando los resultados visuales de "Cipolletti" o cualquier otra búsqueda previa.
3. **Reporte HTML rígido y limitado:**  
   - Las tablas no permiten ordenar por columnas al hacer clic (precio, fecha, escalas, duración).
   - No hay botón para disparar una re-búsqueda o actualización desde la UI.
   - El dashboard solo muestra el mejor precio y el mínimo histórico, pero carece de estadísticas clave: **Precio Mínimo, Precio Máximo y Precio Promedio**.
4. **Desorden de archivos en la raíz:**  
   Scripts, configuraciones, base de datos, logs y plantillas están mezclados en el directorio raíz.
5. **Dependencia de PC encendida y scripts `.bat`:**  
   Se requiere una solución gratuita en la nube con interfaz web accesible, ejecución automática diaria y notificaciones inteligentes por correo electrónico.

---

## 3. ESPECIFICACIÓN TÉCNICA DE LOS REQUERIMIENTOS

---

### MÓDULO 1: REORGANIZACIÓN Y REFACTOR ESTRUCTURAL
Reorganiza el proyecto sin romper ninguna ruta relativa o absoluta:

```text
buscador-vuelos/
│
├── config/
│   └── config.yaml               # Configuración centralizada de búsquedas
│
├── data/
│   ├── historial.db              # Base de datos persistente
│   └── estado.json               # Control de última ejecución
│
├── logs/
│   └── vuelos.log                # Registro de actividad
│
├── reportes/
│   ├── index.html                # Dashboard maestro con todas las búsquedas consolidadas
│   └── [nombre_busqueda].html    # Reportes independientes por búsqueda (opcional/enlazado)
│
├── src/
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── paths.py              # Definición centralizada de rutas absolutas con pathlib
│   │   ├── config.py             # Validador y cargador de config.yaml
│   │   ├── database.py           # Gestor SQLite (migraciones, inserción, consultas)
│   │   └── flight_searcher.py    # Motor de consultas a Google Flights / APIs
│   │
│   ├── reporting/
│   │   ├── __init__.py
│   │   ├── metrics.py            # Cálculo de Min, Max, Promedio, ahorros y deltas
│   │   └── generator.py          # Renderizado de HTML con inyección segura de JSON
│   │
│   ├── templates/
│   │   └── plantilla_reporte.html # Template base HTML + JS
│   │
│   └── main.py                   # Entrypoint CLI principal
│
├── scripts/
│   ├── ejecutar_ahora.bat        # Adaptado a: python "%~dp0..\src\main.py" --forzar
│   ├── instalar.bat              # Instalador adaptado a la nueva estructura
│   ├── instalar.ps1              # Script PowerShell adaptado
│   └── quitar_del_inicio.bat
│
├── requirements.txt
└── README.md
```

> **Regla de oro de rutas:** En `src/core/paths.py`, calcula la raíz con:
> `PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent`  
> Todas las lecturas/escrituras (`historial.db`, `config.yaml`, logs y reportes) deben resolverse desde `PROJECT_ROOT`.

---

### MÓDULO 2: CONTROL PRECISO DE FECHAS EN `config.yaml`
Modifica el parser de fechas y la lógica de generación de combinaciones para soportar:

1. **Rango estricto de fechas (Fechas calendario fijas):**
   ```yaml
   salida_desde: "2027-01-05"
   salida_hasta: "2027-02-28"
   ```
2. **Filtro opcional por meses permitidos:**
   ```yaml
   meses_permitidos: [1, 2] # Solo salidas en Enero y Febrero
   ```
3. **Regla de validación:** Descartar automáticamente fechas pasadas (`ida < hoy + 1 día`) y fechas donde `vuelta < ida`.

---

### MÓDULO 3: GESTIÓN DE REPORTES SIN SOBREESCRITURA
- **Problema resuelto:** Modificar `generator.py` para que, sin importar si la corrida fue general o de una sola búsqueda (`--busqueda "China"`), `reportes/index.html` lea desde `historial.db` la última corrida completa de **todas** las búsquedas activas en la base de datos.
- Generar adicionalmente archivos individuales `reportes/china.html` y `reportes/cipolletti.html` para acceso directo y evitar colisiones.

---

### MÓDULO 4: MEJORAS EN EL REPORTE HTML (`plantilla_reporte.html`)

1. **Tablas con ordenamiento dinámico (Sortable Tables):**
   - Implementar en Vanilla JavaScript (sin librerías pesadas externas) la capacidad de hacer clic en los `<th>` de las tablas para ordenar ascendente o descendente.
   - Debe interpretar correctamente tipos numéricos (precios como `150 USD`, duración `14h 30m`, escalas `0, 1, 2`) y tipos fecha (`YYYY-MM-DD`).
   - Agregar iconos indicadores `▲` / `▼` en la columna activa.
2. **Dashboard estadístico ampliado (KPIs):**
   - En las tarjetas resumen de cada destino, mostrar:
     - **Precio Mínimo (Mejor Oferta).**
     - **Precio Máximo.**
     - **Precio Promedio.**
     - **Mínimo Histórico.**
     - Comparativa vs corrida anterior (con flecha verde de bajada o roja de subida).
3. **Botón de actualización:**
   - Incorporar un botón "🔄 Actualizar datos de este vuelo".
   - Si se abre como archivo local (`file://`), mostrar un modal explicativo indicando ejecutar `ejecutar_ahora.bat` o levantar el servidor local.
   - Si se ejecuta mediante el servidor web (FastAPI/Flask), disparar una petición POST a `/api/actualizar` con indicador de carga (*loading spinner*).

---

### MÓDULO 5: ARQUITECTURA CLOUD 100% GRATUITA Y DESATENDIDA

El sistema debe poder operar 24/7 sin necesidad de tener la computadora encendida.

1. **Riesgo crítico a mitigar:**  
   Google Flights bloquea con frecuencia las IPs de centros de datos (GitHub Actions, Render, AWS). Para garantizar robustez total, provee:
   - **Opción A (Scraping):** Pausas aleatorias y rotación de User-Agent en `fast-flights`.
   - **Opción B (Recomendada y 100% gratuita):** Adaptador para la **API oficial de Amadeus (Flight Offers Search)**, que cuenta con tier gratuito mensual de **2.000 búsquedas gratis**, devolviendo JSON estructurado sin bloqueos ni captchas.

2. **Automatización Diaria (Cron Gratuito):**
   - Diseñar un workflow de **GitHub Actions** (`.github/workflows/busqueda_diaria.yml`):
     - Se ejecuta automáticamente todos los días mediante `schedule: cron: '0 10 * * *'` (10:00 UTC).
     - Permite ejecución manual con `workflow_dispatch`.
     - Conserva la persistencia de `historial.db` mediante Git Commit automático o almacenamiento en caché/artefactos.

3. **Sistema de Alertas Inteligente por Email:**
   - Integración con **Resend** (3.000 emails/mes gratis) o **Brevo/SendGrid** (o servidor SMTP de Gmail con contraseña de aplicación).
   - **Lógica estricta de envío (Evitar SPAM):**
     - Comparar la mejor opción de hoy con la corrida anterior.
     - **NO enviar email** si el precio no varió o subió con las mismas condiciones.
     - **SÍ enviar email** si:
       a) El precio bajó respecto al mínimo registrado.
       b) El precio es igual, pero mejoraron las condiciones (menos escalas o menor tiempo de vuelo).
     - El correo debe tener un diseño HTML responsive con:
       - Resumen del viaje, precio por persona, aerolínea y fechas.
       - Ahorro detectado respecto al promedio o corrida anterior.
       - Enlace directo a Google Flights o web de la aerolínea para reservar.

4. **Interfaz Web Gratuita para Usuarios:**
   - Crear una interfaz web ligera con **FastAPI + Jinja2** o **Streamlit**, lista para desplegar en **Render (Free Tier)** o **Hugging Face Spaces**.
   - Los usuarios pueden ver los reportes en vivo, agregar su email a la lista de alertas y definir sus fechas preferidas.

---

## 4. ENTREGABLES REQUERIDOS

Presenta la solución con código completo y listo para producción:
1. Script `src/core/paths.py` y `src/main.py` con argparse adaptado.
2. Módulo de lógica de fechas y filtros en `src/core/flight_searcher.py`.
3. Template `plantilla_reporte.html` actualizado con sort de tablas en JS y KPIs estadísticos (mínimo, máximo, promedio).
4. Módulo de envío de emails `src/notifications/mailer.py` con el motor de detección de mejoras de precio/escalas.
5. Archivo `.github/workflows/busqueda_diaria.yml` y configuración para la nube.
6. Guía clara en `README.md` de despliegue y configuración de secretos (`RESEND_API_KEY`, etc.).
