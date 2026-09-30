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
    "M2", "M3", "GLL", "PZA", "DIA", "DIAS", "MES", "MESES", "SET", "DOC", "ST",
}

# Codigo de servicio SAP que Cencosud pone delante del concepto: "ZPGJ Gasto
# Comun fijo", "ZINT Intereses Punitorios". Va en mayusculas y el concepto no.
CODIGO_SAP_RE = re.compile(r"Z[A-Z]{3}")

CONECTORES_SUELTOS = {"DEL", "AL", "DE", "A", "-", "AL.", "Y"}

# Una linea de concepto acaba en dos a cinco importes separados por espacios.
# Cada importe tiene que llevar algun digito: un punto suelto al final de una
# glosa ("VTAS: 253525.77 .") no es una columna.
LINEA_DETALLE_RE = re.compile(
    r"^(?P<cuerpo>.*?[A-Za-z].*?)\s+"
    r"(?P<numeros>(?:-?[\d.,]*\d[\d.,]*\s+){1,4}-?[\d.,]*\d[\d.,]*)$"
)

# Glosas informativas que llevan cifras pero no son conceptos: la base de la
# renta variable ("%RV: 8.50 - VTAS: 253525.77") repite las ventas del mes.
GLOSA_INFORMATIVA_RE = re.compile(r"%\s*[A-Z]{2,6}\s*:|\bVTAS\s*:|\bVENTAS\s*:")


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
        if codigo is None and len(tokens) > 1 and CODIGO_SAP_RE.fullmatch(token):
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


def _anotar_continuacion(fila, linea):
    """Guarda la linea que completa un concepto y rescata su periodo.

    Hay proveedores que ponen el periodo y el local en una segunda linea bajo
    el concepto: "ZPGJ Gasto Comun fijo ... 4,344.03" y debajo
    "*01.09.2026-30.09.2026-Gastos Fijos - LC Local S2012".
    """
    fila["glosa"] = collapse_spaces(f"{fila.get('glosa') or ''} {linea}")
    if not fila.get("desde"):
        desde, hasta, _ = extraer_periodo(linea)
        if desde:
            fila["desde"], fila["hasta"] = desde, hasta


def extraer_lineas_detalle(texto):
    """Conceptos de la factura, cada uno con todos sus importes de la fila.

    Cada fila lleva ademas en "glosa" las lineas sin importes que la siguen,
    que es donde algunos proveedores ponen el periodo y el local.
    """
    filas = []
    ultima = None  # la fila a la que pertenecen las lineas de continuacion

    for linea in zona_detalle(texto):
        limpia = collapse_spaces(linea)
        if not limpia:
            continue

        plano = normalize(limpia)
        if any(ruido in plano for ruido in RUIDO_DETALLE):
            ultima = None
            continue
        if NUMERO_FACTURA_RE.search(limpia):
            continue

        match = LINEA_DETALLE_RE.match(limpia)
        if not match or GLOSA_INFORMATIVA_RE.search(plano):
            # Solo las dos lineas que siguen al concepto: mas abajo ya suele
            # ser el pie de la factura.
            if ultima is not None and ultima["_continuaciones"] < 2:
                _anotar_continuacion(ultima, limpia)
                ultima["_continuaciones"] += 1
            continue

        numeros = [parse_amount(token) for token in match.group("numeros").split()]
        numeros = [numero for numero in numeros if numero is not None]
        if not numeros:
            continue

        cuerpo = match.group("cuerpo")
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
            "glosa": None,
            "_continuaciones": 0,
        })
        ultima = filas[-1]

    return filas
