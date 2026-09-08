"""Facturas de arriendo de locales: una fila por concepto, en el formato de control.

Cada centro comercial arma su factura distinto, asi que la lectura se apoya en
tres cosas que si son estables: el titulo del comprobante electronico, la zona
de detalle (entre el encabezado de la tabla y la primera linea de totales) y el
cuadre contra la base imponible. Esto ultimo resuelve el problema de que la
columna del valor sin IGV este en una posicion distinta segun el emisor: se
elige la columna cuya suma cuadra con las operaciones gravadas e inafectas.
"""
import io
import re

import pandas as pd
import pdfplumber
import streamlit as st

from forus_parsing import (
    REEMPLAZO,
    buscar_mes_escrito,
    collapse_spaces,
    cuadra,
    detect_currency_document,
    extraer_periodo,
    faltantes,
    fechas_a_formato_local,
    find_date,
    find_label_value,
    find_money,
    find_percent,
    find_razon_social_emisor,
    find_ruc,
    mes_de_fecha,
    normalize,
    parse_amount,
    split_lines,
)

# Hoja Control: exactamente las columnas del Excel de Hugo, en su orden, para
# poder pegarlas directo. Todo lo demas va a la hoja de al lado.
CONTROL_COLUMNS = [
    "Fecha",
    "Tienda",
    "MES",
    "Concepto",
    "SOLES",
    "DOLARES",
    "# Factura",
    "RAZON SOCIAL",
    "FECHA DE ENTREGA",
]

# Las mismas filas con todo lo que se pudo leer, para revisar y auditar.
CONTROL_AMPLIADO_COLUMNS = CONTROL_COLUMNS + [
    "Contrato",
    "Local",
    "Tipo Documento",
    "Periodo Desde",
    "Periodo Hasta",
    "Importe con IGV",
    "Referencia",
    "RUC Emisor",
    "PDF File",
    "Pagina",
    "Observaciones",
]

FACTURA_COLUMNS = [
    "PDF File",
    "Pagina Inicial",
    "# Factura",
    "Tipo Documento",
    "Fecha",
    "Fecha Vencimiento",
    "RUC Emisor",
    "RAZON SOCIAL",
    "Contrato",
    "Local / Tienda",
    "Moneda",
    "Op. Gravadas",
    "Op. Inafectas",
    "Op. Exoneradas",
    "IGV",
    "Importe Total",
    "Detraccion %",
    "Conceptos",
    "Suma Conceptos",
    "Cuadra Base",
    "Duplicado",
    "Observaciones",
]

SUMMARY_COLUMNS = ["Metric", "Value"]

AUDIT_COLUMNS = ["PDF File", "Pagina", "Rol", "# Factura", "Detalle"]

CAMPOS_OBLIGATORIOS = ["# Factura", "Fecha", "RAZON SOCIAL"]

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
# cambia en cada proveedor, incluido el simbolo de numero mal decodificado.
NUMERO_FACTURA_RE = re.compile(
    rf"\b([FBE][A-Z0-9]{{2,3}})\s*(?:N[°ºO{REEMPLAZO}]?\.?\s*)?[-–]?\s*(\d{{5,10}})\b",
    re.I,
)

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


def elegir_columna_valor(filas, base_imponible):
    """Posicion (contada desde el final) de la columna con el valor sin IGV.

    Se prueba cada columna y se queda la primera cuya suma cuadra con la base
    imponible del documento. Si no hay base con que comparar, se usa el ultimo
    importe de la fila, que es lo habitual.
    """
    if not filas:
        return -1, False

    if base_imponible is None:
        return -1, False

    maximo = max(len(fila["numeros"]) for fila in filas)
    for posicion in range(1, maximo + 1):
        suma = 0
        completa = True
        for fila in filas:
            if len(fila["numeros"]) < posicion:
                completa = False
                break
            suma += fila["numeros"][-posicion]
        if completa and abs(suma - base_imponible) <= 0.10:
            return -posicion, True

    return -1, False


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


def extraer_cabecera(texto, pagina_inicial):
    gravadas = find_money(texto, [
        "TOTAL GRAVADAS", "TOTAL GRAVADO", "OP. GRAVADAS", "OP. GRAVADA",
        "OPERACIONES GRAVADAS", "VALOR DE VENTA",
    ])
    inafectas = find_money(texto, [
        "TOTAL INAFECTAS", "TOTAL NO GRAVADO", "OP. INAFECTAS", "OP. INAFECTA",
        "OPERACIONES INAFECTAS",
    ])
    exoneradas = find_money(texto, [
        "TOTAL EXONERADAS", "TOTAL EXONERADO", "OP. EXONERADAS", "OP. EXONERADA",
    ])
    igv = find_money(texto, [
        "IGV (18%)", "I.G.V. 18%", "IGV 18%", "TOTAL IGV 18%", "TOTAL IGV",
        "I.G.V.", "IGV",
    ])
    total = find_money(texto, ["IMPORTE TOTAL", "TOTAL A PAGAR", "TOTAL"])

    # Sin base imponible no se puede verificar nada, asi que vale la pena
    # intentar rescatarla de la fila de totales sin etiquetas.
    if gravadas is None:
        deducida, igv_deducido, total_deducido = deducir_totales_por_suma(texto)
        if deducida is not None:
            gravadas, igv, total = deducida, igv_deducido, total_deducido

    local = find_label_value(texto, [
        "LOCAL COMERCIAL", "NOMBRE DE CONTRATO", "LOCAL",
    ], max_chars=45, requiere_separador=True)
    # "LOCAL COMERCIAL" a veces es solo un encabezado de columna y lo que sigue
    # en la linea es otro encabezado, no un valor.
    if local and any(palabra in normalize(local) for palabra in
                     ("CONDICION", "PAGO", "ZONA", "PEDIDO", "ORDEN", "CODIGO")):
        local = None

    fila = {
        "Pagina Inicial": pagina_inicial,
        "# Factura": detect_numero_factura(texto),
        "Tipo Documento": detect_tipo_documento(texto),
        "Fecha": find_date(texto, [
            "FECHA DE EMISION", "FECHA EMISION", "F. EMISION", "FECHA",
        ]),
        "Fecha Vencimiento": find_date(texto, [
            "FECHA DE VENCIMIENTO", "FECHA VENCIMIENTO", "FECHA VENC",
        ]),
        "RUC Emisor": find_ruc(texto, ["R.U.C.", "RUC"]),
        "RAZON SOCIAL": find_razon_social_emisor(texto, excluir=("FORUS",)),
        "Contrato": find_label_value(texto, [
            "NRO. DE CONTRATO", "NUMERO DE CONTRATO", "NUM CONTRATO",
            "N. CONTRATO", "NRO CONTRATO", "CONTRATO",
        ], max_chars=30, requiere_separador=True),
        "Local / Tienda": local,
        "Referencia": find_label_value(texto, [
            "CON REFERENCIA A", "OBSERVACION",
        ], max_chars=60),
        "Moneda": detect_currency_document(texto),
        "Op. Gravadas": gravadas,
        "Op. Inafectas": inafectas,
        "Op. Exoneradas": exoneradas,
        "IGV": igv,
        "Importe Total": total,
        "Detraccion %": find_percent(texto, [
            "PORCENTAJE DETRACCION", "PORCENTAJE DE DETRACCION", "DETRACCION",
        ]),
    }

    base = None
    if gravadas is not None or inafectas is not None or exoneradas is not None:
        base = (gravadas or 0) + (inafectas or 0) + (exoneradas or 0)
    fila["_base"] = base

    return fila


def construir_filas_control(cabecera, filas_detalle, nombre_archivo, mes_documento=None):
    """Convierte cada concepto en una fila del control."""
    base = cabecera.get("_base")
    posicion, cuadro = elegir_columna_valor(filas_detalle, base)
    moneda = cabecera.get("Moneda")
    igv_total = cabecera.get("IGV") or 0
    suma_base = sum(
        fila["numeros"][posicion] for fila in filas_detalle
        if len(fila["numeros"]) >= abs(posicion)
    )

    # La columna Tienda lleva el local cuando la factura lo trae y, si no, el
    # numero de contrato: es lo que identifica el punto en cada proveedor.
    tienda = (
        cabecera.get("Local / Tienda")
        or cabecera.get("Contrato")
        or cabecera.get("Referencia")
    )

    control = []
    for fila in filas_detalle:
        numeros = fila["numeros"]
        valor = numeros[posicion] if len(numeros) >= abs(posicion) else numeros[-1]

        # El mes del periodo facturado; si el concepto no lo trae, el que
        # aparezca escrito en el detalle y, como ultimo recurso, el de emision.
        mes = mes_de_fecha(fila["desde"])
        notas = []
        if not mes and mes_documento:
            mes = mes_documento
            notas.append("MES tomado del texto del detalle")
        if not mes:
            mes = mes_de_fecha(cabecera.get("Fecha"))
            if mes:
                notas.append("MES tomado de la fecha de emision")
        if not cuadro and base is not None:
            notas.append("El detalle no cuadra con la base imponible")
        if base is None:
            notas.append("Sin totales para verificar el importe")

        # El IGV del documento se reparte entre conceptos segun su peso.
        con_igv = None
        if valor is not None and suma_base:
            con_igv = round(valor + igv_total * (valor / suma_base), 2)

        control.append({
            "Fecha": cabecera.get("Fecha"),
            "Tienda": tienda,
            "MES": mes,
            "Concepto": fila["concepto"],
            "SOLES": valor if moneda == "PEN" else None,
            "DOLARES": valor if moneda == "USD" else None,
            "# Factura": cabecera.get("# Factura"),
            "RAZON SOCIAL": cabecera.get("RAZON SOCIAL"),
            "FECHA DE ENTREGA": None,  # no viene en el PDF, se completa a mano
            "Contrato": cabecera.get("Contrato"),
            "Local": cabecera.get("Local / Tienda"),
            "Tipo Documento": cabecera.get("Tipo Documento"),
            "Periodo Desde": fila["desde"],
            "Periodo Hasta": fila["hasta"],
            "Importe con IGV": con_igv,
            "Referencia": cabecera.get("Referencia"),
            "RUC Emisor": cabecera.get("RUC Emisor"),
            "PDF File": nombre_archivo,
            "Pagina": cabecera.get("Pagina Inicial"),
            "Observaciones": "; ".join(notas) if notas else None,
        })

    return control, suma_base, cuadro


def leer_paginas(uploaded_file):
    paginas = []
    with pdfplumber.open(io.BytesIO(uploaded_file.getvalue())) as pdf:
        for numero, page in enumerate(pdf.pages, start=1):
            paginas.append({"numero": numero, "texto": page.extract_text() or ""})
    return paginas


def agrupar_documentos(paginas):
    """Una factura por cada pagina con titulo de comprobante; el resto son anexos."""
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

    return documentos


def process_pdf(uploaded_file):
    """Devuelve (control, facturas, auditoria) para un PDF."""
    paginas = leer_paginas(uploaded_file)
    documentos = agrupar_documentos(paginas)

    filas_control = []
    filas_factura = []
    filas_auditoria = []

    if not documentos:
        for pagina in paginas:
            filas_auditoria.append({
                "PDF File": uploaded_file.name,
                "Pagina": pagina["numero"],
                "Rol": "sin_documento",
                "# Factura": None,
                "Detalle": "No se encontro el titulo del comprobante electronico",
            })
        return filas_control, filas_factura, filas_auditoria

    for documento in documentos:
        # Los anexos (detalle de facturacion, estado de cuenta) no se leen como
        # detalle: repiten importes de otras facturas y ensuciarian el control.
        texto = "\n".join(pagina["texto"] for pagina in documento["paginas"])
        cabecera = extraer_cabecera(texto, documento["pagina_inicial"])

        # Los anexos no aportan detalle -repiten importes de otras facturas-,
        # pero si suelen traer el nombre del local mejor identificado.
        if documento["anexos"] and not (cabecera.get("Contrato") and cabecera.get("Local / Tienda")):
            texto_anexos = "\n".join(anexo["texto"] for anexo in documento["anexos"])
            if not cabecera.get("Local / Tienda"):
                cabecera["Local / Tienda"] = find_label_value(texto_anexos, [
                    "NOMBRE DE CONTRATO", "LOCAL COMERCIAL", "LOCAL",
                ], max_chars=45, requiere_separador=True)
            if not cabecera.get("Contrato"):
                cabecera["Contrato"] = find_label_value(texto_anexos, [
                    "NUM CONTRATO", "NUMERO DE CONTRATO", "N. CONTRATO", "CONTRATO",
                ], max_chars=30, requiere_separador=True)

        filas_detalle = extraer_lineas_detalle(texto)
        control, suma_base, cuadro = construir_filas_control(
            cabecera,
            filas_detalle,
            uploaded_file.name,
            mes_documento=buscar_mes_escrito("\n".join(zona_detalle(texto))),
        )
        filas_control.extend(control)

        factura = {clave: valor for clave, valor in cabecera.items() if not clave.startswith("_")}
        factura["PDF File"] = uploaded_file.name
        factura["Conceptos"] = len(filas_detalle)
        factura["Suma Conceptos"] = round(suma_base, 2) if filas_detalle else None
        factura["Cuadra Base"] = cuadra(cabecera.get("_base"), suma_base, tolerancia=0.10)
        factura["Observaciones"] = faltantes(factura, CAMPOS_OBLIGATORIOS)
        if not filas_detalle:
            factura["Observaciones"] = "; ".join(
                filtro for filtro in [factura["Observaciones"], "No se leyo ningun concepto"] if filtro
            )
        filas_factura.append(factura)

        filas_auditoria.append({
            "PDF File": uploaded_file.name,
            "Pagina": documento["pagina_inicial"],
            "Rol": "factura",
            "# Factura": cabecera.get("# Factura"),
            "Detalle": factura["Observaciones"],
        })
        for anexo in documento["anexos"]:
            filas_auditoria.append({
                "PDF File": uploaded_file.name,
                "Pagina": anexo["numero"],
                "Rol": "anexo",
                "# Factura": cabecera.get("# Factura"),
                "Detalle": "Anexo: no se toma como detalle",
            })

    return filas_control, filas_factura, filas_auditoria


def marcar_duplicados(facturas):
    """Senala el mismo comprobante leido mas de una vez, aunque cambie el archivo."""
    vistos = {}
    for factura in facturas:
        numero = factura.get("# Factura")
        if not numero:
            factura["Duplicado"] = None
            continue
        vistos[numero] = vistos.get(numero, 0) + 1

    for factura in facturas:
        numero = factura.get("# Factura")
        factura["Duplicado"] = "Si" if numero and vistos.get(numero, 0) > 1 else None


def build_summary_rows(control, facturas, archivos):
    duplicadas = sum(1 for f in facturas if f.get("Duplicado") == "Si")
    return [
        {"Metric": "Archivos procesados", "Value": archivos},
        {"Metric": "Facturas leidas", "Value": len(facturas)},
        {"Metric": "Filas de control", "Value": len(control)},
        {"Metric": "Total soles", "Value": round(sum(f["SOLES"] or 0 for f in control), 2)},
        {"Metric": "Total dolares", "Value": round(sum(f["DOLARES"] or 0 for f in control), 2)},
        {"Metric": "Facturas que no cuadran", "Value": sum(1 for f in facturas if f.get("Cuadra Base") == "No")},
        {"Metric": "Facturas sin conceptos", "Value": sum(1 for f in facturas if not f.get("Conceptos"))},
        {"Metric": "Facturas repetidas", "Value": duplicadas},
        {"Metric": "Facturas con campos sin leer", "Value": sum(1 for f in facturas if f.get("Observaciones"))},
    ]


def build_excel(files):
    control = []
    facturas = []
    auditoria = []

    for uploaded_file in files:
        try:
            filas_control, filas_factura, filas_auditoria = process_pdf(uploaded_file)
        except Exception as error:  # un PDF danado no debe tumbar el lote entero
            auditoria.append({
                "PDF File": uploaded_file.name,
                "Pagina": None,
                "Rol": "error",
                "# Factura": None,
                "Detalle": f"No se pudo leer: {error}",
            })
            continue

        control.extend(filas_control)
        facturas.extend(filas_factura)
        auditoria.extend(filas_auditoria)

    marcar_duplicados(facturas)
    resumen = build_summary_rows(control, facturas, len(files))

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(fechas_a_formato_local(control)).reindex(columns=CONTROL_COLUMNS).to_excel(
            writer, index=False, sheet_name="Control",
        )
        pd.DataFrame(fechas_a_formato_local(control)).reindex(columns=CONTROL_AMPLIADO_COLUMNS).to_excel(
            writer, index=False, sheet_name="Control ampliado",
        )
        pd.DataFrame(fechas_a_formato_local(facturas)).reindex(columns=FACTURA_COLUMNS).to_excel(
            writer, index=False, sheet_name="Facturas",
        )
        pd.DataFrame(resumen).reindex(columns=SUMMARY_COLUMNS).to_excel(
            writer, index=False, sheet_name="Resumen",
        )
        pd.DataFrame(fechas_a_formato_local(auditoria)).reindex(columns=AUDIT_COLUMNS).to_excel(
            writer, index=False, sheet_name="Auditoria",
        )

    output.seek(0)
    return output, control, facturas


def render_sidebar():
    """Ayuda propia de la pantalla de alquileres."""
    with st.sidebar:
        st.markdown('<div class="side-title">Que se extrae</div>', unsafe_allow_html=True)
        st.markdown(
            """
            <div class="side-card">
                UNA FILA POR CONCEPTO<br>
                CONTRATO Y LOCAL<br>
                SOLES Y DOLARES
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown('<div class="side-title">Operacion</div>', unsafe_allow_html=True)
        st.markdown(
            """
            <div class="side-note">
                Sube las facturas tal como llegan.<br>
                Los anexos y estados de cuenta se ignoran.
            </div>
            """,
            unsafe_allow_html=True,
        )
