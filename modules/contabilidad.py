"""Modulo Contabilidad: lectura de comprobantes de proveedor y salida a Excel.

Cubre factura, boleta, nota de credito, nota de debito y recibo por honorarios
en formato electronico peruano. La lectura es por etiquetas, no por posicion,
de modo que tolera que cada proveedor arme el PDF a su manera. Lo que no logra
identificar no se inventa: queda vacio y se reporta en la hoja Auditoria.

El mismo motor alimenta dos pantallas:

- **Pagos**: una fila por comprobante con la decision ya tomada de si va con
  detraccion, con retencion o con ninguna de las dos, para no revisarlo a mano
  factura por factura. La regla esta en forus_tributario.
- **Costos**: los parametros generales de cada comprobante y su detalle de
  items, sin la parte de pago.
"""
import io
import re

import pandas as pd
import pdfplumber
import streamlit as st

import forus_ocr
import forus_tributario as tributario
from forus_comprobante import (
    agrupar_documentos,
    deducir_totales_por_suma,
    detect_numero_factura,
    diagnostico_lectura,
    extraer_lineas_detalle,
    zona_detalle,
    detect_tipo_documento,
)

from forus_parsing import (
    collapse_spaces,
    cuadra,
    detect_currency_document,
    faltantes,
    find_date,
    find_label_value,
    find_money,
    find_percent,
    find_nombre_emisor,
    fix_mojibake,
    normalize,
    parse_amount,
    split_lines,
)
from forus_proveedores import razon_social_por_ruc
from forus_ui import (
    close_card,
    format_file_size,
    open_card,
    render_banner,
    render_benefits,
    render_empty,
    render_file_list,
    render_hero,
    render_pipeline,
    render_result_grid,
    render_rules,
    render_stat_grid,
)

DOCUMENT_COLUMNS = [
    "PDF File",
    "Pagina Inicial",
    "Paginas",
    "Tipo Documento",
    "Serie",
    "Correlativo",
    "Numero",
    "RUC Emisor",
    "Razon Social Emisor",
    "RUC Cliente",
    "Razon Social Cliente",
    "Fecha Emision",
    "Fecha Vencimiento",
    "Moneda",
    "Tipo de Cambio",
    "Op. Gravadas",
    "Op. Exoneradas",
    "Op. Inafectas",
    "Op. Gratuitas",
    "Descuentos",
    "ISC",
    "IGV",
    "Otros Cargos",
    "Importe Total",
    "Detraccion %",
    "Detraccion Monto",
    "Orden de Compra",
    "Forma de Pago",
    "Guia de Remision",
    "Items",
    "Cuadra Total",
    "Observaciones",
]

ITEM_COLUMNS = [
    "PDF File",
    "Numero",
    "Tipo Documento",
    "Fecha Emision",
    "RUC Emisor",
    "Pagina",
    "Linea",
    "Codigo",
    "Descripcion",
    "Cantidad",
    "UM",
    "Valor Unitario",
    "Precio Unitario",
    "Descuento",
    "Importe",
]

# Hoja Pagos: las columnas del control de Contabilidad, en su orden, para
# pegarlas directo. "D" es detraccion y "R" retencion.
PAGOS_COLUMNS = [
    "FECHA",
    "RUC",
    "PROVEEDOR",
    "DOCUMENTO",
    "GLOSA",
    "IMPORTE",
    "D/R",
    "%",
    "DETRACCION/RETENCION",
    "ANTICIPOS",
    "MONTO A PAGAR",
]

# Las mismas filas con todo lo que se leyo, para revisar de donde sale cada cosa.
PAGOS_AMPLIADO_COLUMNS = PAGOS_COLUMNS + [
    "MONEDA",
    "Tipo Documento",
    "Fecha Vencimiento",
    "Tipo de Cambio",
    "Importe en Soles",
    "Afecto a",
    "Motivo",
    "Codigo Detraccion",
    "Concepto Detraccion",
    "Origen Tasa",
    "Origen Texto",
    "Op. Gravadas",
    "IGV",
    "Orden de Compra",
    "Forma de Pago",
    "PDF File",
    "Observaciones",
]

SUMMARY_COLUMNS = ["Metric", "Value"]

AUDIT_COLUMNS = ["PDF File", "Pagina", "Rol", "Numero", "Detalle"]

CAMPOS_OBLIGATORIOS = [
    "Numero",
    "RUC Emisor",
    "Fecha Emision",
    "Importe Total",
]

ALIAS_COLUMNAS = [
    ("Codigo", ["CODIGO INTERNO", "COD. PRODUCTO", "CODIGO", "COD.", "COD", "ITEM"]),
    ("Descripcion", ["DESCRIPCION", "DETALLE", "CONCEPTO", "PRODUCTO", "BIEN O SERVICIO"]),
    ("Cantidad", ["CANTIDAD", "CANT.", "CANT", "CTD"]),
    ("UM", ["UNIDAD DE MEDIDA", "UNIDAD", "U.M.", "UM", "MEDIDA"]),
    ("Valor Unitario", ["VALOR UNITARIO", "VALOR UNIT.", "V. UNITARIO", "VALOR UNIT"]),
    ("Precio Unitario", ["PRECIO UNITARIO", "PRECIO UNIT.", "P. UNITARIO", "PRECIO UNIT", "PRECIO"]),
    ("Descuento", ["DESCUENTO", "DSCTO.", "DSCTO"]),
    ("Importe", ["IMPORTE TOTAL", "VALOR DE VENTA", "VALOR VENTA", "IMPORTE", "SUBTOTAL", "TOTAL"]),
]

UNIDADES = {
    "NIU", "ZZ", "UND", "UNI", "UNIDAD", "KGM", "KG", "GRM", "LTR", "MTR",
    "CJA", "BOL", "PAR", "SET", "DOC", "GLL", "M2", "M3", "SER", "PZA",
}


def get_ruc_empresa():
    """RUC de la empresa, opcional, para distinguir al emisor del cliente."""
    try:
        empresa = dict(st.secrets.get("empresa", {}))
    except Exception:
        empresa = {}
    ruc = str(empresa.get("ruc", "")).strip()
    return ruc if re.fullmatch(r"\d{11}", ruc) else None


def extraer_rucs(text):
    """Devuelve (ruc_emisor, ruc_cliente) mirando todos los RUC del documento."""
    encontrados = []
    for match in re.finditer(r"\b((?:10|15|16|17|20)\d{9})\b", text or ""):
        ruc = match.group(1)
        if ruc not in encontrados:
            encontrados.append(ruc)

    if not encontrados:
        return None, None

    ruc_empresa = get_ruc_empresa()
    if ruc_empresa and ruc_empresa in encontrados:
        emisor = next((ruc for ruc in encontrados if ruc != ruc_empresa), None)
        return emisor, ruc_empresa

    emisor = encontrados[0]
    cliente = encontrados[1] if len(encontrados) > 1 else None
    return emisor, cliente


def extraer_cabecera(text, pagina_inicial, paginas):
    numero = detect_numero_factura(text)
    serie, _, correlativo = (numero or "").partition("-")
    tipo = detect_tipo_documento(text)

    ruc_emisor, ruc_cliente = extraer_rucs(text)

    moneda = detect_currency_document(text)

    gravadas = find_money(text, [
        "OP. GRAVADAS", "OP GRAVADAS", "OPERACIONES GRAVADAS", "OP. GRAVADA",
        "TOTAL OPERACIONES GRAVADAS", "SUB TOTAL VENTAS", "VALOR DE VENTA",
        "SUBTOTAL", "SUB TOTAL",
    ])
    igv = find_money(text, [
        "IGV (18%)", "I.G.V. (18%)", "IGV 18%", "I.G.V. 18%", "IGV(18%)",
        "IMPUESTO GENERAL A LAS VENTAS", "I.G.V.", "IGV",
    ])
    total = find_money(text, [
        "IMPORTE TOTAL", "TOTAL A PAGAR", "PRECIO VENTA", "TOTAL VENTA",
        "TOTAL COMPROBANTE", "TOTAL",
    ])
    if total is None:
        total = find_money(text, ["TOTAL"])

    # Algunos proveedores imprimen los totales sin repetir las etiquetas.
    if gravadas is None or total is None:
        base, igv_deducido, total_deducido = deducir_totales_por_suma(text)
        if base is not None:
            gravadas = gravadas if gravadas is not None else base
            igv = igv if igv is not None else igv_deducido
            total = total if total is not None else total_deducido

    fila = {
        "Pagina Inicial": pagina_inicial,
        "Paginas": paginas,
        "Tipo Documento": tipo,
        "Serie": serie or None,
        "Correlativo": correlativo or None,
        "Numero": numero,
        "RUC Emisor": ruc_emisor,
        "Razon Social Emisor": (
            razon_social_por_ruc(ruc_emisor) or find_nombre_emisor(text, excluir=("FORUS",))
        ),
        "RUC Cliente": ruc_cliente,
        "Razon Social Cliente": find_label_value(text, [
            "SENOR(ES)", "SENORES", "SENOR", "CLIENTE", "ADQUIRIENTE",
            "RAZON SOCIAL", "DENOMINACION",
        ]),
        "Fecha Emision": find_date(text, [
            "FECHA DE EMISION", "FECHA EMISION", "F. EMISION", "FECHA",
        ]),
        "Fecha Vencimiento": find_date(text, [
            "FECHA DE VENCIMIENTO", "FECHA VENCIMIENTO", "F. VENCIMIENTO",
        ]),
        "Moneda": moneda,
        "Tipo de Cambio": find_money(text, ["TIPO DE CAMBIO", "T.C.", "TIPO CAMBIO"]),
        "Op. Gravadas": gravadas,
        "Op. Exoneradas": find_money(text, [
            "OP. EXONERADAS", "OP EXONERADAS", "OPERACIONES EXONERADAS", "OP. EXONERADA",
        ]),
        "Op. Inafectas": find_money(text, [
            "OP. INAFECTAS", "OP INAFECTAS", "OPERACIONES INAFECTAS", "OP. INAFECTA",
        ]),
        "Op. Gratuitas": find_money(text, [
            "OP. GRATUITAS", "OP GRATUITAS", "OPERACIONES GRATUITAS",
        ]),
        "Descuentos": find_money(text, [
            "TOTAL DESCUENTOS", "DESCUENTO GLOBAL", "DESCUENTOS",
        ]),
        "ISC": find_money(text, ["I.S.C.", "ISC"]),
        "IGV": igv,
        "Otros Cargos": find_money(text, ["OTROS CARGOS", "OTROS TRIBUTOS"]),
        "Importe Total": total,
        "Detraccion %": find_percent(text, [
            "PORCENTAJE DE DETRACCION", "DETRACCION", "OPERACION SUJETA A DETRACCION",
        ]),
        "Retencion Monto": find_money(text, [
            "TOTAL RETENCION", "MONTO DE RETENCION", "RETENCION IGV", "RETENCION",
        ]),
        "Detraccion Monto": find_money(text, [
            "MONTO DE DETRACCION", "MONTO DETRACCION", "TOTAL DETRACCION",
        ]),
        "Orden de Compra": find_label_value(text, [
            "ORDEN DE COMPRA", "ORDEN COMPRA", "O/C", "NRO. ORDEN", "NUMERO DE ORDEN",
        ], max_chars=40),
        "Forma de Pago": find_label_value(text, [
            "FORMA DE PAGO", "CONDICION DE PAGO", "CONDICIONES DE PAGO", "TIPO DE PAGO",
        ], max_chars=40),
        "Guia de Remision": find_label_value(text, [
            "GUIA DE REMISION", "GUIA REMISION",
        ], max_chars=40),
    }

    esperado = None
    if gravadas is not None or igv is not None:
        esperado = (gravadas or 0) + (igv or 0)
        for extra in ("Op. Exoneradas", "Op. Inafectas", "ISC", "Otros Cargos"):
            esperado += fila.get(extra) or 0
    fila["Cuadra Total"] = cuadra(esperado, total, tolerancia=0.10)

    return fila


# Restos del pie de la factura que las tablas arrastran como si fueran items.
RUIDO_DESCRIPCION = (
    "SON ", "SON:", "TOTAL", "SUB TOTAL", "SUBTOTAL", "OP.", "IGV", "I.G.V.",
    "DETRACCION", "DETRACCI", "SPOT", "OBLIGACIONES TRIBUTARIAS", "CUENTA",
    "BANCO", "SUNAT", "RESOLUCION", "AUTORIZADO", "REPRESENTACION", "PLAZO DE PAGO",
    "AGENTE", "AGENTES", "DOCUMENTOS REFERENCIADOS", "CODIGO DETRACC",
    "PORCENTAJE", "LEYENDA", "INFORMACION",
)


def descripcion_util(descripcion):
    """Descarta lo que no es un concepto sino texto del pie del comprobante."""
    if not descripcion:
        return False

    plano = normalize(descripcion)
    if any(ruido in plano for ruido in RUIDO_DESCRIPCION):
        return False
    # El importe en letras ("...CON 34/100 DOLARES") llega partido en celdas y
    # pierde el "SON:" del principio, pero conserva siempre los centimos.
    if re.search(r"\d{1,2}\s*/\s*100", descripcion):
        return False
    return len(re.findall(r"[A-Za-z]", descripcion)) >= 4


def mapear_columnas(encabezado):
    """Empareja las celdas del encabezado de una tabla con nuestros campos."""
    mapa = {}
    usados = set()

    for indice, celda in enumerate(encabezado):
        plano = normalize(collapse_spaces(celda) or "")
        if not plano:
            continue
        for campo, alias in ALIAS_COLUMNAS:
            if campo in usados:
                continue
            if any(nombre in plano for nombre in alias):
                mapa[campo] = indice
                usados.add(campo)
                break

    return mapa


def items_desde_tablas(tables, pagina):
    """Lee los items de las tablas cuyo encabezado reconocemos."""
    items = []

    for table in tables or []:
        if not table or len(table) < 2:
            continue

        mapa = mapear_columnas(table[0])
        if "Descripcion" not in mapa or not ({"Cantidad", "Importe"} & set(mapa)):
            continue

        for fila_tabla in table[1:]:
            if not fila_tabla:
                continue

            def valor(campo):
                indice = mapa.get(campo)
                if indice is None or indice >= len(fila_tabla):
                    return None
                return collapse_spaces(fila_tabla[indice])

            descripcion = valor("Descripcion")
            importe = parse_amount(valor("Importe"))
            cantidad = parse_amount(valor("Cantidad"))

            if not descripcion and importe is None:
                continue
            if not descripcion_util(descripcion):
                continue

            items.append({
                "Pagina": pagina,
                "Codigo": valor("Codigo"),
                "Descripcion": descripcion,
                "Cantidad": cantidad,
                "UM": valor("UM"),
                "Valor Unitario": parse_amount(valor("Valor Unitario")),
                "Precio Unitario": parse_amount(valor("Precio Unitario")),
                "Descuento": parse_amount(valor("Descuento")),
                "Importe": importe,
            })

    return items


LINEA_ITEM_RE = re.compile(
    r"^(?P<cantidad>\d[\d.,]*)\s+(?P<um>[A-Z0-9]{1,10})\s+(?P<resto>.+?)\s+"
    r"(?P<unitario>-?[\d.,]+)\s+(?P<importe>-?[\d.,]+)$"
)


def items_desde_texto(text, pagina):
    """Respaldo para PDFs sin tablas: lineas 'cantidad UM descripcion unit importe'."""
    items = []

    for linea in zona_detalle(text):
        plano = normalize(linea)
        if plano.startswith(("SON ", "TOTAL", "SUB TOTAL", "OP.", "IGV", "IMPORTE TOTAL")):
            continue

        match = LINEA_ITEM_RE.match(collapse_spaces(linea) or "")
        if not match:
            continue
        if normalize(match.group("um")) not in UNIDADES:
            continue

        items.append({
            "Pagina": pagina,
            "Codigo": None,
            "Descripcion": collapse_spaces(match.group("resto")),
            "Cantidad": parse_amount(match.group("cantidad")),
            "UM": match.group("um"),
            "Valor Unitario": parse_amount(match.group("unitario")),
            "Precio Unitario": None,
            "Descuento": None,
            "Importe": parse_amount(match.group("importe")),
        })

    return items


def leer_paginas(uploaded_file):
    """Texto y tablas de cada pagina, con la codificacion ya corregida.

    Las paginas escaneadas no traen texto: esas se leen por OCR.
    """
    datos = uploaded_file.getvalue()
    paginas = []
    with pdfplumber.open(io.BytesIO(datos)) as pdf:
        for numero, page in enumerate(pdf.pages, start=1):
            paginas.append({
                "numero": numero,
                "texto": fix_mojibake(page.extract_text() or ""),
                "tablas": page.extract_tables() or [],
            })

    forus_ocr.completar_paginas_vacias(datos, paginas)
    return paginas


def construir_glosa(items, maximo=110):
    """Concepto del comprobante: las descripciones de sus lineas, sin repetir."""
    vistas = []
    for item in items:
        descripcion = collapse_spaces(item.get("Descripcion"))
        if descripcion and descripcion not in vistas:
            vistas.append(descripcion)

    if not vistas:
        return None

    glosa = " / ".join(vistas)
    return glosa if len(glosa) <= maximo else glosa[:maximo - 3].rstrip() + "..."


def construir_fila_pago(cabecera):
    """Traduce el comprobante a las columnas del control de Contabilidad.

    Una nota de credito entra en negativo: en un control de pagos lo que hace
    es restar de lo que se le debe al proveedor.
    """
    importe = cabecera.get("Importe Total")
    monto = cabecera.get("Monto Detraccion/Retencion")
    afecto = cabecera.get("Afecto a")

    if importe is not None and cabecera.get("Tipo Documento") == "Nota de Credito":
        importe = -abs(importe)

    marca = {tributario.DETRACCION: "D", tributario.RETENCION: "R"}.get(afecto)

    a_pagar = None
    if importe is not None:
        a_pagar = round(importe - (monto or 0), 2)

    return {
        "FECHA": cabecera.get("Fecha Emision"),
        "RUC": cabecera.get("RUC Emisor"),
        "PROVEEDOR": cabecera.get("Razon Social Emisor"),
        "DOCUMENTO": cabecera.get("Numero"),
        "GLOSA": cabecera.get("Glosa"),
        "IMPORTE": importe,
        "D/R": marca,
        "%": cabecera.get("% Aplicado"),
        "DETRACCION/RETENCION": monto,
        "ANTICIPOS": None,  # no viene en el PDF, se completa a mano
        "MONTO A PAGAR": a_pagar,
        "MONEDA": cabecera.get("Moneda"),
    }


def process_pdf(uploaded_file, parametros=None):
    """Devuelve (documentos, items, auditoria) para un PDF."""
    parametros = parametros or tributario.get_parametros()
    paginas = leer_paginas(uploaded_file)
    documentos_agrupados = agrupar_documentos(paginas)

    filas_documento = []
    filas_item = []
    filas_auditoria = []

    # Ningun archivo puede desaparecer sin dejar constancia de por que.
    if not documentos_agrupados:
        filas_auditoria.append({
            "PDF File": uploaded_file.name,
            "Pagina": None,
            "Rol": "no_leido",
            "Numero": None,
            "Detalle": diagnostico_lectura(paginas) or "No se reconocio ningun comprobante",
        })
        return filas_documento, filas_item, filas_auditoria

    aviso = diagnostico_lectura(paginas)

    for documento in documentos_agrupados:
        texto_completo = "\n".join(pagina["texto"] for pagina in documento["paginas"])
        cabecera = extraer_cabecera(
            texto_completo,
            documento["pagina_inicial"],
            len(documento["paginas"]),
        )
        cabecera["PDF File"] = uploaded_file.name

        items = []
        for pagina in documento["paginas"]:
            encontrados = items_desde_tablas(pagina["tablas"], pagina["numero"])
            if not encontrados:
                encontrados = items_desde_texto(pagina["texto"], pagina["numero"])
            if not encontrados:
                # Ultimo recurso: el lector de lineas acotado a la zona de
                # detalle, que aguanta los formatos de tabla mas irregulares.
                encontrados = [
                    {
                        "Pagina": pagina["numero"],
                        "Codigo": fila["codigo"],
                        "Descripcion": fila["concepto"],
                        "Cantidad": fila["cantidad"],
                        "UM": fila["unidad"],
                        "Valor Unitario": None,
                        "Precio Unitario": None,
                        "Descuento": None,
                        "Importe": fila["numeros"][-1] if fila["numeros"] else None,
                    }
                    for fila in extraer_lineas_detalle(pagina["texto"])
                    if descripcion_util(fila["concepto"])
                ]
            items.extend(encontrados)

        for orden, item in enumerate(items, start=1):
            item.update({
                "PDF File": uploaded_file.name,
                "Numero": cabecera["Numero"],
                "Tipo Documento": cabecera["Tipo Documento"],
                "Fecha Emision": cabecera["Fecha Emision"],
                "RUC Emisor": cabecera["RUC Emisor"],
                "Linea": orden,
            })

        cabecera["Items"] = len(items)
        cabecera["Glosa"] = construir_glosa(items)

        # Detraccion o retencion: se decide aqui, con el texto completo del
        # comprobante delante, y viaja ya resuelto hasta el Excel de Pagos.
        cabecera.update(tributario.evaluar(
            cabecera.get("Importe Total"),
            cabecera.get("Moneda"),
            cabecera.get("Tipo de Cambio"),
            texto_completo,
            monto_detraccion=cabecera.get("Detraccion Monto"),
            parametros=parametros,
            igv=cabecera.get("IGV"),
            tipo_documento=cabecera.get("Tipo Documento"),
            glosa=cabecera.get("Glosa"),
            monto_retencion=cabecera.get("Retencion Monto"),
        ))
        cabecera.update(construir_fila_pago(cabecera))

        leido_por_ocr = any(pagina.get("ocr") for pagina in documento["paginas"])
        if leido_por_ocr:
            cabecera["Origen Texto"] = "OCR"
        cabecera["Observaciones"] = "; ".join(
            parte for parte in [
                faltantes(cabecera, CAMPOS_OBLIGATORIOS),
                aviso,
                "Leido por OCR de un escaneo: conviene revisar las cifras" if leido_por_ocr else None,
            ] if parte
        ) or None

        filas_documento.append(cabecera)
        filas_item.extend(items)

        for indice, pagina in enumerate(documento["paginas"]):
            filas_auditoria.append({
                "PDF File": uploaded_file.name,
                "Pagina": pagina["numero"],
                "Rol": "inicio_documento" if indice == 0 else "continuacion",
                "Numero": cabecera["Numero"],
                "Detalle": cabecera["Observaciones"] and f"Sin leer: {cabecera['Observaciones']}",
            })

        # Los anexos que acompanan a la factura -detalle de facturacion, estado
        # de cuenta- no son comprobantes: se dejan anotados y no se leen.
        for anexo in documento["anexos"]:
            filas_auditoria.append({
                "PDF File": uploaded_file.name,
                "Pagina": anexo["numero"],
                "Rol": "anexo",
                "Numero": cabecera["Numero"],
                "Detalle": "Anexo: no se toma como comprobante",
            })

    return filas_documento, filas_item, filas_auditoria


def build_summary_rows(documentos, items, auditoria, archivos):
    por_moneda = {}
    for documento in documentos:
        moneda = documento.get("Moneda") or "Sin moneda"
        por_moneda[moneda] = por_moneda.get(moneda, 0) + (documento.get("Importe Total") or 0)

    filas = [
        {"Metric": "Archivos procesados", "Value": archivos},
        {"Metric": "Paginas leidas", "Value": len(auditoria)},
        {"Metric": "Comprobantes", "Value": len(documentos)},
        {"Metric": "Lineas de detalle", "Value": len(items)},
        {"Metric": "Total IGV", "Value": round(sum(d.get("IGV") or 0 for d in documentos), 2)},
    ]

    for moneda, total in sorted(por_moneda.items()):
        filas.append({"Metric": f"Importe total {moneda}", "Value": round(total, 2)})

    filas.append({
        "Metric": "Comprobantes que no cuadran",
        "Value": sum(1 for d in documentos if d.get("Cuadra Total") == "No"),
    })
    filas.append({
        "Metric": "Comprobantes con campos sin leer",
        "Value": sum(1 for d in documentos if d.get("Observaciones")),
    })

    return filas


def build_summary_pagos(documentos, archivos):
    def suma(clave, afecto=None):
        return round(sum(
            d.get(clave) or 0 for d in documentos
            if afecto is None or d.get("Afecto a") == afecto
        ), 2)

    return [
        {"Metric": "Archivos procesados", "Value": archivos},
        {"Metric": "Comprobantes", "Value": len(documentos)},
        {"Metric": "Con detraccion", "Value": sum(1 for d in documentos if d.get("Afecto a") == tributario.DETRACCION)},
        {"Metric": "Con retencion", "Value": sum(1 for d in documentos if d.get("Afecto a") == tributario.RETENCION)},
        {"Metric": "Sin detraccion ni retencion", "Value": sum(1 for d in documentos if d.get("Afecto a") == tributario.NO_AFECTO)},
        {"Metric": "Por revisar a mano", "Value": sum(1 for d in documentos if d.get("Afecto a") == tributario.REVISAR)},
        {"Metric": "Monto total detraido", "Value": suma("Monto Detraccion/Retencion", tributario.DETRACCION)},
        {"Metric": "Monto total retenido", "Value": suma("Monto Detraccion/Retencion", tributario.RETENCION)},
        {"Metric": "Neto a pagar", "Value": suma("Neto a Pagar")},
        {"Metric": "Comprobantes con campos sin leer", "Value": sum(1 for d in documentos if d.get("Observaciones"))},
        {"Metric": "Archivos sin ningun comprobante", "Value": archivos - len({d.get("PDF File") for d in documentos})},
    ]


def leer_lote(files):
    """Procesa todos los PDFs una sola vez. Las dos pantallas parten de aqui."""
    parametros = tributario.get_parametros()
    documentos = []
    items = []
    auditoria = []

    for uploaded_file in files:
        try:
            filas_documento, filas_item, filas_auditoria = process_pdf(uploaded_file, parametros)
        except Exception as error:  # un PDF danado no debe tumbar el lote entero
            auditoria.append({
                "PDF File": uploaded_file.name,
                "Pagina": None,
                "Rol": "error",
                "Numero": None,
                "Detalle": f"No se pudo leer: {error}",
            })
            continue

        documentos.extend(filas_documento)
        items.extend(filas_item)
        auditoria.extend(filas_auditoria)

    return documentos, items, auditoria


def build_excel_pagos(files):
    """Excel orientado al pago: que se detrae, que se retiene y cuanto se paga."""
    documentos, _, auditoria = leer_lote(files)
    resumen = build_summary_pagos(documentos, len(files))

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(documentos).reindex(columns=PAGOS_COLUMNS).to_excel(
            writer, index=False, sheet_name="Pagos",
        )
        pd.DataFrame(documentos).reindex(columns=PAGOS_AMPLIADO_COLUMNS).to_excel(
            writer, index=False, sheet_name="Pagos ampliado",
        )
        pd.DataFrame(resumen).reindex(columns=SUMMARY_COLUMNS).to_excel(
            writer, index=False, sheet_name="Resumen",
        )
        pd.DataFrame(auditoria).reindex(columns=AUDIT_COLUMNS).to_excel(
            writer, index=False, sheet_name="Auditoria",
        )

    output.seek(0)
    return output, documentos


def build_excel_costos(files):
    """Excel con los parametros generales de cada comprobante y su detalle."""
    documentos, items, auditoria = leer_lote(files)
    resumen = build_summary_rows(documentos, items, auditoria, len(files))

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(documentos).reindex(columns=DOCUMENT_COLUMNS).to_excel(
            writer, index=False, sheet_name="Documentos",
        )
        pd.DataFrame(items).reindex(columns=ITEM_COLUMNS).to_excel(
            writer, index=False, sheet_name="Detalle",
        )
        pd.DataFrame(resumen).reindex(columns=SUMMARY_COLUMNS).to_excel(
            writer, index=False, sheet_name="Resumen",
        )
        pd.DataFrame(auditoria).reindex(columns=AUDIT_COLUMNS).to_excel(
            writer, index=False, sheet_name="Auditoria",
        )

    output.seek(0)
    return output, documentos, items


def render_sidebar_pagos():
    """Panel lateral de Contabilidad - Pagos."""
    parametros = tributario.get_parametros()
    with st.sidebar:
        st.markdown('<div class="side-title">Regla aplicada</div>', unsafe_allow_html=True)
        st.markdown(
            f"""
            <div class="side-card">
                UMBRAL S/ {parametros['umbral']:,.0f}<br>
                RETENCION {parametros['tasa_retencion']:g}%<br>
                DETRACCION SEGUN EL PDF
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown('<div class="side-title">Operacion</div>', unsafe_allow_html=True)
        st.markdown(
            """
            <div class="side-note">
                Cada factura sale ya marcada como<br>
                <b>DETRACCION</b>, <b>RETENCION</b> o <b>NO AFECTO</b>,
                con el motivo al lado.
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_sidebar_costos():
    """Panel lateral de Contabilidad - Costos."""
    with st.sidebar:
        st.markdown('<div class="side-title">Documentos aceptados</div>', unsafe_allow_html=True)
        st.markdown(
            """
            <div class="side-card">
                FACTURA<br>
                BOLETA DE VENTA<br>
                NOTA DE CREDITO<br>
                NOTA DE DEBITO<br>
                RECIBO POR HONORARIOS
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown('<div class="side-title">Operacion</div>', unsafe_allow_html=True)
        st.markdown(
            """
            <div class="side-note">
                No hace falta renombrar los archivos.<br>
                Un PDF puede traer varios comprobantes.
            </div>
            """,
            unsafe_allow_html=True,
        )


ICONO_PAGOS = """
<svg viewBox="0 0 96 96" width="72" height="72" aria-label="Pagos">
    <rect x="12" y="26" width="72" height="46" rx="8" fill="#ffffff" stroke="#bdd4f7"/>
    <rect x="12" y="36" width="72" height="9" fill="#10916d"/>
    <circle cx="66" cy="59" r="10" fill="#d7f2e7" stroke="#10916d"/>
    <path d="M63 59h6M66 55v8" stroke="#0a5c47" stroke-width="2.4" stroke-linecap="round"/>
    <rect x="22" y="55" width="22" height="4" rx="2" fill="#cfe6dd"/>
    <rect x="22" y="63" width="14" height="4" rx="2" fill="#cfe6dd"/>
</svg>
"""

ICONO_COSTOS = """
<svg viewBox="0 0 96 96" width="72" height="72" aria-label="Costos">
    <rect x="20" y="10" width="56" height="76" rx="8" fill="#ffffff" stroke="#bdd4f7"/>
    <rect x="28" y="20" width="40" height="14" rx="4" fill="#d7f2e7"/>
    <rect x="28" y="42" width="16" height="12" rx="3" fill="#10916d"/>
    <rect x="50" y="42" width="18" height="12" rx="3" fill="#cfe6dd"/>
    <rect x="28" y="60" width="16" height="12" rx="3" fill="#cfe6dd"/>
    <rect x="50" y="60" width="18" height="12" rx="3" fill="#10916d"/>
</svg>
"""

# Se muestra la hoja Pagos tal cual saldra en el Excel.
COLUMNAS_PAGOS_PREVIA = PAGOS_COLUMNS

COLUMNAS_COSTOS_PREVIA = [
    "Numero", "Tipo Documento", "Fecha Emision", "RUC Emisor",
    "Razon Social Emisor", "Moneda", "Op. Gravadas", "IGV", "Importe Total",
    "Cuadra Total", "Observaciones",
]


def avisar_no_leidos(archivos, documentos):
    """Avisa en pantalla de los PDFs que no dieron ningun comprobante."""
    leidos = {documento.get("PDF File") for documento in documentos}
    no_leidos = [archivo.name for archivo in archivos if archivo.name not in leidos]
    if not no_leidos:
        return

    disponible, motivo = forus_ocr.estado_ocr()
    if not disponible:
        aviso_ocr = f" El OCR no esta activo: {motivo}."
    elif forus_ocr.ultimo_error():
        aviso_ocr = f" El OCR fallo: {forus_ocr.ultimo_error()}."
    else:
        aviso_ocr = ""

    render_banner(
        f"{len(no_leidos)} de {len(archivos)} archivo(s) no dieron ningun comprobante. "
        "El motivo de cada uno esta en la hoja Auditoria del Excel. La causa mas "
        f"frecuente es que el PDF venga escaneado, sin texto que leer.{aviso_ocr}",
        tipo="warn",
    )
    with st.expander(f"Ver los {len(no_leidos)} archivos que no se leyeron"):
        for nombre in no_leidos:
            st.write(f"- {nombre}")


def _cargar_comprobantes(clave, titulo):
    """Bloques 1 y 2 de la pantalla: cargar archivos y listarlos."""
    st.markdown(f'<div class="work-card upload-wrap acct"><h3>{titulo}</h3></div>', unsafe_allow_html=True)
    uploaded_files = st.file_uploader(
        "Subir PDFs de comprobantes",
        type=["pdf"],
        accept_multiple_files=True,
        label_visibility="collapsed",
        key=f"{clave}_uploader",
    )

    open_card("2. Archivos cargados", kicker="Control de entrada")

    if uploaded_files:
        render_stat_grid([
            ("PDFs cargados", len(uploaded_files)),
            ("Tamano total", format_file_size(
                sum(getattr(archivo, "size", 0) or 0 for archivo in uploaded_files)
            )),
            ("Estado", "Listos"),
        ])
        render_file_list(
            [
                (
                    archivo.name,
                    f"{format_file_size(getattr(archivo, 'size', None))} - Comprobante",
                    "PDF",
                    "Listo",
                )
                for archivo in uploaded_files
            ],
            variante="acct",
        )
    else:
        render_empty("Carga los comprobantes de tus proveedores para comenzar.")

    close_card()
    return uploaded_files


def render_pagos():
    """Pantalla de Contabilidad - Pagos."""
    parametros = tributario.get_parametros()
    render_hero(
        "CONTABILIDAD - PAGOS",
        'Comprobantes <span style="color:#8ff0cf">&rsaquo;</span> Detraccion o retencion resuelta',
        "Sube las facturas de tus proveedores y cada una sale marcada como detraccion, retencion o ninguna, con el monto y el neto a pagar ya calculados.",
        tags=[("Sin revisar a mano", "green")],
        variante="acct",
        icono_svg=ICONO_PAGOS,
    )

    render_pipeline([
        ("Input", "Comprobantes PDF", "active", "Pend."),
        ("Lectura", "Importes y proveedor", "ok", "OK"),
        ("Regla", f"Umbral S/ {parametros['umbral']:,.0f}", "warn", "Auto"),
        ("Salida", "Excel de pagos", "", "Pend."),
    ])

    render_rules(
        "Como se decide cada factura",
        "La regla se aplica sola, en este orden, y el motivo queda escrito al lado de cada fila.",
        [
            ("1. Detraccion", "Si el comprobante la declara, manda su porcentaje"),
            ("2. Umbral", f"Hasta S/ {parametros['umbral']:,.0f} no corresponde retencion"),
            ("3. Retencion", f"Sobre el umbral, {parametros['tasa_retencion']:g}% salvo proveedor excluido"),
        ],
        variante="acct",
    )

    with st.expander("Tabla de tasas de detraccion por codigo"):
        st.caption(
            "Esta tabla solo se usa cuando el comprobante no imprime el porcentaje. "
            "Si lo imprime, manda siempre el del documento. Las tasas las cambia "
            "SUNAT por resolucion: las marcadas como 'Por validar' hay que "
            "contrastarlas con la tabla oficial antes de confiar en ellas, y se "
            "corrigen desde los secrets en la seccion [detraccion_tasas], sin "
            "tocar el codigo."
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Codigo": codigo,
                        "Bien o servicio": nombre,
                        "Tasa %": tasa,
                        "Estado": (
                            "Contrastada con factura"
                            if codigo in tributario.CODIGOS_VERIFICADOS
                            else "Por validar con Contabilidad"
                        ),
                    }
                    for codigo, (nombre, tasa) in sorted(tributario.get_tasas().items())
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )

    uploaded_files = _cargar_comprobantes("pagos", "1. Cargar comprobantes")

    open_card(
        "3. Procesar y generar Excel",
        kicker="Salida final",
        texto="Genera el Excel con las hojas Pagos, Resumen y Auditoria.",
    )

    if st.button(
        "Procesar comprobantes",
        type="primary",
        disabled=not uploaded_files,
        key="pagos_procesar",
    ):
        with st.spinner("Leyendo comprobantes y aplicando la regla..."):
            excel_bytes, documentos = build_excel_pagos(uploaded_files)

        con_detraccion = sum(1 for d in documentos if d.get("Afecto a") == tributario.DETRACCION)
        con_retencion = sum(1 for d in documentos if d.get("Afecto a") == tributario.RETENCION)
        sin_afectar = sum(1 for d in documentos if d.get("Afecto a") == tributario.NO_AFECTO)
        por_revisar = sum(1 for d in documentos if d.get("Afecto a") == tributario.REVISAR)

        if documentos:
            render_banner("Excel generado correctamente. Ya puedes descargar el detalle de pagos.")
        else:
            render_banner(
                "No se reconocio ningun comprobante. Revisa la hoja Auditoria del Excel.",
                tipo="warn",
            )

        render_stat_grid([
            ("Con detraccion", con_detraccion),
            ("Con retencion", con_retencion),
            ("Sin afectar", sin_afectar),
        ])

        avisar_no_leidos(uploaded_files, documentos)

        if por_revisar:
            render_banner(
                f"{por_revisar} comprobante(s) no se pudieron decidir solos y salen como "
                "REVISAR, con el motivo escrito en su fila.",
                tipo="warn",
            )

        if documentos:
            st.dataframe(
                pd.DataFrame(documentos).reindex(columns=COLUMNAS_PAGOS_PREVIA).head(80),
                use_container_width=True,
                hide_index=True,
            )

        st.download_button(
            "Descargar Excel",
            data=excel_bytes,
            file_name="contabilidad_pagos.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="pagos_descargar",
        )

    close_card()

    render_benefits([
        ("La regla, aplicada sola", "Detraccion, retencion o ninguna, decidido por el monto y por lo que declara el PDF."),
        ("Con el motivo al lado", "Cada fila dice por que le toco lo que le toco, para poder revisarla en un vistazo."),
        ("Nada inventado", "Si falta un dato para decidir, la fila sale como REVISAR en vez de arriesgar un numero."),
    ])



def render_costos():
    """Pantalla de Contabilidad - Costos."""
    render_hero(
        "CONTABILIDAD - COSTOS",
        'Comprobantes <span style="color:#8ff0cf">&rsaquo;</span> Parametros generales',
        "Sube facturas, boletas, notas y recibos por honorarios y baja los parametros generales de cada comprobante con su detalle de items.",
        tags=[("Lectura por etiquetas", "green")],
        variante="acct",
        icono_svg=ICONO_COSTOS,
    )

    render_pipeline([
        ("Input", "Comprobantes PDF", "active", "Pend."),
        ("Lectura", "Etiquetas SUNAT", "ok", "OK"),
        ("Validacion", "Gravadas + IGV vs Total", "warn", "Revisar"),
        ("Salida", "Excel de costos", "", "Pend."),
    ])

    render_rules(
        "Que parametros se bajan",
        "El tipo de documento se identifica por su titulo y su serie, y cada comprobante se separa aunque vengan varios en el mismo PDF.",
        [
            ("Cabecera", "RUC, razon social, fechas, moneda y tipo de cambio"),
            ("Importes", "Gravadas, exoneradas, inafectas, IGV, ISC y total"),
            ("Detalle", "Codigo, descripcion, cantidad, unitario e importe"),
        ],
        variante="acct",
    )

    uploaded_files = _cargar_comprobantes("costos", "1. Cargar comprobantes")

    open_card(
        "3. Procesar y generar Excel",
        kicker="Salida final",
        texto="Genera el Excel con las hojas Documentos, Detalle, Resumen y Auditoria.",
    )

    if st.button(
        "Procesar comprobantes",
        type="primary",
        disabled=not uploaded_files,
        key="costos_procesar",
    ):
        with st.spinner("Leyendo comprobantes..."):
            excel_bytes, documentos, items = build_excel_costos(uploaded_files)

        sin_leer = sum(1 for documento in documentos if documento.get("Observaciones"))
        descuadrados = sum(1 for documento in documentos if documento.get("Cuadra Total") == "No")

        if documentos:
            render_banner("Excel generado correctamente. Ya puedes descargar la salida consolidada.")
        else:
            render_banner(
                "No se reconocio ningun comprobante. Revisa la hoja Auditoria del Excel.",
                tipo="warn",
            )

        render_result_grid([
            ("Comprobantes leidos", len(documentos)),
            ("Lineas de detalle", len(items)),
        ])

        avisar_no_leidos(uploaded_files, documentos)

        if sin_leer or descuadrados:
            render_banner(
                f"{sin_leer} comprobante(s) con campos sin leer y {descuadrados} donde "
                "gravadas + IGV no coincide con el total. Estan marcados en el Excel.",
                tipo="warn",
            )

        if documentos:
            st.dataframe(
                pd.DataFrame(documentos).reindex(columns=COLUMNAS_COSTOS_PREVIA).head(80),
                use_container_width=True,
                hide_index=True,
            )

        st.download_button(
            "Descargar Excel",
            data=excel_bytes,
            file_name="contabilidad_costos.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="costos_descargar",
        )

    close_card()

    render_benefits([
        ("Parametros completos", "32 columnas de cabecera mas el detalle de items de cada comprobante."),
        ("Control de cuadre", "Compara gravadas mas IGV contra el importe total de cada comprobante."),
        ("Nada inventado", "Lo que no se puede leer queda vacio y aparece en la hoja Auditoria."),
    ])

