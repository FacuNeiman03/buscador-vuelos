# Prompt para Claude Design: rediseño UX/UI del "Buscador de vuelos baratos"

> Copiá desde la línea de abajo hasta el final y pegalo en Claude Design. Si podés, adjuntá también `src/templates/plantilla_reporte.html` (la página actual) y una captura del reporte.

---

## Rol y objetivo

Sos un diseñador de producto senior (UX/UI) especializado en herramientas de datos y comparadores de precios. Tenés que rediseñar la interfaz de una **aplicación web personal que busca los vuelos más baratos** en Google Flights. La app ya funciona: la lógica, los datos y la API existen y **no cambian**. Tu trabajo es que la interfaz sea **limpia, elegante, profesional, fácil de usar y 100 % responsive** (celular, tablet y escritorio), sin agregar funciones que no existen y sin sacar ninguna de las que hay.

Idioma de toda la interfaz: **castellano rioplatense** (vos, "elegí", "tocá", "buscá"). Moneda y formato: `US$ 1.234` (punto de miles, sin decimales). Fechas cortas: `vie 5 ene 27`; en tablas se ordenan por la fecha ISO.

## Qué tenés que entregar

1. **Sistema de diseño**: paleta con tokens (CSS custom properties) para **modo claro y oscuro** (`prefers-color-scheme`), tipografía (fuente del sistema o una de Google Fonts), escala de espaciado, radios, sombras, estados (hover, focus visible, disabled, cargando), colores semánticos (bajó = verde, subió = rojo, advertencia = ámbar, información = azul) con contraste AA.
2. **Diseños** de todas las pantallas y estados listados abajo, en **3 anchos**: celular (360–430 px), tablet (768 px) y escritorio (1280 px).
3. **Implementación final**: un **único archivo HTML** (HTML + CSS + JavaScript vanilla en el mismo archivo) que reemplace a `src/templates/plantilla_reporte.html`, respetando el **contrato técnico** de la sección "Restricciones técnicas". Si además diseñás las páginas de suscripción, entregá los templates Jinja2 (`base.html`, `suscribirse.html`, `mensaje.html`) aparte.

## Qué es la app (contexto para el diseño)

- El usuario crea **búsquedas** (ej: "Cipolletti", "Japón enero"). Cada búsqueda tiene origen (por defecto Buenos Aires: AEP y EZE), destino (uno o varios aeropuertos, o un país entero), un **rango de fechas en que puede viajar** (la ida y la vuelta quedan dentro), días de viaje mínimo y máximo, pasajeros, escalas, clase, equipaje, estrategia de búsqueda y un precio de alerta opcional.
- Una vez por día (o cuando el usuario toca "Actualizar") el programa consulta cientos de combinaciones de fechas y guarda los precios en una base.
- Esta página muestra, **para cada búsqueda**, los precios más baratos, estadísticas, gráficos y tablas; y permite **crear, editar, pausar, borrar y actualizar** búsquedas.
- **Todos los precios son por persona.** La app manda alertas por email cuando baja el precio (eso no se ve en esta página, salvo el link "Alertas por email").

## Cómo se abre la página (3 modos, el diseño debe contemplarlos)

| Modo | Cómo se detecta | Qué funciona |
|---|---|---|
| **Servidor web** (local `http://localhost:8000` o Render) | `GET /api/salud` responde 200 | Todo: actualizar, crear/editar/pausar/borrar, suscripción |
| **Archivo local** (`file://`) | `location.protocol === "file:"` | Solo lectura. Los botones de acción abren un **modal explicativo** con cómo levantar el servidor |
| **Página estática** (GitHub Pages) | `/api/salud` falla | Igual que archivo local; el modal suma un link a GitHub Actions si `DATOS.repo` existe |

Además hay **dos variantes de la misma página**: el **índice** (`DATOS.modo === "indice"`, con todas las búsquedas en solapas) y el **reporte individual** (`DATOS.modo === "individual"`, una sola búsqueda, con links a las demás y a "Todas las búsquedas").

## Inventario completo de funciones (NO sacar ninguna, NO inventar otras)

### A. Encabezado global
- Título "Vuelos baratos" y "Generado dd/mm/aaaa hh:mm" (`DATOS.generado`).
- Botón primario **"Nueva búsqueda"** (abre el formulario).
- Botón **"Abrir sólo esta ↗"** (solo en el índice): abre el reporte individual de la búsqueda activa (`<slug>.html`).
- Link **"Alertas por email"** a `suscribirse`: **solo visible si hay servidor**.

### B. Navegación entre búsquedas
- Índice: una solapa por búsqueda (activas primero, después pausadas). Cada solapa puede tener una etiqueta chica: **"pausada"** (si `activa` es false) o **"sin datos"** (si `sin_datos`). La solapa activa se refleja en el hash de la URL (`#slug`) y se respeta al recargar; el cambio de hash también cambia de solapa.
- Individual: link "← Todas las búsquedas" + links a las otras búsquedas (`DATOS.enlaces`).
- Con muchas búsquedas en celular, las solapas deben seguir siendo usables (scroll horizontal, selector o lo que propongas).

### C. Cabecera de la búsqueda activa
- Nombre, ruta legible (`ruta`, ej: "AEP/EZE ⇄ Brasil (28 aeropuertos, detalle de los 3 más baratos)"), cantidad de pasajeros, "actualizado …", "vía <proveedor>".
- Chip con el rango vigente (`rango`, ej: "viaje entre 01/01/2027 y 31/03/2027").
- Acciones: **Actualizar datos de este vuelo** (primario; deshabilitado si la búsqueda está pausada), **Editar**, **Pausar / Reanudar**, **Borrar** (pide confirmación; aclara que el historial queda guardado).
- Mientras actualiza: el botón muestra spinner y un texto de progreso que llega del servidor (ej: "Buscando Cipolletti…", "Generando reportes…"). Al terminar, recarga la página. Si falla, muestra el error.

### D. Avisos (banners)
- Corrida **parcial** (`parcial`): "La última corrida se interrumpió antes de terminar…".
- **Errores** (`errores > 0`): "X de Y consultas fallaron…".
- **Pausada**: "Búsqueda pausada: no se actualiza sola."
- **Sin datos** (`sin_datos`): estado vacío con la cabecera y las acciones, y el texto "Todavía no hay precios dentro de este rango de fechas. Tocá Actualizar…".
- **Sin búsquedas**: estado vacío invitando a crear la primera.

### E. Indicadores (KPIs), en tarjetas
1. **Precio mínimo · mejor oferta** (destacada): precio, destino si hay varios, fechas ida → vuelta, aerolíneas, escalas, duración. Si la mejor opción es de varios pasajes: etiqueta del tipo y un link por pasaje. Si hay equipaje, "sin equipaje: US$ X".
2. **Precio máximo** (el de la fecha más cara) · "de N combinaciones".
3. **Precio promedio** · "la mejor ahorra US$ X".
4. **Mínimo histórico** · fechas y cuándo se vio, o "¡es el de hoy!".
5. **vs corrida anterior**: ▼ verde / ▲ rojo con monto y %, "= igual" o "primera corrida".
6. **Cambiando días** (solo si existe `mejor_flex`): precio y fechas de la oferta con otra duración.

### F. Tarjeta "Cómo conviene comprar"
Solo si hay más de un tipo de pasaje con precio (`por_tipo`), o si hay equipaje elegido, o si la mejor opción usa escala armada. Muestra el mejor precio de cada tipo: **Ida y vuelta**, **2 pasajes**, **Escala armada**; resalta el más barato y muestra "+US$ X más caro" en los demás. Si la mejor opción es una escala armada, muestra en ámbar la **advertencia** de `mejor.detalle.aviso` (pasajes separados: si el primer vuelo se atrasa nadie te reubica; en la escala pasás migraciones y volvés a despachar; revisá si necesitás visa). Si hay equipaje: "Precios con valija de mano/despachada incluida".

### G. Exploración de destinos (solo si `exploracion` tiene filas)
Tabla: destino (ciudad + código), precio de muestra por persona, fechas de la muestra y desde qué aeropuerto, aerolíneas, escalas, marca "✓ buscado en detalle" en los elegidos (fila resaltada), link "Ver". Texto: "Pasada rápida con fechas de muestra por N aeropuertos… los X más baratos se buscaron en detalle".

### H. Gráfico "Precio más barato según fecha de salida"
Línea con el precio más barato por día de salida (`por_dia`), tooltip con fechas, precio y aerolínea, y una línea punteada con el precio de alerta si existe. Hoy usa **Chart.js 4 por CDN**; si no carga (sin internet), se muestra un aviso y las tablas siguen funcionando. Podés rediseñar el gráfico (colores, ejes, tooltip), pero debe ser legible en celular.

### I. Tabla "Ofertas cambiando unos días" (solo si hay `ofertas`)
Ida, vuelta, días, precio, ahorro (monto y %), "comparado con" (precio y fechas de referencia), aerolíneas, escalas, link.

### J. Tabla "Mejor opción por mes" y K. "Top N más baratas"
Columnas: (mes) · ida · vuelta · días · **destino** (solo si `multi_destino`) · precio x persona (+ etiqueta **alerta** si está bajo el precio de alerta, + etiqueta de tipo **🎫 2 pasajes** o **🔀 escala MAD**) · **sin equipaje** (solo si la búsqueda tiene equipaje: precio y "+cuánto suma") · vs anterior (▼/▲) · aerolíneas · escalas · duración de la ida · ruta de la ida · links (un "Ver" para ida y vuelta; o "Ida · Vta" / "Ida 1 · Ida 2 · Vta 1 · Vta 2" para pasajes separados, cada uno con tooltip del tramo). Las filas bajo el precio de alerta van resaltadas.
- **Todas las tablas se ordenan** haciendo clic (o Enter/Espacio) en el encabezado: asc/desc con indicador ▲/▼; entiende precios, duraciones (`14h 30m`), números y fechas; los vacíos van al final. Mantené esto (los valores crudos viajan en `data-v`).
- En celular las tablas anchas tienen que seguir siendo usables: proponé una solución (tarjetas por fila, columnas prioritarias + detalle desplegable, o scroll horizontal con primera columna fija). No escondas datos sin forma de verlos.

### L. Gráfico "Evolución del precio"
Mínimo, promedio y máximo por corrida (`evolucion`). Con menos de 2 corridas: "Aparece a partir de la segunda corrida".

### M. Pie
"Precios de Google Flights al momento de la consulta, por persona… verificá siempre en el link antes de comprar. Hacé clic en los títulos de las columnas para ordenar."

### N. Formulario "Nueva búsqueda" / "Editar: <nombre>" (modal o panel lateral; en celular, pantalla completa)
Campos, en este orden lógico (podés agrupar en pasos o secciones si mejora la UX):
1. **Nombre** (opcional; si queda vacío se arma solo, ej. "Brasil enero").
2. **Salgo desde**: selector de aeropuertos con chips. Por defecto **AEP · Buenos Aires** y **EZE · Buenos Aires (Ezeiza)**. Autocompleta ciudades y aeropuertos (no países).
3. **Quiero ir a**: el mismo selector, pero acepta **país, ciudad o aeropuerto**:
   - Sugerencias con ícono: 🌎 país ("Brasil · país · 129 aeropuertos"), 🏙 ciudad en castellano ("Tokio · Japón · HND, NRT"; una ciudad agrega todos sus aeropuertos), ✈️ aeropuerto ("GRU · São Paulo · nombre").
   - Navegación con flechas, Enter elige; si escribís y salís sin elegir, se toma la primera coincidencia; si no hay, muestra "No encontré X".
   - Al elegir un **país**: un chip "🌎 Brasil · 28 aeropuertos" y un **panel** con la lista de aeropuertos del país tildables (ciudad + código), atajos "Tildar todos (N)", "Solo los principales", "Ninguno", y un número **"buscar en detalle los [3] más baratos"** (1 a 10) con el texto: "Primero una pasada rápida por los N aeropuertos tildados y después búsqueda completa en los X más baratos".
   - Si la lista de aeropuertos no carga: aviso en rojo ("el servidor está corriendo una versión anterior… mientras tanto escribí códigos de 3 letras").
4. **Puedo viajar desde / Hasta** (fechas; mínimo mañana) + ayuda: "La ida y la vuelta quedan siempre dentro de estas fechas".
5. **Tipo de viaje**: Ida y vuelta / Solo ida (con solo ida se ocultan días de viaje, estrategia y escalas armadas).
6. **Días de viaje (mín – máx)**.
7. **Pasajeros** (1–9) · **Escalas** (Solo directos / Hasta 1 / Hasta 2 / Cualquiera) · **Clase** (Económica / Premium economy / Business / Primera) · **Avisarme debajo de (x persona)** (opcional).
8. Sección **"Cómo buscar el precio más bajo"**:
   - **Estrategia** (en este orden, la primera es la predeterminada): **"Solo ida y vuelta (como Google)"**, **"Inteligente: 2 pasajes de ida + ida y vuelta"**, **"Solo 2 pasajes de ida (menos consultas)"**, con un texto de ayuda que cambia según la opción.
   - **Equipaje que llevás**: Solo mochila / Valija de mano / Valija despachada ("los precios incluyen lo que cobra cada aerolínea").
   - Casilla **"Probar escalas armadas con pasajes separados"** → al tildarla aparece: selector de **ciudades de escala** con chips (se autocompletan sugerencias según el destino, ej. MAD, IST, DXB, GRU), **conexión mínima (horas)** (1–24, por defecto 4) y la advertencia ⚠️ de pasajes separados, migraciones y visa.
9. Casilla **"Buscar precios apenas la guarde"** (tildada por defecto).
10. **Estimación en vivo** (se recalcula al cambiar cualquier campo): "Va a hacer ~624 consultas (unos 52 min):" + desglose por etapas (exploración, ida y vuelta, pasajes de ida y de vuelta, verificación, escalas armadas, comparar sin equipaje). Si no hay combinaciones: "⚠️ Con estos datos no hay ninguna combinación de fechas". Si faltan datos: "Completá destino y fechas…".
11. **Errores de validación** del servidor en lista (ej: "Entre 01/01/2027 y 03/01/2027 no entra un viaje de 7 días…").
12. Botones **Cancelar** / **Guardar**. Al guardar: toast "Guardada. Buscando precios…" y progreso, o recarga en la solapa de la búsqueda.

### O. Modales, toasts y otros estados
- Modal "sin servidor" (ver modos).
- Pedido de **token de administrador** cuando la API responde 401 (hoy es un `prompt()`; diseñá un modal propio con campo de contraseña; se guarda en `localStorage` con try/catch).
- Confirmación de **borrado** (hoy `confirm()`; diseñá un modal propio).
- **Toasts** para: búsqueda iniciada, ya hay una búsqueda en curso (409), guardada, pausada/reanudada, borrada, errores.
- Estado de carga en botones (spinner), foco visible, `Esc` cierra modales, clic fuera cierra modales.

### P. Páginas de suscripción (Jinja2, opcional pero deseable, mismo estilo)
- `/suscribirse`: email, viaje (select con las búsquedas activas), "Salir desde / hasta" opcionales, honeypot oculto `web`, botón "Avisarme", mensajes de éxito/error, texto explicando que solo se avisa si baja el precio o mejoran las condiciones.
- `/baja?token=…`: mensaje de confirmación.
- Link "← Ver reportes".

## Datos disponibles (no hay otros)

El servidor inyecta un JSON en `const DATOS = /*__DATOS__*/null;`:

```
DATOS = {
  generado: "27/09/2026 20:28", modo: "indice" | "individual", repo: "usuario/repo" | "", url_reporte: "",
  enlaces: [{nombre, slug, activa}],
  busquedas: [{
    nombre, slug, activa, en_config, sin_datos?, ruta, rango, pasajeros, ida_vuelta, multi_destino,
    duraciones: [7,8], moneda: "USD", alerta: 150 | null, actualizado, parcial, proveedor, consultas, errores, corridas,
    estrategia: "ida_vuelta"|"mixta"|"solo_ida", equipaje: "ninguno"|"mano"|"despachado", escala_separada: bool,
    stats: {minimo, maximo, promedio, n}, prev_stats, prev_best, ahorro_vs_promedio,
    mejor: FILA, mejor_flex: OFERTA | null, hist_min: {precio, ida, vuelta, consultado} | null,
    por_tipo: {ida_vuelta?: 170, dos_solo_ida?: 135, escala_separada?: 625},
    top: [FILA], por_mes: [FILA], ofertas: [OFERTA], por_dia: [{ida, vuelta, precio, aerolineas}],
    evolucion: [{dia, precio, promedio, maximo}],
    exploracion: [{destino, ciudad, precio, ida, vuelta, origen, aerolineas, escalas, link, elegido}],
    config: {nombre, activa, origenes, destinos, destino_pais, explorar_top, tipo, viajar_desde, viajar_hasta,
             dias_min, dias_max, pasajeros, clase, max_escalas, alerta_precio_persona,
             estrategia, equipaje, escala_separada, hubs, conexion_min_horas}   // para precargar "Editar"
  }]
}
FILA = {ida, vuelta, dias, precio (x persona), total, previo, origen, destino, destino_txt, aerolineas, escalas,
        duracion_min, duracion_txt, salida, llegada, ruta, link, tipo: "ida_vuelta"|"dos_solo_ida"|"escala_separada",
        sin_equipaje?, detalle?: {tramos: [{sentido: "ida"|"vuelta", origen, destino, fecha, salida, llegada,
        aerolineas, escalas, duracion_min, ruta, precio (TOTAL de todos los pasajeros), link}], aviso?}}
OFERTA = FILA + {ahorro, ahorro_pct, ref_ida, ref_vuelta, ref_precio}
```
Ojo: `precio` de FILA es **por persona**; `precio` de cada tramo es **total** (dividir por `pasajeros` para mostrarlo por persona).

## API del servidor (no hay otros endpoints)

| Método y ruta | Uso | Respuesta |
|---|---|---|
| `GET /api/salud` | detectar servidor | `{ok: true}` |
| `POST /api/actualizar` `{busqueda}` | buscar precios ya | 202; 409 si ya hay una en curso; 401 sin token |
| `GET /api/estado` | progreso (consultar cada 5 s) | `{corriendo, progreso, inicio, error, ultimo}` |
| `GET /api/aeropuertos` | base para el selector | `{paises:{BR:"Brasil"}, continentes, metros:{"Tokio":["HND","NRT"]}, aeropuertos:[[iata, ciudad, nombre, pais, "L"|"M"|"S"]]}` (~240 KB, cargar al abrir el formulario) |
| `POST /api/busquedas/estimar` | estimación en vivo | `{consultas, combinaciones, rutas, exploracion, tramos, dias_ida, dias_vuelta, verificacion, escala, equipaje, detalle, destinos_detalle, estrategia, hubs, primera_ida, ultima_ida}`; 422 `{detail:{errores:[…]}}` |
| `POST /api/busquedas` | crear | 201 `{nombre, buscando}`; 422 errores |
| `PUT /api/busquedas/{nombre}` | editar | 200 `{nombre, buscando}` |
| `POST /api/busquedas/{nombre}/estado` `{activa}` | pausar/reanudar | 200 |
| `DELETE /api/busquedas/{nombre}` | borrar | 200 |

Las acciones que modifican mandan el header `X-Token` (token de administrador, opcional según el servidor).

## Restricciones técnicas (contrato que no se puede romper)

- **Un solo archivo HTML** con CSS y JS en línea; **JavaScript vanilla**, sin frameworks ni paso de build. Única dependencia externa permitida: Chart.js 4 por CDN (jsdelivr); una fuente de Google Fonts es opcional.
- Mantener literalmente la línea `const DATOS = /*__DATOS__*/null;` (Python reemplaza el marcador por el JSON).
- Debe funcionar abierto como **archivo local** (sin servidor) en modo solo lectura.
- Links entre reportes relativos (`index.html`, `<slug>.html`); formulario de suscripción en `suscribirse`.
- Escapar todo texto que venga de los datos (hay nombres de búsquedas y aerolíneas libres).
- `localStorage` solo para el token, siempre con try/catch.
- Mantener la accesibilidad: foco visible, navegación por teclado en tablas, sugerencias y modales, `aria-sort` en columnas ordenadas, `role="dialog"` en modales, contraste AA, textos alternativos.
- Modo oscuro automático.
- Rendimiento: puede haber 5–10 búsquedas y tablas de hasta 30 filas; la lista de aeropuertos tiene ~4.000 entradas (filtrar en el cliente).

## Criterios de diseño

- **Jerarquía clara**: lo primero que se ve es el precio más bajo y sus fechas; después "cómo conviene comprar"; después el detalle.
- **Limpio y elegante**: mucho aire, pocas líneas divisorias, tipografía tabular en números, colores con significado (no decorativos).
- **Responsive real**: en celular, KPIs en 2 columnas o carrusel; formulario en pantalla completa con botones fijos abajo; tablas convertidas a un formato legible; acciones de la búsqueda accesibles (ej. menú "⋯" con Editar, Pausar, Borrar).
- **Estados completos**: vacío, cargando, error, sin servidor, pausada, parcial, sin datos, con y sin equipaje, con y sin varios destinos, con y sin exploración, con y sin escalas armadas.
- **Microcopys** en castellano rioplatense, claros y cortos.

## Qué NO hacer

- No agregues funciones que no están en esta lista (login, cuentas de usuario, compra dentro de la app, mapas, filtros de aerolínea, calendario de precios nuevo, favoritos, compartir en redes, etc.).
- No saques ninguna función de las secciones A a P.
- No cambies los nombres de los campos del JSON ni de la API.
- No uses React, Vue, Tailwind por CDN ni otras librerías.

## Checklist antes de entregar

- [ ] Todas las secciones A–P diseñadas en celular, tablet y escritorio.
- [ ] Modo claro y oscuro.
- [ ] Los 3 modos de apertura (servidor, archivo local, estática) y los 2 modos de página (índice, individual).
- [ ] Formulario completo con todos sus estados (país con panel, ciudad, aeropuerto, errores, estimación, escalas armadas, equipaje, solo ida).
- [ ] Tablas ordenables y usables en celular.
- [ ] Archivo HTML final que respeta el contrato técnico.
