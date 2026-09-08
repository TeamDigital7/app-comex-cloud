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
    aplanar,
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
    # Entre el tipo y "ELECTRONICA" caben las palabras que cada pais intercala:
    # "FACTURA ELECTRONICA DE VENTA", "FACTURA EXPORTACION ELECTRONICA".
    rf"(?:\s+[A-Z]+){{0,2}}\s+ELECTR[O{REEMPLAZO}]NIC[AO]",
    re.I,
)

# El recibo por honorarios (cuarta categoria) y el recibo de servicios publicos
# son comprobantes de pago como la factura, y hasta ahora se descartaban: sus
# archivos salian en la lista de "no se pudo leer" con el motivo equivocado.
MARCADOR_RECIBO_RE = re.compile(
    rf"RECIBO\s+POR\s+HONORARIOS|RECIBO\s+DE\s+SERVICIOS?\s+P[U{REEMPLAZO}]BLICOS?"
    r"|\bRECIBO\s*(?:N[°ºO]?\.?)?\s*:",
    re.I,
)

TIPOS_DOCUMENTO = [
    (r"RECIBO\s+POR\s+HONORARIOS", "Recibo por Honorarios"),
    (rf"RECIBO\s+DE\s+SERVICIOS?\s+P[U{REEMPLAZO}]BLICOS?", "Recibo de Servicios"),
    (rf"NOTA\s+DE\s+CR[E{REEMPLAZO}]DITO", "Nota de Credito"),
    (rf"NOTA\s+DE\s+D[E{REEMPLAZO}]BITO", "Nota de Debito"),
    (r"BOLETA\s+DE\s+VENTA", "Boleta de Venta"),
    (r"FACTURA", "Factura"),
    (r"\bRECIBO\b", "Recibo de Servicios"),
]

# Un comprobante emitido fuera del Peru: no lleva RUC, ni serie y correlativo de
# SUNAT, y el titulo va en el idioma de cada pais. Se reconocen para poder
# pagarlos, dejando claro que no son comprobantes peruanos.
MARCADOR_EXTERIOR_RE = re.compile(
    r"\bTAX\s+INVOICE\b|\bINVOICE\s*(?:#|N[O°º]?\.?|NUMBER)\s*[:#]?\s*\w"
    r"|\bN[UÚ]MERO\s+DE\s+FACTURA\b|\bFACTURA\s+DE\s+EXPORTACI[OÓ]N\b"
    r"|\bFACTURA\s+EXPORTACI[OÓ]N\b|\bNIT\b|\bVAT\b|\bRUT\.?\s*:"
    r"|\bE-?FACT\b|\bEXPORTACI[OÓ]N\b|\bNIF\b|\bCUIT\b|\bTAX\s+ID\b",
    re.I,
)

# Paginas que acompañan al comprobante pero no lo son: la orden de compra, la
# cotizacion, la constancia de SUNAT, el detalle consolidado. Sus importes y su
# moneda no son los de la factura -la orden de compra imprime "$" hasta cuando
# la factura es en soles- y contaminaban la lectura.
MARCADOR_ANEXO_RE = re.compile(
    r"ORDEN\s+DE\s+COMPRA\s+NRO|COTIZACI[OÓ]N|RESULTADO\s+DE\s+(?:LA\s+)?SOLICITUD"
    r"|SUSPENSI[OÓ]N(?:ES)?\s+DE\s+4TA|REPORTE\s+CONSOLIDADO"
    r"|CERTIFICADO\s+DE\s+RESIDENCIA|ESPACIO\s+RESERVADO\s+PARA\s+LA\s+DIAN",
    re.I,
)

# "N| F004 - 00332954", "F102- 00162720", "F001 N| 00946359": el separador
# cambia en cada proveedor, incluido el simbolo de numero mal decodificado. El
# correlativo puede venir sin ceros a la izquierda ("E001-4012"), asi que basta
# con tres digitos; la serie tiene que empezar por letra para que una cuenta
# bancaria no pase por numero de comprobante.
# La serie termina siempre en dos digitos (F001, E001, F102, FF01). Exigirlo
# evita que "AV. JAVIER PRADO ESTE 4200" se lea como la serie "ESTE-4200".
# Con guion es la forma normal: "F105- 00206001", "Nro: E001- 19",
# "Recibo :SB01-0645497702". Ahi el correlativo puede ser de una sola cifra: los
# recibos por honorarios recien empezados ("E001-19") se descartaban por exigir
# tres, y el archivo entero salia como ilegible.
# La serie es letra + caracter + dos digitos (F105, FE01, E001, SB01), lo que
# evita que "AV. JAVIER PRADO ESTE 4200" pase por serie y correlativo.
NUMERO_CON_GUION_RE = re.compile(
    rf"\b([A-Z][A-Z0-9]\d{{2}})\s*(?:N[°ºO{REEMPLAZO}]?\.?\s*)?[-–—]\s*(\d{{1,10}})\b",
    re.I,
)

# Sin guion solo se acepta cuando el proveedor marca el numero ("F001 N| 00946359").
NUMERO_CON_MARCA_RE = re.compile(
    rf"\b([A-Z][A-Z0-9]\d{{2}})\s*N[°ºO{REEMPLAZO}]?\.?\s*(\d{{3,10}})\b",
    re.I,
)

# Se conserva el nombre anterior porque otros modulos lo usan para descartar del
# detalle las lineas que llevan un numero de comprobante.
NUMERO_FACTURA_RE = NUMERO_CON_GUION_RE


def detect_tipo_documento(texto):
    plano = normalize(texto)
    for patron, etiqueta in TIPOS_DOCUMENTO:
        if re.search(patron, plano):
            return etiqueta
    return None


def detect_numero_factura(texto):
    match = NUMERO_CON_GUION_RE.search(texto or "") or NUMERO_CON_MARCA_RE.search(texto or "")
    if not match:
        return None
    return f"{match.group(1).upper()}-{match.group(2)}"


def es_pagina_de_documento(texto):
    """Si la pagina lleva el titulo de un comprobante de pago peruano."""
    plano = normalize(texto or "")
    if MARCADOR_ANEXO_RE.search(plano):
        return False
    return bool(MARCADOR_DOCUMENTO_RE.search(plano) or MARCADOR_RECIBO_RE.search(plano))


def es_pagina_anexo(texto):
    """Si la pagina es un adjunto (orden de compra, cotizacion, constancia)."""
    return bool(MARCADOR_ANEXO_RE.search(normalize(texto or "")))


def es_documento_exterior(texto):
    """Si el documento lo emitio un proveedor del exterior.

    No lleva serie y correlativo de SUNAT, asi que se reconoce por el titulo y
    por el identificador fiscal de su pais (NIT, VAT, RUT). Se exige ademas que
    no haya un numero de comprobante peruano, para no confundir una factura
    nacional que mencione de paso a un proveedor extranjero.
    """
    plano = normalize(texto or "")
    if MARCADOR_ANEXO_RE.search(plano):
        return False
    if detect_numero_factura(texto):
        return False
    return bool(MARCADOR_EXTERIOR_RE.search(plano))


# "Nro." tambien encabeza el numero de una direccion ("CAL. AMADOR MERINO REYNA
# NRO. 267"). Las lineas de direccion se saltan para no tomar el numero de la
# calle como numero de factura.
LINEA_DIRECCION_RE = re.compile(
    r"(?i)DIRECCI[OÓ]N|\bCAL\b|\bAV\b|\bJR\b|\bMZA\b|\bCALLE\b|\bCRA\b"
    r"|\bINT\b|\bURB\b|\bDPTO\b|\bPISO\b|\bOFICINA\b|\bOF\b|\bLOTE\b",
)

# Como se numeran las facturas fuera del Peru: "Invoice #: 254084766",
# "Numero de factura: 5649775837", "No. ADSM 5270", "Nro. TM 6778", "Nº 198".
# Primero las marcas inequivocas ("Invoice #", "Numero de factura") y solo
# despues el "Nro." suelto, que en un documento aparece muchas veces. Las
# palabras que pueden ir entre "Factura" y su numero estan enumeradas a
# proposito: dejar cualquier texto por medio hacia que "numeracion de
# facturacion N°18764094446504" pasara por numero de la factura.
RELLENO_TITULO = r"(?:[^\S\n]+(?:ELECTR[OÓ]NIC[AO]|DE|VENTA|EXPORTACI[OÓ]N))*"

ETIQUETAS_NUMERO_EXTERIOR = (
    rf"\b(?:INVOICE|FACTURA)\b{RELLENO_TITULO}\s{{0,3}}(?:#|N[O°º]\.?|NUMBER|N[U\u00da]MERO\b)",
    r"N[U\u00da]MERO\s+DE\s+FACTURA",
)

# El "Nro." a secas se prueba al final, cuando ninguna marca clara dio nada.
ETIQUETA_NUMERO_SUELTA = r"\bNRO\.?|\bNO\.|N[°º]"

# Un candidato a numero: unas siglas opcionales y luego cifras.
CANDIDATO_NUMERO_RE = re.compile(r"([A-Z]{0,6}[\s\-]?\d[\w\-]{1,19})", re.I)

# Lo que nunca es el numero del comprobante aunque venga detras de un "No.":
# el identificador fiscal del proveedor, el de una orden de compra o el codigo
# de autorizacion del documento.
CONTEXTO_AJENO_RE = re.compile(
    r"(?i)(?:VAT|TAX|CNPJ|NIT|RUT|RUC|CUIT|NIF|CAE|ACCOUNT|CUENTA|IBAN|SWIFT"
    r"|ORDEN|ORDER|COMPRA|IDENTIFICACI[OÓ]N|CUOTA|PEDIDO|TEL[EÉ]FONO|TELF)"
    r"[\s\S]{0,12}$"
)

VENTANA_NUMERO = 150

# La factura de exportacion uruguaya rotula sus columnas y pone los datos en la
# linea siguiente: "Tipo Serie Numero" / "e-Fact. Exp. A 346".
CABECERA_SERIE_NUMERO_RE = re.compile(r"TIPO\s+SERIE\s+N[U\u00da]MERO", re.I)
SERIE_NUMERO_SUELTOS_RE = re.compile(r"\b([A-Z]{1,4})\s+(\d{1,10})\s*$")


def _antes_en_la_linea(texto, posicion, maximo=40):
    """Lo que precede a una posicion dentro de su propia linea.

    Mirar mas atras de la linea daba falsos positivos: la direccion de la linea
    anterior descartaba un numero que estaba perfectamente bien.
    """
    inicio_linea = texto.rfind("\n", 0, posicion) + 1
    return texto[max(inicio_linea, posicion - maximo):posicion]


def _candidato_valido(texto, inicio, candidato):
    """Descarta lo que no puede ser el numero de un comprobante."""
    previo = _antes_en_la_linea(texto, inicio)
    if CONTEXTO_AJENO_RE.search(previo):
        return False
    if LINEA_DIRECCION_RE.search(previo):
        return False

    solo_digitos = re.sub(r"\D", "", candidato)
    # Un RUC peruano de once digitos es el de la empresa, no un numero de
    # factura del exterior.
    if re.fullmatch(r"(?:10|15|16|17|20)\d{9}", solo_digitos):
        return False
    return True


def _es_parte_de_fecha(texto, inicio, fin):
    return texto[inicio - 1:inicio] == "/" or texto[fin:fin + 1] == "/"


def _numero_tras_etiqueta(texto, etiqueta):
    """Primer candidato aceptable detras de una de las marcas de numeracion."""
    for marca in re.finditer(etiqueta, texto, re.I):
        if LINEA_DIRECCION_RE.search(_antes_en_la_linea(texto, marca.start())):
            continue

        ventana = texto[marca.end():marca.end() + VENTANA_NUMERO]
        for candidato in CANDIDATO_NUMERO_RE.finditer(ventana):
            inicio = marca.end() + candidato.start()
            fin = marca.end() + candidato.end()
            if _es_parte_de_fecha(texto, inicio, fin):
                continue
            if not _candidato_valido(texto, inicio, candidato.group(1)):
                continue
            numero = collapse_spaces(candidato.group(1))
            if numero:
                return numero.upper().replace(" ", "-")
    return None


def _numero_bajo_encabezado(texto):
    """La factura de exportacion que rotula las columnas y los datos debajo."""
    lineas = split_lines(texto)
    for indice, linea in enumerate(lineas[:-1]):
        if not CABECERA_SERIE_NUMERO_RE.search(linea):
            continue
        suelto = SERIE_NUMERO_SUELTOS_RE.search(collapse_spaces(lineas[indice + 1]) or "")
        if suelto:
            return f"{suelto.group(1).upper()}-{suelto.group(2)}"
    return None


def detect_numero_exterior(texto):
    """Numero del comprobante del exterior, tal como lo imprime el proveedor."""
    texto = texto or ""

    for etiqueta in ETIQUETAS_NUMERO_EXTERIOR:
        numero = _numero_tras_etiqueta(texto, etiqueta)
        if numero:
            return numero

    return _numero_bajo_encabezado(texto) or _numero_tras_etiqueta(
        texto, ETIQUETA_NUMERO_SUELTA
    )

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
        tiene_titulo = es_pagina_de_documento(texto) and not es_pagina_anexo(texto)

        # La segunda hoja de una factura repite su titulo y su numero. Es una
        # continuacion, no un anexo: tratarla como anexo dejaba fuera el pie de
        # totales, que es justo lo que muchas facturas imprimen ahi.
        continua = (
            tiene_titulo and numero and documentos
            and documentos[-1]["numero"] == numero
        )
        abre = tiene_titulo and bool(numero) and not continua

        if abre:
            documentos.append({
                "numero": numero,
                "pagina_inicial": pagina["numero"],
                "paginas": [pagina],
                "anexos": [],
            })
        elif continua:
            documentos[-1]["paginas"].append(pagina)
        elif documentos:
            documentos[-1]["anexos"].append(pagina)

    if not documentos:
        documentos = _rescatar(paginas)

    return documentos


def _rescatar(paginas):
    """Ultimo intento cuando ninguna pagina lleva titulo de comprobante.

    Solo se miran las paginas que no son anexos: la orden de compra que se
    adjunta trae sus propios importes y su propia moneda -imprime "$" aunque la
    factura sea en soles- y tomarlos como si fueran los de la factura es peor
    que no leer nada.
    """
    con_texto = [pagina for pagina in paginas if (pagina["texto"] or "").strip()]
    propias = [pagina for pagina in con_texto if not es_pagina_anexo(pagina["texto"])]
    if not propias:
        return []

    anexos = [pagina for pagina in con_texto if es_pagina_anexo(pagina["texto"])]
    texto = "\n".join(pagina["texto"] for pagina in propias)

    numero = detect_numero_factura(texto)
    if numero:
        return [{
            "numero": numero,
            "pagina_inicial": propias[0]["numero"],
            "paginas": propias,
            "anexos": anexos,
            "sin_titulo": True,
        }]

    if es_documento_exterior(texto):
        return [{
            "numero": detect_numero_exterior(texto),
            "pagina_inicial": propias[0]["numero"],
            "paginas": propias,
            "anexos": anexos,
            "exterior": True,
        }]

    return []


def aviso_paginas(paginas):
    """Aviso sobre las paginas que no se pudieron leer, si es que las hay.

    Solo se avisa de la pagina que se quedo sin texto porque el OCR no estaba o
    fallo. Una hoja en blanco pasa por el OCR, no da texto y no es ningun
    problema: avisar de ella solo hacia ruido en la hoja Auditoria.
    """
    sin_leer = [
        pagina["numero"] for pagina in paginas
        if not (pagina["texto"] or "").strip() and not pagina.get("ocr")
        and not pagina.get("ocr_intentado")
    ]

    disponible, motivo = estado_ocr()
    fallo = ultimo_error()

    if not sin_leer and not (fallo and any(p.get("ocr_intentado") for p in paginas)):
        return None

    partes = []
    if sin_leer:
        partes.append("Paginas escaneadas (sin texto): "
                      + ", ".join(str(numero) for numero in sin_leer))
    if not disponible:
        partes.append(f"El OCR no esta disponible: {motivo}")
    elif fallo:
        partes.append(fallo)

    return ". ".join(partes) + "." if partes else None


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

    if es_documento_exterior(texto):
        # No es un comprobante peruano: no tiene sentido reclamarle la serie y
        # el correlativo de SUNAT.
        return detalle or None

    faltan = []
    if not es_pagina_de_documento(texto):
        faltan.append('no se encontro el titulo del comprobante ("FACTURA ELECTRONICA" y similares)')
    if not detect_numero_factura(texto) and not detect_numero_exterior(texto):
        faltan.append("no se encontro la serie y correlativo (F001-00001234)")

    if not faltan:
        return detalle or None

    # El texto se leyo bien; lo que fallo fue reconocer el comprobante dentro de
    # el. Decirlo asi evita el diagnostico falso que se daba antes -"PDF
    # escaneado, falta el OCR"- en archivos que traian todo su texto.
    return (detalle + "El PDF si trae texto, pero no se reconocio el comprobante: "
            + "; ".join(faltan))


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
}

CONECTORES_SUELTOS = {"DEL", "AL", "DE", "A", "-", "AL.", "Y"}

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


def extraer_lineas_detalle(texto):
    """Conceptos de la factura, cada uno con todos sus importes de la fila."""
    filas = []

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
        })

    return filas
