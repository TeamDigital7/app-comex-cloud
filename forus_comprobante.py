"""Identificacion de comprobantes electronicos peruanos.

Estas piezas nacieron leyendo facturas reales de centros comerciales y son las
que evitan los tres errores tipicos: tomar una cuenta bancaria por un numero de
factura, quedarse con el numero del documento que una nota de credito corrige,
y tratar los anexos -detalle de facturacion, estado de cuenta- como si fueran
comprobantes aparte. Las usan tanto Contabilidad como Arriendos, para que las
dos identifiquen igual.
"""
import re

from forus_ocr import estado_ocr, ultimo_error
from forus_parsing import (
    REEMPLAZO,
    collapse_spaces,
    extraer_periodo,
    normalize,
    parse_amount,
    split_lines,
)


# El titulo del comprobante electronico. Es lo unico que distingue de verdad la
# pagina de la factura de sus anexos (detalle de facturacion, estado de cuenta),
# que tambien nombran facturas y llevan numeros de serie.
MARCADOR_DOCUMENTO_RE = re.compile(
    r"\b(?:FACTURA|BOLETA\s+DE\s+VENTA"
    rf"|NOTA\s+DE\s+D[E{REEMPLAZO}]BITO|NOTA\s+DE\s+CR[E{REEMPLAZO}]DITO)"
    rf"\s+ELECTR[O{REEMPLAZO}]NICA",
    re.I,
)

TIPOS_DOCUMENTO = [
    (rf"NOTA\s+DE\s+CR[E{REEMPLAZO}]DITO", "Nota de Credito"),
    (rf"NOTA\s+DE\s+D[E{REEMPLAZO}]BITO", "Nota de Debito"),
    (r"BOLETA\s+DE\s+VENTA", "Boleta de Venta"),
    (r"FACTURA", "Factura"),
]

# "N| F004 - 00332954", "F102- 00162720", "F001 N| 00946359": el separador
# cambia en cada proveedor, incluido el simbolo de numero mal decodificado. El
# correlativo puede venir sin ceros a la izquierda ("E001-4012"), asi que basta
# con tres digitos; la serie tiene que empezar por letra para que una cuenta
# bancaria no pase por numero de comprobante.
# La serie termina siempre en dos digitos (F001, E001, F102, FF01). Exigirlo
# evita que "AV. JAVIER PRADO ESTE 4200" se lea como la serie "ESTE-4200".
NUMERO_FACTURA_RE = re.compile(
    rf"\b([FBE][A-Z0-9]\d{{2}})\s*(?:N[°ºO{REEMPLAZO}]?\.?\s*)?[-–]?\s*(\d{{3,10}})\b",
    re.I,
)

def detect_tipo_documento(texto):
    plano = normalize(texto)
    for patron, etiqueta in TIPOS_DOCUMENTO:
        if re.search(patron, plano):
            return etiqueta
    return None


def detect_numero_factura(texto):
    match = NUMERO_FACTURA_RE.search(texto or "")
    if not match:
        return None
    return f"{match.group(1).upper()}-{match.group(2)}"


def es_pagina_de_documento(texto):
    return bool(MARCADOR_DOCUMENTO_RE.search(normalize(texto or "")))

def agrupar_documentos(paginas):
    """Una factura por cada pagina con titulo de comprobante; el resto son anexos.

    El criterio de arriba es estricto a proposito: es lo que impide que el
    estado de cuenta que acompana a una factura se cuele como un comprobante
    aparte. Pero si el PDF entero no produce ni un documento, se toma su primera
    pagina igualmente: mas vale leerla a medias y avisar, que descartar el
    archivo en silencio.
    """
    documentos = []

    for pagina in paginas:
        texto = pagina["texto"]
        numero = detect_numero_factura(texto)
        abre = es_pagina_de_documento(texto) and bool(numero) and (
            not documentos or documentos[-1]["numero"] != numero
        )

        if abre:
            documentos.append({
                "numero": numero,
                "pagina_inicial": pagina["numero"],
                "paginas": [pagina],
                "anexos": [],
            })
        elif documentos:
            documentos[-1]["anexos"].append(pagina)

    if not documentos:
        # El rescate exige encontrar al menos la serie y correlativo. Sin eso no
        # hay comprobante que leer, y armar una fila con lo que haya suelto es
        # peor que no leer nada: los PDFs escaneados suelen traer adjunta una
        # orden de compra, y sus importes no son los de la factura.
        con_texto = [pagina for pagina in paginas if (pagina["texto"] or "").strip()]
        numero = detect_numero_factura("\n".join(p["texto"] for p in con_texto))
        if con_texto and numero:
            documentos.append({
                "numero": numero,
                "pagina_inicial": con_texto[0]["numero"],
                "paginas": con_texto,
                "anexos": [],
                "sin_titulo": True,
            })

    return documentos


def diagnostico_lectura(paginas):
    """Por que un PDF no se pudo reconocer. Se escribe en la hoja Auditoria."""
    if not paginas:
        return "El PDF no tiene paginas"

    vacias = [p["numero"] for p in paginas
              if not (p["texto"] or "").strip() and not p.get("ocr")]
    texto = "\n".join(p["texto"] for p in paginas)

    if not texto.strip():
        disponible, motivo = estado_ocr()
        if not disponible:
            return f"PDF escaneado y el OCR no esta disponible. {motivo}"
        fallo = ultimo_error()
        if fallo:
            return f"PDF escaneado y el OCR no pudo leerlo. {fallo}"
        return ("PDF escaneado: el OCR no logro sacar texto de la imagen. "
                "Hace falta el PDF original que emitio el proveedor")

    if vacias:
        detalle = ("Paginas escaneadas (sin texto): "
                   + ", ".join(str(n) for n in vacias) + ". ")
        # Si quedaron paginas sin leer, hay que decir por que no las cogio el OCR.
        disponible, motivo = estado_ocr()
        if not disponible:
            detalle += f"El OCR no esta disponible: {motivo}. "
        elif ultimo_error():
            detalle += f"{ultimo_error()}. "
    else:
        detalle = ""

    faltan = []
    if not es_pagina_de_documento(texto):
        faltan.append('no se encontro el titulo del comprobante ("FACTURA ELECTRONICA" y similares)')
    if not detect_numero_factura(texto):
        faltan.append("no se encontro la serie y correlativo (F001-00001234)")

    if not faltan:
        return detalle or None
    return detalle + "Se leyo a medias: " + "; ".join(faltan)


def deducir_totales_por_suma(texto):
    """Rescata base, IGV y total cuando vienen sueltos bajo sus encabezados.

    Algunos proveedores imprimen la fila de totales sin repetir las etiquetas,
    dejando solo tres importes. Solo se aceptan si el tercero es la suma de los
    dos primeros, que es la comprobacion que los identifica sin ambiguedad.
    """
    for linea in split_lines(texto):
        importes = re.findall(r"(?:S/|US\s*\$|\$)\s*(-?[\d.,]+)", linea)
        if len(importes) != 3:
            continue
        valores = [parse_amount(importe) for importe in importes]
        if any(valor is None for valor in valores):
            continue
        if abs((valores[0] + valores[1]) - valores[2]) <= 0.10:
            return valores[0], valores[1], valores[2]
    return None, None, None


# Encabezado de la tabla de detalle. Se exige una palabra de descripcion y otra
# de columna, en una linea corta y sin comas: el pie legal de las facturas
# tambien habla de "conceptos" e "importe total" y no debe confundirse con el
# encabezado de la tabla.
PALABRAS_DESCRIPCION = ("DESCRIPCION", "DETALLE", "CONCEPTO", "ARTICULO")
PALABRAS_COLUMNA = (
    "CANTIDAD", "CANT", "UNIDAD", "UM", "U. M.", "IMPORTE", "VALOR",
    "PRECIO", "TOTAL", "DSCTO", "DESCUENTO", "CODIGO", "PV.",
)

FIN_DETALLE_RE = re.compile(
    r"\bSON\s*[:.]|OP\.\s*GRAVADA|OP\.\s*EXONERADA|OP\.\s*INAFECTA"
    r"|TOTAL\s+GRAVADA|TOTAL\s+GRAVADO|IMPORTE\s+TOTAL|VALOR\s+VENTA\s+TOTAL"
    r"|TOTAL\s+NO\s+GRAVADO|DOCUMENTOS\s+REFERENCIADOS"
)


def es_encabezado_detalle(linea):
    plano = normalize(linea)
    if len(plano) > 90 or "," in plano:
        return False
    if not any(palabra in plano for palabra in PALABRAS_DESCRIPCION):
        return False
    return any(palabra in plano for palabra in PALABRAS_COLUMNA)

# Dentro de la zona de detalle todavia se cuelan avisos de pago y de detraccion.
RUIDO_DETALLE = (
    "CUENTA", "BANCO", "BCP", "RESOLUCION", "SUNAT", "DECRETO", "DETRACCION",
    "RETENCION", "IDENTIFIQUESE", "DEPOSITO", "ABONAR", "SIRVASE", "CANCELAR",
    "AUTORIZADO", "REPRESENTACION", "AGENTES", "SPOT", "CCI", "TELF", "FAX",
)

# Unidades de medida que aparecen pegadas al concepto. "LOCAL" no entra a
# proposito: se comeria el final de "RENTA MINIMA LOCAL".
UNIDADES = {
    "UN", "UND", "UNI", "NIU", "ZZ", "LOCALES", "SER", "KGM", "KG", "MTR",
    "M2", "M3", "GLL", "PZA", "DIA", "DIAS", "MES", "MESES", "SET", "DOC",
    "ST", "SRV", "GLN",
}

CONECTORES_SUELTOS = {"DEL", "AL", "DE", "A", "-", "AL.", "Y"}

# Codigo de material de las facturas SAP (Cencosud): ZPGJ, ZREL, ZRAC, ZINT,
# ZREP, ZADP, ZVPM. Son cuatro letras que empiezan por Z y van delante de la
# descripcion; sin quitarlas el concepto no se puede normalizar.
CODIGO_SAP_RE = re.compile(r"^Z[A-Z]{3}$")

# El local dentro de la linea del concepto: "- LC Local S2012", "- LC 5A",
# "- LC M1104". Una misma factura puede traer varios locales, uno por linea,
# asi que este dato manda sobre el local de la cabecera.
LOCAL_EN_LINEA_RE = re.compile(
    r"\bLC\s+(?:LOCAL\s+)?([A-Z]{0,3}\d{1,5}[A-Z]?(?:\s*[/-]\s*[A-Z]{0,2}\d{1,5}[A-Z]?)*)\b",
    re.I,
)

# Una linea que continua el concepto de la de arriba, no un concepto nuevo:
# "*01.09.2026-30.09.2026-Gastos Fijos - LC Local S2012" (Cencosud) o
# "AGOSTO 2026 - RENTA VARIABLE - %RV: 8.50 - VTAS: 253525.77" (Real Plaza),
# donde ese numero es la venta del mes y no un importe que se cobre.
INICIO_CONTINUACION_RE = re.compile(r"^\s*[*+>.\-]")

# Una linea de concepto acaba en dos a cinco importes separados por espacios.
LINEA_DETALLE_RE = re.compile(
    r"^(?P<cuerpo>.*?[A-Za-z].*?)\s+(?P<numeros>(?:-?[\d.,]+\s+){1,4}-?[\d.,]+)$"
)


def zona_detalle(texto):
    """Lineas entre el encabezado de la tabla de detalle y los totales."""
    lineas = split_lines(texto)

    inicio = None
    for indice, linea in enumerate(lineas):
        if es_encabezado_detalle(linea):
            inicio = indice + 1
            break

    if inicio is None:
        return lineas  # sin encabezado reconocible, se revisa todo con cuidado

    fin = len(lineas)
    for indice in range(inicio, len(lineas)):
        if FIN_DETALLE_RE.search(normalize(lineas[indice])):
            fin = indice
            break

    return lineas[inicio:fin]


def limpiar_concepto(cuerpo, periodo_texto):
    """Deja solo la descripcion: sin codigo, cantidad, unidad ni periodo."""
    texto = cuerpo
    if periodo_texto:
        texto = texto.replace(periodo_texto, " ")

    tokens = (collapse_spaces(texto) or "").split()
    codigo = None
    cantidad = None
    unidad = None

    # Cabeza: codigo, cantidad y unidad, en el orden que traiga cada proveedor.
    while tokens:
        token = tokens[0]
        plano = normalize(token)

        if re.fullmatch(r"-?[\d.,]+", token):
            if cantidad is None:
                cantidad = parse_amount(token)
            tokens.pop(0)
            continue
        if plano in UNIDADES and unidad is None:
            unidad = plano
            tokens.pop(0)
            continue
        if codigo is None and re.fullmatch(r"[A-Z0-9][A-Z0-9\-]{2,14}", plano) and re.search(r"\d", plano):
            codigo = token
            tokens.pop(0)
            continue
        # Codigo de material SAP: solo se quita si detras queda descripcion.
        if codigo is None and CODIGO_SAP_RE.fullmatch(plano) and len(tokens) > 1:
            codigo = token
            tokens.pop(0)
            continue
        break

    # Cola: unidad y conectores que quedaron sueltos al quitar el periodo.
    while tokens:
        plano = normalize(tokens[-1])
        if (plano in UNIDADES and unidad is None) or plano in CONECTORES_SUELTOS:
            if plano in UNIDADES:
                unidad = plano
            tokens.pop()
            continue
        break

    return collapse_spaces(" ".join(tokens)), codigo, cantidad, unidad


def _local_de_linea(texto):
    """Local escrito dentro de la linea del concepto, si lo trae."""
    match = LOCAL_EN_LINEA_RE.search(normalize(texto or ""))
    if not match:
        return None
    return collapse_spaces(match.group(1))


def _es_fila_de_concepto(linea, numeros, columnas):
    """Si la linea abre un concepto nuevo o solo continua el de arriba.

    Las facturas parten un concepto en dos lineas mas a menudo de lo que
    parece: Cencosud pone debajo el periodo y el local ("*01.09.2026-
    30.09.2026-Gastos Fijos - LC Local S2012") y Real Plaza, el detalle del
    calculo de la renta variable ("AGOSTO 2026 - RENTA VARIABLE - %RV: 8.50 -
    VTAS: 253525.77"). Esa segunda linea no es un cobro: si se toma como
    concepto, la factura sale con una fila de mas y con las ventas del mes
    metidas como si fueran un importe a pagar.

    La senal que distingue una de otra es la forma de la fila: los conceptos de
    verdad traen todas las columnas de importes de la tabla -cantidad, valor
    unitario, total-, y las continuaciones traen a lo sumo un numero suelto.
    """
    if INICIO_CONTINUACION_RE.match(linea):
        return False
    # La tabla tiene varias columnas de importes: una linea con un solo numero
    # no es una fila de la tabla.
    if columnas >= 2 and len(numeros) < 2:
        return False
    return True


def extraer_lineas_detalle(texto):
    """Conceptos de la factura, cada uno con todos sus importes de la fila.

    Cada concepto sale con el periodo, el local y los importes de su propia
    linea, incluido lo que venga escrito en las lineas de continuacion.
    """
    candidatos = []

    for linea in zona_detalle(texto):
        limpia = collapse_spaces(linea)
        if not limpia:
            continue

        plano = normalize(limpia)
        if any(ruido in plano for ruido in RUIDO_DETALLE):
            continue
        if NUMERO_FACTURA_RE.search(limpia):
            continue

        match = LINEA_DETALLE_RE.match(limpia)
        if not match:
            # Sin importes al final no puede ser un concepto, pero si puede
            # traer el periodo o el local del concepto de arriba.
            candidatos.append((limpia, None, None))
            continue

        numeros = [parse_amount(token) for token in match.group("numeros").split()]
        numeros = [numero for numero in numeros if numero is not None]
        candidatos.append((limpia, match.group("cuerpo"), numeros))

    # Cuantas columnas de importes tiene la tabla de esta factura. Se mide sobre
    # las propias lineas para no depender del formato de cada emisor.
    columnas = max(
        [len(numeros) for _, cuerpo, numeros in candidatos if cuerpo and numeros],
        default=0,
    )

    filas = []
    for limpia, cuerpo, numeros in candidatos:
        es_concepto = bool(cuerpo) and bool(numeros) and _es_fila_de_concepto(
            limpia, numeros, columnas
        )

        if not es_concepto:
            if filas:  # continuacion del concepto anterior
                filas[-1]["continuacion"] = collapse_spaces(
                    " ".join(x for x in [filas[-1]["continuacion"], limpia] if x)
                )
            continue

        desde, hasta, periodo_texto = extraer_periodo(cuerpo)
        concepto, codigo, cantidad, unidad = limpiar_concepto(cuerpo, periodo_texto)

        if not concepto or len(re.findall(r"[A-Za-z]", concepto)) < 4:
            continue

        filas.append({
            "concepto": concepto,
            "codigo": codigo,
            "cantidad": cantidad,
            "unidad": unidad,
            "desde": desde,
            "hasta": hasta,
            "numeros": numeros,
            "local": _local_de_linea(cuerpo),
            "continuacion": None,
        })

    # Lo que quedo en la linea de continuacion completa el concepto: de ahi
    # salen el periodo y el local cuando el emisor no los pone en la primera.
    for fila in filas:
        continuacion = fila.get("continuacion")
        if not continuacion:
            continue
        if not fila["desde"]:
            desde, hasta, _ = extraer_periodo(continuacion)
            fila["desde"], fila["hasta"] = desde, hasta
        if not fila["local"]:
            fila["local"] = _local_de_linea(continuacion)

    return filas
