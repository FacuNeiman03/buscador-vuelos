# Buscador de vuelos baratos

Consulta precios en Google Flights para muchas combinaciones de fechas, guarda el historial en SQLite, arma un dashboard HTML con todas tus búsquedas y te avisa por email **solo cuando hay una mejora real**. Corre en tu PC (al prenderla, 1 vez por día) o **gratis en la nube** con GitHub Actions, sin necesidad de tener la computadora encendida.

- [1. Estructura](#1-estructura)
- [2. Uso en tu PC (Windows)](#2-uso-en-tu-pc-windows)
- [3. Configuración (`config/config.yaml`)](#3-configuración-configconfigyaml)
- [4. Estrategias para bajar el precio](#4-estrategias-para-bajar-el-precio)
- [5. Reportes](#5-reportes)
- [6. Alertas por email](#6-alertas-por-email)
- [7. En la nube, gratis (GitHub Actions + Pages + Render)](#7-en-la-nube-gratis)
- [8. Interfaz web](#8-interfaz-web)
- [9. Línea de comandos](#9-línea-de-comandos)
- [10. Problemas comunes](#10-problemas-comunes)

---

## 1. Estructura

```text
buscador-vuelos/
├── config/config.yaml            # tus búsquedas (el único archivo que tenés que editar)
├── data/
│   ├── historial.db              # todos los precios (SQLite, con migraciones automáticas)
│   ├── estado.json               # control de "1 vez por día"
│   └── suscriptores.json         # suscriptores a alertas creados desde la web (se crea solo)
├── logs/vuelos.log               # detalle de cada consulta y errores
├── reportes/
│   ├── index.html                # dashboard con TODAS las búsquedas
│   └── <busqueda>.html           # un reporte por búsqueda (china.html, cipolletti.html…)
├── src/
│   ├── core/
│   │   ├── paths.py              # rutas absolutas (todo se resuelve desde la raíz del proyecto)
│   │   ├── config.py             # carga y valida config.yaml (todos los errores juntos y en castellano)
│   │   ├── database.py           # SQLite: esquema versionado, corridas, consultas
│   │   ├── flight_searcher.py    # reglas de fechas, proveedores (Google Flights / SerpApi) y corrida
│   │   ├── estrategias.py        # dos solo ida, escalas armadas y combinación de pasajes
│   │   └── suscriptores.py       # suscriptores (JSON, sincronizable con GitHub)
│   ├── reporting/
│   │   ├── metrics.py            # mínimo, máximo, promedio, deltas, histórico
│   │   └── generator.py          # genera los HTML con JSON inyectado de forma segura
│   ├── notifications/mailer.py   # detección de mejoras + email (Resend o SMTP/Gmail)
│   ├── templates/                # plantilla del reporte y del email
│   ├── web/                      # interfaz web FastAPI + Jinja2
│   └── main.py                   # punto de entrada (CLI)
├── scripts/                      # .bat / .ps1 para Windows
├── tests/                        # pytest (fechas, reportes, alertas, web)
├── cloud/busqueda_diaria.yml     # workflow de GitHub Actions (scripts\preparar_github.bat lo copia a .github/workflows/)
├── render.yaml                   # despliegue de la web en Render (free)
├── requirements.txt              # núcleo
└── requirements-web.txt          # + interfaz web
```

`buscar_vuelos.py` en la raíz es solo un puente para el acceso directo viejo del Inicio de Windows. Después de correr `scripts\instalar.bat` una vez, se puede borrar.

---

## 2. Uso en tu PC (Windows)

| Qué querés hacer | Cómo |
|---|---|
| Instalar (una vez) | Doble clic en `scripts\instalar.bat`: instala Python si falta, crea `venv`, lo agrega al Inicio de Windows y hace la primera búsqueda |
| Que busque solo | Nada: al iniciar sesión corre en segundo plano 1 vez por día y abre `reportes\index.html` |
| Buscar ahora | `scripts\ejecutar_ahora.bat` |
| Ver cuántas consultas va a hacer antes de correr | `scripts\simular.bat` |
| Interfaz web local (botón "Actualizar" funcionando) | `scripts\servidor_web.bat` → http://localhost:8000 |
| Dejar de correr al prender la PC | `scripts\quitar_del_inicio.bat` |
| Ver qué pasó si algo falló | `logs\vuelos.log` |

> Si movés la carpeta, volvé a correr `scripts\instalar.bat`.

---

## 3. Configuración (`config/config.yaml`)

Indentación con **espacios** (nunca tabs). Lo que va después de `#` es comentario. Si algo está mal, el programa lista **todos** los errores juntos antes de empezar (no a mitad de una corrida de 20 minutos).

### 3.1 `general`

| Parámetro | Valores | Qué hace |
|---|---|---|
| `moneda` | `USD`, `ARS`, `EUR`… | Moneda de los precios |
| `idioma` | `es` | Idioma de Google Flights |
| `abrir_reporte` | `siempre` / `si_hay_novedad` / `nunca` | Cuándo abrir el reporte al terminar (en la nube nunca) |
| `una_vez_por_dia` | `true` / `false` | Aunque prendas la PC varias veces, corre una sola vez por día |
| `velocidad` | `rapida` / `normal` / `prudente` | Ritmo de consultas (ver abajo). Opcionales: `concurrencia` (1-8) e `intervalo_min` (segundos) |
| `max_consultas_por_corrida` | número | Tope total de consultas por corrida |
| `proveedor` | `auto` / `fast_flights` / `serpapi` | De dónde saca los precios ([ver 7.1](#71-el-problema-de-los-bloqueos-y-cómo-se-resuelve)) |
| `url_reporte` | URL | Link al reporte publicado, se incluye en los emails |
| `alertas.emails` | `[yo@mail.com]` | Reciben alertas de todas las búsquedas |
| `alertas.avisar_mejores_condiciones` | `true` / `false` | También avisar si el precio es igual pero hay menos escalas o menos horas |

**Velocidad de búsqueda.** Ya no hay pausas fijas: se hacen varias consultas en paralelo con un intervalo mínimo entre el inicio de cada una, y el ritmo se **adapta solo**: si Google devuelve un bloqueo o fallan consultas, frena al instante (y reintenta como siempre); cuando todo anda bien vuelve a acelerar de a poco. Son exactamente las mismas consultas y los mismos resultados, solo cambia cuánto se espera.

| Preset | En paralelo | Intervalo mínimo | Cuándo |
|---|---|---|---|
| `normal` (default) | 3 | 0,8 s | Uso diario desde tu PC |
| `rapida` | 4 | 0,4 s | Si `normal` nunca muestra "⏸ Google se puso estricto" en el log |
| `prudente` | 1 | 3,5 s | Como antes (≈ pausas de 2-5 s). Para la nube o si Google bloquea seguido |

### 3.2 `busquedas` (un bloque por viaje)

| Parámetro | Ejemplo | Qué hace |
|---|---|---|
| `nombre` | `Cipolletti` | Nombre (único) que aparece en el reporte y da nombre a `reportes/cipolletti.html` |
| `activa` | `true` / `false` | Con `false` no se busca, pero el bloque (y sus datos viejos) quedan |
| `hora` | `6` | Hora del día (Argentina) a la que corre en la nube. Sin `hora`, usa `general.hora` |
| `buscar_desde` / `buscar_hasta` | `2026-10-01` / `2026-12-31` | Opcionales. Solo se busca entre esas fechas; después se detiene sola y conserva el historial (no confundir con las fechas del viaje) |
| `origenes` / `destinos` | `[AEP, EZE]` | Códigos IATA |
| `tipo` | `ida_vuelta` / `solo_ida` | |
| `pasajeros` | `2` | Adultos. **Todos los precios se muestran por persona** |
| `clase` | `economy`, `premium-economy`, `business`, `first` | |
| `max_escalas` | `0`, `1`, `2`, `null` | |
| `max_duracion_horas` | `45` | Duración máxima de cada tramo |
| `equipaje` | `ninguno`, `mano`, `despachado` | Equipaje incluido en el precio ([ver 4](#4-estrategias-para-bajar-el-precio)) |
| `estrategia` | `mixta`, `solo_ida`, `ida_vuelta` | Cómo se arma el precio ([ver 4](#4-estrategias-para-bajar-el-precio)) |
| `escala_separada`, `hubs`, `conexion_min_horas` | `true`, `[MAD, GRU]`, `4` | Escalas armadas con pasajes separados |
| `ocultar_autotransferencia` | `true` / `false` | Saca pasajes separados donde re-despachás vos |
| `alerta_precio_persona` | `150` | Marca en verde lo que esté por debajo |
| `tolerancia_dias` / `ahorro_minimo_pct` | `2` / `5` | Ofertas cambiando unos días |
| `emails` | `[amigo@mail.com]` | Alertas solo de esta búsqueda |

### 3.3 Fechas: control preciso

**Rango de viaje (recomendado, es lo que usa la página web)** — la ida **y** la vuelta tienen que caer dentro del rango; nada afuera se consulta:

```yaml
    viajar_desde: "2027-01-01"
    viajar_hasta: "2027-03-31"
    duracion_dias: {min: 7, max: 10}
```

Con 7 a 10 días en enero (01/01 al 31/01) son 90 combinaciones de fechas: salidas del 1 al 24 para 7 días, del 1 al 23 para 8, etc. Los reportes, el mínimo histórico y las alertas también se acotan al rango vigente: si achicás el rango, los precios viejos de otros meses dejan de mostrarse y no bloquean avisos.

**Explorar un país a mano en el YAML:**

```yaml
    destinos: [GRU, GIG, SSA, FOR, REC, FLN]   # candidatos
    destino_pais: BR        # solo para mostrar "Brasil" en el reporte
    explorar_top: 3         # exploración rápida de todos y detalle de los 3 más baratos (0 = sin exploración)
```

La base de aeropuertos (`src/core/aeropuertos.json`, datos de [OurAirports](https://ourairports.com/data/), dominio público) se actualiza con `python scripts/generar_aeropuertos.py`.

**Otras formas** (para barridos más amplios):

**Ventana estricta** — solo busca salidas entre esas dos fechas (inclusive):

```yaml
    salida_desde: "2027-01-05"     # fecha fija, o relativa: "+20" = dentro de 20 días
    salida_hasta: "2027-02-28"
    meses_permitidos: [1, 2]       # opcional: SOLO salidas en enero y febrero
    vuelta_hasta: "2027-03-07"     # opcional: descarta viajes que vuelvan después
    duracion_dias: {min: 7, max: 10}   # o [7], o [7, 10, 14]
    paso_dias: 2                   # 1 = todos los días; 2 = día por medio (y después refina)
```

- `meses_permitidos` también sirve con ventanas amplias: `salida_desde: "+1"`, `salida_hasta: "+330"`, `meses_permitidos: [1, 2, 7]` busca solo enero, febrero y julio de todo el año próximo, sin gastar consultas en el resto.
- Reglas automáticas: nunca busca salidas anteriores a **mañana** (una ventana que ya empezó se recorta sola) ni combinaciones con **vuelta anterior a la ida**. Las pasadas de refinamiento y "cambiando días" respetan los mismos filtros.
- Antes de correr, `scripts\simular.bat` (o `python src/main.py --simular`) muestra cuántas consultas hará cada búsqueda.

**Fechas exactas** (si está completo, se ignora la ventana):

```yaml
    fechas:
      - {ida: 2027-03-05, vuelta: 2027-03-19}
      - {ida: 2027-04-10, vuelta: 2027-04-24}
```

**Cómo busca (3 pasadas):** 1) todas las salidas de la ventana según `paso_dias`; 2) si `paso_dias` > 1, día por día alrededor de las `refinar_top` más baratas; 3) alrededor de las más baratas sale ±1 día y vuelve con ±`tolerancia_dias`.

**Cuánto tarda:** cada consulta a Google tarda 3-6 s. Con 300 consultas son ~25 minutos.

Códigos útiles: `EZE` Ezeiza · `AEP` Aeroparque · `NQN` Neuquén · `BRC` Bariloche · `MDZ` Mendoza · `BKK`/`DMK` Bangkok · `HKT` Phuket · `PEK`/`PKX` Beijing · `PVG`/`SHA` Shanghai · `HKG` Hong Kong · `MAD` Madrid · `MIA` Miami.

---

## 4. Estrategias para bajar el precio

Cada búsqueda de ida y vuelta puede buscar de tres formas (campo **Estrategia** en la página, o `estrategia:` en el YAML):

| Estrategia | Qué hace | Cuándo conviene |
|---|---|---|
| `mixta` | Consulta cada día de **ida** y cada día de **vuelta** como pasajes sueltos y arma la mejor combinación (aerolíneas distintas, salir por EZE y volver a AEP). Después confirma el **ida y vuelta** real en las `verificar_top` (8) mejores fechas y se queda con lo más barato. | Casi siempre |
| `solo_ida` | Solo la parte de pasajes sueltos. Es la que menos consultas usa y cubre **todas** las duraciones del rango sin costo extra. | Vuelos nacionales y low cost |
| `ida_vuelta` (default) | El comportamiento clásico: solo pasajes de ida y vuelta, como Google. | Vuelos largos donde el ida y vuelta siempre gana |

Consultas: con pasajes sueltos se hacen `días de ida + días de vuelta` consultas por ruta, en vez de `días de ida × duraciones`. Ejemplo: enero entero, viajes de 7 a 10 días → 24 + 24 = 48 consultas por ruta, contra 90 con ida y vuelta.

### Equipaje

Campo **Equipaje que llevás** (`equipaje: ninguno | mano | despachado`). Con `mano` o `despachado` todos los precios incluyen lo que cobra cada aerolínea por ese equipaje (así una low cost barata sin valija no le "gana" a una aerolínea que ya la incluye). Además se re-cotizan las mejores opciones **sin** equipaje y el reporte muestra la columna "Sin equipaje (+cuánto suma)". El costo del equipaje lo estima Google Flights con lo que informa cada aerolínea: para algunas puede no estar disponible.

### Escalas armadas con pasajes separados

Casilla **Probar escalas armadas** (`escala_separada: true`, `hubs: [MAD, GRU]`, `conexion_min_horas: 4`). Para las `escala_top` (4) mejores fechas prueba llegar a una ciudad de escala con un pasaje y seguir con otro (ej: Buenos Aires → Madrid + Madrid → Tokio, y lo mismo a la vuelta). Solo acepta conexiones de al menos `conexion_min_horas` y como máximo 24 h, y solo guarda la opción si es más barata que lo que ya había. Si no elegís ciudades, sugiere según el continente del destino (Europa: MAD, BCN, LIS, GRU · Asia: MAD, IST, DXB, GRU · etc.).

> ⚠️ Con pasajes separados, si el primer vuelo se atrasa la aerolínea del segundo no te espera ni te reubica; en la escala pasás migraciones y volvés a despachar la valija, así que necesitás poder entrar a ese país (visa). El reporte y el email marcan estas opciones con 🔀 y muestran el aviso. No se aplica si pediste "Solo directos".

En el reporte, cada opción armada con varios pasajes muestra una etiqueta (🎫 2 pasajes / 🔀 escala MAD), un link para cada pasaje y la tarjeta **Cómo conviene comprar** compara el mejor precio de cada forma.

## 5. Reportes

### Crear y editar búsquedas desde la página

Con la interfaz web levantada (`scripts\servidor_web.bat` → http://localhost:8000, o Render):

- **➕ Nueva búsqueda:** nombre, desde dónde salís (por defecto **AEP, EZE** = Buenos Aires), destino (con autocompletado de aeropuertos), **rango de fechas en que podés viajar**, días de viaje mínimo y máximo, pasajeros, escalas, clase y precio de alerta. Mientras completás, muestra cuántas consultas va a hacer y cuánto tarda. Con "Buscar precios apenas la guarde" arranca en el momento.
- **Destino por país o ciudad:** en "Quiero ir a" escribís un país (*Brasil*), una ciudad en castellano (*Tokio*, *Londres*, *Bariloche*, *Cipolletti*) o un código (*GRU*). Una ciudad agrega todos sus aeropuertos (Londres = LHR, LGW, STN, LTN, LCY). Un país tilda sus aeropuertos principales y podés tildar/destildar los demás.
- **Exploración en 2 pasos (al elegir un país):** primero una pasada rápida por todos los aeropuertos tildados con 3 fechas de muestra, y después la búsqueda completa (todas las fechas del rango) solo en los N más baratos (3 por defecto). El reporte muestra la tabla de exploración con el precio de muestra de cada destino y cuáles se buscaron en detalle. Ejemplo: Brasil en enero, 7 a 10 días → ~84 consultas de exploración + ~540 de detalle.
- **✏️ Editar**, **⏸ Pausar / ▶ Reanudar** y **🗑 Borrar** en cada búsqueda. Si le cambiás el nombre, el historial se mueve al nombre nuevo; si la borrás, el historial queda en la base.
- Los cambios se guardan en `config/config.yaml` sin perder los comentarios (y, en Render, también en el repo de GitHub). Si hay `WEB_ADMIN_TOKEN`, lo pide una vez.
- Abriendo `reportes\index.html` como archivo suelto, los botones explican cómo levantar el servidor (un HTML suelto no puede guardar nada).

### Qué muestra

Cada corrida regenera `reportes/index.html` **leyendo la base**, así que correr `--busqueda "China"` ya no borra los resultados de Cipolletti: el índice siempre tiene la última corrida útil de todas las búsquedas (las pausadas aparecen marcadas). Además se genera un archivo por búsqueda (`reportes/china.html`, `reportes/cipolletti.html`) para abrir o compartir directo.

En cada búsqueda:

- **KPIs:** precio mínimo (mejor oferta, con fechas, aerolínea, escalas y duración) · precio máximo · precio promedio (y cuánto ahorra la mejor) · mínimo histórico · variación vs la corrida anterior (▼ verde / ▲ rojo, en $ y %) · mejor opción cambiando días.
- Mínimo/máximo/promedio se calculan sobre la opción más barata de **cada combinación de fechas**: el máximo es "la peor fecha para viajar", no el vuelo más caro de todos.
- **Tablas ordenables:** clic en cualquier encabezado ordena ascendente/descendente (▲/▼). Entiende precios (`US$ 1.234`, `150 USD`), duraciones (`14h 30m`), escalas y fechas; los vacíos quedan al final.
- **Gráficos:** precio por fecha de salida y evolución (mínimo, promedio y máximo por corrida).
- **Botón "🔄 Actualizar datos de este vuelo":** con la interfaz web levantada busca esa ruta en el momento (con indicador de progreso). Abierto como archivo local o desde GitHub Pages, explica cómo actualizar.
- Si una corrida se cortó (se apagó la PC), sus datos se aprovechan y el reporte avisa que es parcial.

---

## 6. Alertas por email

**Regla anti-spam** (por destinatario y búsqueda, y dentro de sus fechas preferidas si las eligió):

| Situación de la mejor opción de hoy vs el **mínimo registrado** en corridas anteriores | ¿Email? |
|---|---|
| Bajó el precio | ✅ Sí |
| Mismo precio, pero menos escalas | ✅ Sí |
| Mismo precio, mismas escalas y más de 10 min más corto | ✅ Sí |
| Mismo precio y mismas condiciones | ❌ No |
| Subió | ❌ No |
| Primera corrida de la búsqueda (solo fija la referencia) | ❌ No |

Se compara contra el **mínimo histórico** (no solo contra ayer) para no avisar cada vez que el precio rebota. Cada aviso queda registrado en la tabla `notificaciones` y nunca se repite para la misma corrida.

El email (HTML responsive + texto plano) trae: ruta, fechas, precio por persona y total, aerolínea, escalas y duración, ahorro vs el mínimo anterior, vs la corrida anterior y vs el promedio del día, botón directo a Google Flights, link al reporte y link de baja.

**Configurar el envío** (variables de entorno o secretos de GitHub):

| Opción | Variables |
|---|---|
| **Resend** (recomendado, 3.000 emails/mes gratis) | `RESEND_API_KEY`, `RESEND_FROM` (ej. `Vuelos <alertas@tudominio.com>`). Sin dominio verificado, Resend solo deja enviar desde `onboarding@resend.dev` **a tu propio email** de la cuenta |
| **Gmail** | `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER=tu@gmail.com`, `SMTP_PASSWORD=<contraseña de aplicación>` ([crearla](https://myaccount.google.com/apppasswords), requiere verificación en 2 pasos) |
| Brevo / SendGrid / otro | Mismas variables `SMTP_*` con los datos SMTP del servicio |

Destinatarios: `general.alertas.emails`, `emails` de cada búsqueda, variable `ALERTA_EMAILS` (separados por coma) y los suscriptos desde la web.

Probar el diseño y la configuración: `python src/main.py --probar-email tu@mail.com`.

---

## 7. Todo en la nube (sin la PC)

```
 Render (web)  ── crear/editar/programar ──►  config.yaml en GitHub (main)
      │                                              │
      ├── "Actualizar" ── workflow_dispatch ──►  GitHub Actions (cada hora: ¿a quién le toca?)
      │                                              │  busca en Google Flights, manda los mails
      └──◄── baja historial.db ── rama "datos" ◄─────┘
```

- **GitHub Actions** corre cada hora (minuto 17). Busca solo las búsquedas a las que les toca: activas, dentro de su ventana `buscar_desde`/`buscar_hasta`, ya pasada su `hora` (Argentina; por defecto `general.hora: 6`) y que hoy no hayan corrido bien. Si Google bloquea, reintenta una vez más ese día y nada más. Si no toca nada, termina en segundos.
- **Render** es la web: muestra los reportes, guarda en GitHub lo que creás o editás, y el botón **Actualizar** dispara la búsqueda en GitHub y muestra el progreso. Cuando la búsqueda termina, baja los datos nuevos.
- Los **mails** de alertas salen de GitHub Actions (Gmail por SMTP).
- El historial vive en la rama `datos`, que se pisa en cada corrida (un solo commit). Así el repo no crece.

### 7.1 Bloqueos de Google

Google es más estricto con IPs de datacenter. Por eso en la nube va en velocidad `prudente` (variable `VUELOS_VELOCIDAD`). Si igual bloquea: `SERPAPI_API_KEY` (cupo mensual chico) o `VUELOS_PROXY=http://usuario:clave@host:puerto` (proxy residencial).

### 7.2 GitHub: secretos y variables

**Settings → Secrets and variables → Actions**

| Secreto | Valor |
|---|---|
| `SMTP_HOST` / `SMTP_PORT` | opcionales: por defecto `smtp.gmail.com` / `587` |
| `SMTP_USER` | tu Gmail |
| `SMTP_PASSWORD` | contraseña de aplicación de Gmail (myaccount.google.com/apppasswords; requiere verificación en 2 pasos) |
| `ALERTA_EMAILS` | opcional: quién recibe todas las alertas (separados por coma). Por defecto, `SMTP_USER` |
| `VUELOS_CLAVE` | una frase larga cualquiera; cifra los emails de los suscriptores (el repo es público). La misma en Render |
| `SERPAPI_API_KEY`, `VUELOS_PROXY` | opcionales |

| Variable | Valor |
|---|---|
| `VUELOS_URL_WEB` | la URL de Render (va en los links de los mails) |
| `VUELOS_MAX_CONSULTAS`, `VUELOS_VELOCIDAD` | opcionales |

Para probarlo: **Actions → Búsqueda de vuelos → Run workflow**.

### 7.3 Render (la web)

1. https://render.com, entrás con GitHub → **New → Blueprint** → elegí este repo (usa `render.yaml`).
2. Completá:
   - `GITHUB_TOKEN`: token *fine-grained* (github.com/settings/personal-access-tokens), solo sobre este repo, con **Contents: Read and write** y **Actions: Read and write**.
   - `VUELOS_CLAVE`: la misma frase que en GitHub.
   - `SMTP_USER` / `SMTP_PASSWORD`: los mismos de Gmail (para el mail de confirmación de suscripciones).
   - `VUELOS_URL_WEB`: la URL que te da Render.
3. `WEB_ADMIN_TOKEN` se genera solo (Environment en el panel de Render). La web te lo pide la primera vez que creás, editás o actualizás algo, y queda guardado en ese navegador.

El plan gratis "duerme" a los 15 min sin visitas y tarda ~1 min en despertar. Al despertar baja la config y los datos más nuevos de GitHub. Las búsquedas programadas no dependen de Render.

### 7.4 Suscripciones

Cualquiera con el link `/suscribirse` puede pedir alertas de una búsqueda. Recibe un mail y recién queda anotado cuando abre el link de confirmación. `/baja` lo da de baja. Los emails se guardan cifrados en `data/suscriptores.json`.

---

## 8. Interfaz web

`scripts\servidor_web.bat` (local) o Render:

| Ruta | Qué es |
|---|---|
| `/` y `/<busqueda>.html` | Reportes en vivo |
| `/suscribirse` | Formulario: email, viaje y rango de fechas preferido (opcional) |
| `/confirmar?token=…` | Confirma la suscripción (link del mail) |
| `/baja?token=…` | Baja (link incluido en cada email) |
| `GET/POST /api/busquedas`, `PUT/DELETE /api/busquedas/{nombre}`, `POST /api/busquedas/{nombre}/estado`, `POST /api/busquedas/estimar` | Gestión de búsquedas (misma validación que la página) |
| `POST /api/actualizar` | `{"busqueda": "China"}` → busca en segundo plano (header `X-Token` si hay `WEB_ADMIN_TOKEN`) |
| `GET /api/estado` | Progreso de la actualización en curso |
| `GET /api/salud` | Healthcheck |

---

## 9. Línea de comandos

```bat
venv\Scripts\python.exe src\main.py                               :: todas las activas (respeta 1 vez por día)
venv\Scripts\python.exe src\main.py --forzar                      :: aunque ya haya corrido hoy
venv\Scripts\python.exe src\main.py --busqueda "China"            :: solo esa (aunque esté pausada)
venv\Scripts\python.exe src\main.py --simular                     :: cuántas consultas haría, sin consultar
venv\Scripts\python.exe src\main.py --solo-reporte                :: regenera reportes desde la base
venv\Scripts\python.exe src\main.py --proveedor serpapi           :: pisa general.proveedor
venv\Scripts\python.exe src\main.py --sin-email --no-abrir
venv\Scripts\python.exe src\main.py --probar-email yo@mail.com
venv\Scripts\python.exe src\main.py --config otra.yaml
```

`python -m src.main …` es equivalente. Tests: `pip install -r requirements-dev.txt` y `python -m pytest`.

---

## 10. Problemas comunes

| Problema | Solución |
|---|---|
| "X consultas fallaron" | Google limitó. Poné `velocidad: prudente`, usá `proveedor: auto` con `SERPAPI_API_KEY`, o un proxy |
| En GitHub Actions todo falla con "posible captcha/bloqueo" | Es el bloqueo a IPs de datacenter: configurá `SERPAPI_API_KEY` (y rangos de fechas acotados) |
| Error al leer `config.yaml` | El mensaje lista cada problema. Casi siempre es indentación (espacios, no tabs) o un código IATA mal escrito |
| No llegan emails | Corré `--probar-email`. Con Resend sin dominio verificado solo podés enviarte a vos mismo. En Gmail usá contraseña de aplicación. Recordá que la primera corrida nunca avisa |
| "No hay fechas válidas para buscar" | La ventana quedó en el pasado o `meses_permitidos` no tiene días dentro de la ventana |
| El gráfico no se ve | Se carga de internet (Chart.js): abrí el reporte con conexión. Las tablas funcionan igual |
| El precio no coincide con Google | Los precios cambian. El reporte muestra precio **por persona**; el link "Ver" abre la búsqueda actual |
