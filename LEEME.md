# Lectura Documentos Forus

Convierte `app-comex-cloud` en un portal con cuatro pantallas. El Comex actual
**no cambia de comportamiento**: se mueve tal cual a su propio archivo.

Commit base: **`2a25d7d` — "Update app_comex_cloud.py"**.

---

## 1. Qué subir, en qué orden

El orden importa. Streamlit Cloud redespliega en cada commit, así que
`app_comex_cloud.py` va **al final**: hasta ese momento la app sigue
funcionando como hoy, porque nadie importa todavía los archivos nuevos. Si
subes todo en un solo commit, mejor todavía.

| # | Ruta en el repo | Acción |
|---|---|---|
| 1 | `forus_ui.py` | **Nuevo** |
| 2 | `forus_parsing.py` | **Nuevo** |
| 3 | `forus_comprobante.py` | **Nuevo** |
| 4 | `forus_tributario.py` | **Nuevo** |
| 5 | `forus_auth.py` | **Nuevo** |
| 6 | `modules/__init__.py` | **Nuevo** |
| 7 | `modules/comex.py` | **Nuevo** |
| 8 | `modules/contabilidad.py` | **Nuevo** |
| 9 | `modules/arriendos.py` | **Nuevo** |
| 10 | `app_comex_cloud.py` | **Reemplaza** el que existe |

**Si ya subiste la versión anterior, borra del repo estos dos archivos**, que
ya no se usan:

- `modules/rrhh.py` — era el lector de boletas de pago
- `modules/alquileres.py` — se llama ahora `modules/arriendos.py`

No rompen nada si se quedan (nadie los importa), pero sobran.

**No hay que tocar** `requirements.txt`, `.devcontainer/` ni los PNG del logo.
El entrypoint sigue llamándose `app_comex_cloud.py` para no reconfigurar
Streamlit Cloud.

---

## 2. Secrets

```toml
[auth]
"liliana.vitate@forus.pe" = "la-que-ya-tienes"
"danitza.cupe@forus.pe"   = "la-que-ya-tienes"
"hugo.camara@forus.pe"    = "la-que-ya-tienes"
"romulo.rasilla@forus.pe" = "la-que-ya-tienes"
"bi@forus.pe"             = "la-que-ya-tienes"

[modulos]
"hugo.camara@forus.pe"    = "todos"
"bi@forus.pe"             = "todos"
"liliana.vitate@forus.pe" = "comex"
"danitza.cupe@forus.pe"   = "contabilidad"
"romulo.rasilla@forus.pe" = "rrhh"

# Opcional: ayuda a distinguir al proveedor del cliente en los comprobantes.
[empresa]
ruc = "20514811271"

# Opcional: la regla de Pagos sale con estos valores. Solo hay que tocarlos si
# la norma cambia.
[tributario]
umbral = 700
tasa_retencion = 3
# tasa_detraccion_defecto = 10   # ver seccion 3
```

Claves de `[modulos]`: `comex`, `contabilidad`, `rrhh`, o `todos`. Quien tenga
`contabilidad` ve sus dos pantallas; quien tenga `rrhh` ve Arriendos.

**Sin la sección `[modulos]` solo se ve Comex.** Es el valor por defecto para
que nadie pierda acceso al actualizar: **este paso hay que darlo o las
pantallas nuevas no aparecen.**

---

## 3. Las cuatro pantallas

Se eligen en el desplegable **Sitio destino** del panel lateral, en un paso.

### Comex — sin cambios

Idéntico a hoy: sufijos `_CLB.pdf`, `_VNS.pdf`, `_PRF.pdf` y el mismo Excel.

### Contabilidad - Pagos

Una fila por comprobante, con **la decisión de detracción o retención ya
tomada**, que era el objetivo: que nadie tenga que repasarlo factura por
factura.

La hoja **Pagos** trae **solo tus once columnas, en tu orden**:

| FECHA | RUC | PROVEEDOR | DOCUMENTO | GLOSA | IMPORTE | D/R | % | DETRACCION/RETENCION | ANTICIPOS | MONTO A PAGAR |

`D` es detracción y `R` retención. **ANTICIPOS va vacía**: no está en el PDF, se
completa a mano, y el `MONTO A PAGAR` la descuenta cuando la llenes.
Al lado va la hoja **Pagos ampliado**, con la moneda, el tipo de documento, el
IGV, el motivo de la decisión y de qué archivo salió cada fila.

La regla se aplica en este orden:

0. **Un comprobante del exterior** (Meta, Google, un proveedor colombiano o
   chileno) no está en el sistema tributario peruano: ni detracción ni
   retención de IGV. El motivo recuerda que la renta de no domiciliados y el
   IGV por utilización de servicios se ven aparte.
1. **Un recibo por honorarios** es renta de cuarta categoría: nunca lleva
   detracción ni retención del IGV. Ver más abajo.
2. **Una nota de crédito** no genera detracción ni retención propia: entra en
   **negativo** para que reste del total a pagar al proveedor.
3. **Si el comprobante declara detracción y pasa de S/ 700**, manda eso, con el
   porcentaje que trae impreso. Por debajo de S/ 700 no hay detracción aunque
   la leyenda del SPOT esté impresa: los centros comerciales la imprimen en
   todas sus facturas, también en un reembolso de arbitrios de S/ 130.
4. **Si no hay detracción y el total no pasa de S/ 700**, no corresponde
   retención.
5. **Si la operación no tiene IGV** (inafecta o exonerada, como los intereses
   moratorios), tampoco: la retención es del IGV, y sin IGV no hay qué retener.
6. **Un recibo de servicios públicos** (luz, agua, teléfono) está fuera del
   régimen de retención del IGV por la Res. 037-2002/SUNAT.
7. **Si pasa de S/ 700**, corresponde retención del 3%, salvo que el proveedor
   figure como agente de retención, agente de percepción o buen contribuyente.

### Recibos por honorarios: renta de cuarta

Se paga por lo que el propio recibo imprime en `Retención (8 %) IR`:

| Lo que dice el recibo | Qué hace la app |
|---|---|
| Retención mayor que cero | La respeta y la descuenta del pago |
| Cero, con la constancia de suspensión adjunta en el PDF | No retiene |
| Cero, y el recibo no pasa de S/ 1,500 | No retiene: no llega al mínimo |
| Cero, pasa de S/ 1,500 y no hay constancia | **REVISAR**, con el 8% calculado en el motivo |

El último caso es a propósito: si corresponde retener y no se retiene, la multa
la paga quien pagó el recibo.

Detracción y retención nunca se aplican juntas. La detracción se redondea a
soles enteros cuando la factura es en soles; en dólares conserva los decimales,
porque el redondeo corresponde al monto en soles del día del depósito.

### De dónde sale la tasa de detracción

Se busca en tres niveles, de más fiable a menos, y **la columna `Origen Tasa` de
la hoja ampliada dice cuál se usó** en cada fila:

| Nivel | Cuándo | `Origen Tasa` |
|---|---|---|
| 1 | El comprobante imprime el porcentaje | `comprobante` |
| 2 | Imprime el código del Anexo pero no la tasa | `tabla` |
| 3 | No imprime ninguno: se deduce de la glosa | `concepto` |

Si ninguno da resultado, la fila sale para completar a mano en vez de arriesgar
un número. **El nivel 3 marca el motivo con "VERIFICAR"**, porque la descripción
engaña: un `MANTENIMIENTO` dentro de un arrendamiento tributa como arrendamiento
(019, 10%) y no como mantenimiento de bienes muebles (020, 12%).

**La tabla de tasas hay que confirmarla y mantenerla.** Las modifica SUNAT por
resolución, y la que va en el código es un punto de partida. Se puede corregir
desde los secrets sin tocar código:

```toml
[detraccion_tasas]
"019" = 10
"020" = 12
"022" = 12
```

La tabla vigente se puede consultar dentro de la propia app, en el desplegable
"Tabla de tasas de detracción por código" de la pantalla de Pagos.

**El motivo va escrito en cada fila** de la hoja ampliada — "Importe de S/ 24.03
menor o igual al umbral de S/ 700", "El comprobante indica detracción del
12.0%", "Supera el umbral pero el proveedor figura como Agente de retención" —
para poder verificarlo de un vistazo.

**Cuando falta un dato, la fila sale como `REVISAR` en vez de arriesgar un
número.** Pasa en dos casos: factura en dólares sin tipo de cambio con el que
llevar el importe a soles, y comprobante que dice estar sujeto a detracción
pero no imprime el porcentaje (le ocurre a Mall Plaza). Para el segundo caso,
si quieres que se calcule igual, descomenta `tasa_detraccion_defecto = 10` en
los secrets: se aplicará esa tasa y el motivo dirá que salió de la
configuración, no del documento.

### Qué comprobantes lee

Además de la factura, la boleta y las notas de crédito y débito electrónicas,
lee el **recibo por honorarios** (`E001-19`, aunque el correlativo tenga una
sola cifra), el **recibo de servicios públicos** (`SB01-0645497702`) y la
**factura de un proveedor del exterior**, que no tiene serie ni correlativo de
SUNAT y numera a su manera (`Invoice #: 254084766`, `No. ADSM 5270`, `A 346`).

La **orden de compra, la cotización y las constancias** que se adjuntan al PDF
se anotan como anexos y no se leen: sus importes no son los de la factura, y su
"$" no es la moneda del comprobante.

### Comprobación aritmética

Antes de dar por bueno el importe total se comprueba que **base + IGV = total**.
Si lo leído coincide con la base y hay un IGV que sumar, el total se corrige y
la observación de esa fila lo dice. Es la red de seguridad del error que hacía
bajar facturas por su valor **sin IGV**.

### Contabilidad - Costos

Los parámetros generales de cada comprobante y su detalle. Hojas
**Documentos** (32 columnas: RUC, razón social, fechas, moneda, tipo de cambio,
operaciones gravadas / exoneradas / inafectas / gratuitas, descuentos, ISC,
IGV, otros cargos, total, detracción, orden de compra, forma de pago, guía de
remisión), **Detalle** (código, descripción, cantidad, unidad, valor unitario,
precio unitario, descuento e importe por línea), **Resumen** y **Auditoría**.
Valida que `gravadas + IGV` cuadre con el total.

### Recursos Humanos - Arriendos

El control de facturas de arriendo de locales. La hoja **Control** trae **solo
tus nueve columnas, en tu orden**, una fila por concepto:

| Fecha | Tienda | MES | Concepto | SOLES | DOLARES | # Factura | RAZON SOCIAL | FECHA DE ENTREGA |

Más las hojas **Control ampliado** (con contrato, local, periodo, importe con
IGV, observaciones), **Facturas**, **Resumen** y **Auditoría**.

- **Tienda** — el local cuando la factura lo trae (`S146 HUSH PUPPIES`) y, si
  no, el número de contrato (`9244`) o la referencia (`LCS 1070`).
- **MES** — el del periodo facturado, sacado del propio concepto.
- **SOLES / DOLARES** — el importe **sin IGV**. La columna correcta se elige
  probando cuál cuadra con la base imponible, porque cada emisor la pone en un
  sitio distinto.
- **FECHA DE ENTREGA** — va vacía: no está en el PDF, es dato de ustedes.

**Ya no hay lectura de boletas de pago.**

---

## 4. Diff contra `2a25d7d`

El original era un archivo de **2.213 líneas y 36 funciones**. Ahora son
**10 archivos, 5.079 líneas y 117 funciones**.

### Funciones heredadas del Comex: 36 de 36 conservadas

| Estado | Cantidad |
|---|---|
| Idénticas **byte a byte** (verificado por AST) | **34** |
| Modificadas a propósito | **2** |
| Eliminadas | **0** |

Las dos modificadas son `render_login_screen` (título nuevo y carga de
permisos) e `is_authenticated` (mismo cuerpo, con `current_user_modules()` al
lado). Del panel lateral de Comex se quitaron las dos líneas del desplegable
muerto "Sitio destino", cuyo sitio ocupa ahora el selector del portal.

### Archivos

| Archivo | Líneas | Funciones | Qué hace |
|---|---|---|---|
| `app_comex_cloud.py` | 62 | 0 | Arranque, login y selector de destino |
| `forus_ui.py` | 1.286 | 16 | CSS completo y componentes de pantalla |
| `forus_auth.py` | 181 | 7 | Login y permisos |
| `forus_parsing.py` | 481 | 25 | Importes, fechas, etiquetas, razón social |
| `forus_comprobante.py` | 97 | 5 | Identificación de comprobantes |
| `forus_tributario.py` | 237 | 5 | Regla de detracción y retención |
| `modules/comex.py` | 1.007 | 27 | Comex, sin cambios |
| `modules/contabilidad.py` | 940 | 18 | Pagos y Costos |
| `modules/arriendos.py` | 787 | 14 | Control de arriendos |

`forus_comprobante.py` es nuevo y merece una nota: reúne la identificación de
comprobantes que se había endurecido leyendo tus facturas, y ahora la usan
**las dos** áreas. Antes Contabilidad tenía su propia versión, más floja, y
con tus PDFs confundía una cuenta bancaria del BCP con el número de factura,
leía Lambramani como si fuera en dólares y tomaba el estado de cuenta de Mall
Plaza como si fuera un comprobante más de S/ 104.503.

---

## 5. Comprobaciones que se pasaron

**Estáticas**: los 10 archivos compilan, cero funciones huérfanas, las 34
funciones heredadas idénticas byte a byte al commit base, el CSS es cadena
literal y no f-string.

**Funcionales** (133 comprobaciones): importes en formato peruano y europeo,
fechas en cuatro formatos, Contabilidad con varios comprobantes por PDF,
Arriendos con todas sus columnas, y **26 comprobaciones solo de la regla de
detracción y retención**: el umbral por arriba y por abajo, S/ 700 exactos, la
detracción declarada ganando al umbral, el redondeo a soles enteros, el
proveedor excluido, dólares con y sin tipo de cambio, y el caso sin importe.

**AppTest**: login correcto y rechazado, las cuatro pantallas cargando sin
`st.exception` ni `st.error`, permisos por usuario y destino inválido guardado.
Arranque headless HTTP 200.

**Contra las facturas reales.** Arriendos: 7 de 7 leídas, 7 de 7 cuadran, 10
filas de control. Contabilidad - Pagos, con las 7 muestras de Contabilidad,
**los 7 resultados coinciden con lo que indicó Contabilidad**:

| Documento | Proveedor | Importe | D/R | % | Detracción | A pagar |
|---|---|---|---|---|---|---|
| F004-00330138 | Jockey Plaza | US$ 6.800,34 | D | 10 | 680,03 | 6.120,31 |
| F004-00331093 | Jockey Plaza | S/ 536,75 | | | 0,00 | 536,75 |
| F003-00011544 | Lambramani | S/ 11.027,87 | D | 10 | 1.103,00 | 9.924,87 |
| F001-00002037 | Strip Centers (NC) | S/ −11.745,83 | | | 0,00 | −11.745,83 |
| F005-00000269 | Strip Centers (ND) | S/ 82,10 | | | 0,00 | 82,10 |
| F002-00026607 | Inversiones Castelar | US$ 354,00 | D | 10 | 35,40 | 318,60 |
| E001-4012 | ILP Soluciones | S/ 2.242,00 | D | 10 | 224,00 | 2.018,00 |

En ILP el resultado se puede contrastar contra el propio PDF, que imprime
"Monto detracción: S/ 224.00" y "Monto neto pendiente de pago: S/ 2,018.00".

---

## 6. Lo que NO se hizo, y por qué

1. **La retención asume el 3% del régimen general** y que Forus es agente de
   retención. Si alguna operación va por otra tasa, se ajusta en
   `[tributario]` sin tocar código.

2. **No se consulta a SUNAT.** La condición del proveedor (agente de
   retención, buen contribuyente) se toma de lo que el propio comprobante
   declare impreso. Es lo que hay en el PDF; si necesitas contrastarlo contra
   el padrón de SUNAT, eso es un trabajo aparte.

3. **La tasa de detracción sale del comprobante, no de una tabla por tipo de
   servicio.** Es lo correcto: el proveedor ya la determinó. Cuando no la
   imprime, la fila sale como `REVISAR` salvo que configures una por defecto.

4. **"Parámetros generales" en Costos lo interpreté** como la cabecera completa
   del comprobante más su detalle de ítems. Si esperabas otra cosa, dímelo y
   lo ajusto: el motor ya lee todo, es cuestión de elegir columnas.

5. **Tienda queda vacía en la factura de Real Plaza**: no trae contrato ni
   local con etiqueta propia. Preferí dejarla vacía antes que meter un dato
   equivocado.

6. **`parse_money` de Comex se quedó como estaba.** Interpreta `1,234.00` como
   1.234 porque asume el punto como separador de miles. No lo toqué porque
   alteraría los resultados de Comex en producción. **Vale la pena revisarlo
   aparte**: si alguna factura de Columbia o Vans trae importes en formato
   americano, hoy se leen mal.

---

## 7. Si algo sale mal

Restaura `app_comex_cloud.py` desde el commit `2a25d7d`. Los archivos nuevos
pueden quedarse: sin el entrypoint nuevo, nadie los importa.

---

## 8. Correcciones sobre facturas escaneadas, importes y detracciones

Esta tanda salió de revisar **50 facturas reales** (centros comerciales,
recibos por honorarios, recibos de Claro y facturas de proveedores del
exterior). Antes se leían **26 de 50**; ahora se leen **las 50**.

### El importe que bajaba sin IGV

La búsqueda por etiqueta tenía dos fallos que se sumaban:

- **`TOTAL` acertaba dentro de `SUBTOTAL`.** En una factura que imprime
  `Subtotal S/3,022.72` antes que `Total S/3,566.81`, se bajaba la base. Es el
  error de las 78 facturas que hubo que corregir a mano. Ahora la etiqueta
  tiene que ser una palabra entera, y `SUB TOTAL` tampoco cuenta.
- **El separador entre etiqueta y valor admitía el salto de línea.** El
  encabezado de la tabla (`... Descuento Total`) se llevaba el primer número de
  la línea siguiente, que es la cantidad del ítem: de ahí los importes de 1,00.
  Ahora el importe tiene que estar en la misma línea que su etiqueta.

Encima va la comprobación **base + IGV = total**, que corrige el importe si aun
así se coló la base y lo deja anotado en la observación de la fila.

### Los archivos que decían "PDF escaneado, falta el OCR"

**Sí traían texto.** Lo que fallaba era el reconocimiento del comprobante: un
recibo por honorarios `E001-19` se descartaba por tener el correlativo de dos
cifras, y un recibo de Claro `SB01-...` porque la serie no empezaba por F, B
o E. El diagnóstico echaba la culpa al OCR y mandaba a instalar una librería
que no faltaba. Ahora el aviso distingue las dos cosas.

Del OCR de verdad se arreglaron dos cosas:

- **`'RapidOCROutput' object is not iterable`**: una página en blanco tumbaba la
  lectura de esa página en vez de devolver vacío.
- **El aviso de librería que falta mentía**: al probar las dos versiones del
  motor solo se contaba el fallo de la segunda, escondiendo la causa real.

### Las detracciones que salían en cero

La leyenda del SPOT viene partida donde acaba la caja de texto del PDF, y en las
facturas a dos columnas se le mete texto de la otra columna por medio:

```
OPERACIONES SUJETA AL SISTEMA DE PAGO DE
Autorizado mediante resolución: N°300-2014-SUNAT
OBLIGACIONES TRIBUTARIAS CON EL GOBIERNO CENTRAL
```

Buscada como una frase seguida no aparecía nunca, y toda factura de centro
comercial salía como no sujeta a detracción. Ahora se busca tolerando el hueco.
Junto con eso entró el **mínimo de S/ 700 del SPOT**, sin el cual esas mismas
facturas habrían salido detraídas hasta en un reembolso de S/ 130.

### Otros arreglos de la misma revisión

| Qué pasaba | Qué se hizo |
|---|---|
| Un recibo por honorarios llevaba retención de IGV del 3% | Va por renta de cuarta: 8%, mínimo S/ 1.500 y constancia de suspensión |
| El proveedor salía como "FECHA DE EMISIÓN (DD/MM/AA)" | La razón social se busca en toda la cabecera y se descartan los rótulos |
| El RUC del cliente pasaba por el del proveedor | El RUC que sigue a "Recibí de", "Señor(es)" o "Bill to" es el del cliente |
| La segunda hoja de la factura contaba como anexo | Si repite título y número es continuación: sus totales se leen |
| La orden de compra adjunta ponía la factura en dólares | Los anexos ya no aportan importes ni moneda |
| La fecha salía vacía con el rótulo en una línea y el dato en otra | Para fechas se mira también la línea siguiente, y el valor se valida |

### Pruebas

`pruebas_contabilidad.py` deja fijado cada uno de estos errores con un trozo del
comprobante que lo provocaba. Se ejecuta sin PDFs ni red:

```
python -m unittest pruebas_contabilidad -v
```

Sobre las 50 facturas de la revisión: 50 comprobantes leídos de 50 archivos,
0 archivos sin leer, y las 4 observaciones que quedan son las de los PDFs
realmente escaneados, avisando de que conviene repasar sus cifras.
