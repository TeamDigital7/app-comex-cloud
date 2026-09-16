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

import forus_ocr
import pandas as pd
import pdfplumber
import streamlit as st

from forus_parsing import (
    buscar_mes_escrito,
    buscar_periodo_documento,
    fecha_es,
    collapse_spaces,
    cuadra,
    detect_currency_document,
    extraer_periodo,
    faltantes,
    find_date,
    find_label_value,
    find_money,
    find_percent,
    find_nombre_emisor,
    find_ruc,
    mes_de_fecha,
    normalize,
    parse_amount,
    split_lines,
)
from forus_conceptos import concepto_o_original
from forus_comprobante import (
    extraer_lineas_detalle,
    zona_detalle,
    deducir_totales_por_suma,
    NUMERO_FACTURA_RE,
    agrupar_documentos,
    detect_numero_factura,
    detect_tipo_documento,
)
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

# Formatos de local vistos en las facturas: "S 120/122", "S209/211",
# "LC-163A/161", "LC-149,LC-150", "P03-A01-L120", "LN20051", "NCL-222",
# "N 238/240/242/244". La familia de letras va primero para no confundir un
# codigo de local con un importe.
LOCAL_CODIGO_RE = re.compile(
    r"((?:LCS|LCN|LC|LN|LF|NCL|LZE|SA|[NSLP])\s?-?\s?"
    r"\d{1,5}[A-Z]?(?:\s*[/,-]\s*(?:LC|L)?\s?\d{1,5}[A-Z]?)*)"
)

ETIQUETAS_LOCAL = [
    "LOCAL COMERCIAL", "NOMBRE DE CONTRATO", "LOCAL", "TIENDA", "UNIDAD",
]


def extraer_local(texto):
    """Codigo del local, probando los formatos de cada centro comercial.

    Devuelve None si no aparece: es preferible dejarlo vacio y marcarlo que
    inventar un codigo.
    """
    # 1) Etiqueta propia: "LOCAL :S146 HUSH PUPPIES".
    valor = find_label_value(texto, ETIQUETAS_LOCAL, max_chars=45,
                             requiere_separador=True)
    if valor and not any(p in normalize(valor) for p in
                         ("CONDICION", "PAGO", "ZONA", "PEDIDO", "ORDEN", "CODIGO")):
        return collapse_spaces(valor)

    # 2) "CON REFERENCIA A: S 120/122" y "Local: 99" dentro del detalle.
    referencia = find_label_value(texto, ["CON REFERENCIA A", "LOCAL"],
                                  max_chars=40, requiere_separador=True)
    if referencia:
        match = LOCAL_CODIGO_RE.search(normalize(referencia))
        if match:
            return collapse_spaces(match.group(1))
        if re.fullmatch(r"[\dA-Z\s/,-]{1,20}", normalize(referencia) or ""):
            return collapse_spaces(referencia)

    # 3) La observacion de Jockey: "BSOUL / NCL-222 / NRO. DE CONTRATO: 9244",
    #    donde el local es el campo del medio.
    observacion = find_label_value(texto, ["OBSERVACION"], max_chars=70)
    if observacion:
        partes = [p.strip() for p in observacion.split("/") if p.strip()]
        for parte in partes[1:]:
            if "CONTRATO" in normalize(parte):
                break
            if re.search(r"\d", parte):
                return collapse_spaces(parte)

    # 4) Encabezado de tabla con el valor en la linea de abajo, como hace Real
    #    Plaza: "... ORDEN DE COMPRA LOCAL COMERCIAL CONDICIONES DE PAGO" y
    #    debajo "2051481127 1800946379 LC-111B A 30 Dias".
    local = local_bajo_encabezado(texto)
    if local:
        return local

    # 5) El local escrito dentro de la glosa: "Renta minima mes Agosto 2026 -
    #    Local: 99" es como lo pone la PUCP en las observaciones.
    for linea in split_lines(texto):
        if "LOCAL" not in normalize(linea):
            continue
        match = re.search(r"(?i)\bLOCAL\s*[:.\-]?\s*([A-Z0-9][\w/,\-]{0,18})", linea)
        if match:
            candidato = collapse_spaces(match.group(1))
            if candidato and re.search(r"\d", candidato):
                return candidato

    return None


def local_bajo_encabezado(texto):
    """Valor de la columna "LOCAL COMERCIAL" cuando el titulo va en otra linea.

    Se localiza en que posicion empieza el titulo y se busca, en la linea de
    valores de debajo, el trozo que cae mas o menos bajo esa columna.
    """
    lineas = split_lines(texto)
    for indice, linea in enumerate(lineas[:-1]):
        plano = normalize(linea)
        posicion = plano.find("LOCAL COMERCIAL")
        if posicion < 0:
            continue

        # Solo si de verdad es una fila de encabezados y no una frase.
        if not any(t in plano for t in ("CODIGO", "ZONA", "PEDIDO", "CONDICIONES", "ORDEN")):
            continue

        siguiente = lineas[indice + 1]
        for match in LOCAL_CODIGO_RE.finditer(normalize(siguiente)):
            # El codigo tiene que caer cerca de la columna del titulo.
            if abs(match.start() - posicion) <= 30:
                return collapse_spaces(match.group(1))

        # Si no encaja por posicion, vale cualquier codigo con letra y numero.
        match = re.search(r"\b([A-Z]{1,3}-?\d{1,5}[A-Z]?(?:[/,-][\dA-Z]+)*)\b",
                          normalize(siguiente))
        if match:
            return collapse_spaces(match.group(1))

    return None


def numerar_documento(texto):
    """Serie y correlativo. Las notas de credito llevan NC delante."""
    numero = detect_numero_factura(texto)
    if not numero:
        return None
    tipo = detect_tipo_documento(texto)
    if tipo == "Nota de Credito":
        return f"NC {numero}"
    if tipo == "Nota de Debito":
        return f"ND {numero}"
    return numero


def mes_del_concepto(fila, texto_documento, cabecera):
    """Mes facturado, con el orden de prioridad que pidio Contabilidad.

    Lo de la propia linea manda sobre lo del documento: una misma factura de
    Cencosud trae conceptos de julio y de setiembre, y si se mira solo el
    documento todas las filas salen con el mismo mes.

    La fecha de emision es el ultimo recurso y siempre queda anotado: una
    factura emitida el 05/08 que dice "CORRESPONDIENTE DEL 01.07 AL 31.07" es
    de JULIO, no de agosto.
    """
    # 1) Periodo escrito en la propia linea del concepto (o en su continuacion).
    if fila.get("desde"):
        return mes_de_fecha(fila["desde"]), fila["desde"], None

    # 2) Mes con letras en la propia linea: "AGOSTO 2026 - RENTA VARIABLE".
    propio = buscar_mes_escrito(
        " ".join(x for x in [fila.get("concepto"), fila.get("continuacion")] if x)
    )
    if propio:
        return propio, None, None

    # 3) Periodo declarado en cualquier parte del documento.
    desde, _ = buscar_periodo_documento(texto_documento)
    if desde:
        return mes_de_fecha(desde), desde, None

    # 4) Mes escrito con letras, "JULIO 2026 - GASTO COMUN FIJO".
    escrito = buscar_mes_escrito(texto_documento)
    if escrito:
        return escrito, None, None

    # 5) Solo entonces, el mes de emision, y avisando.
    mes = mes_de_fecha(cabecera.get("Fecha"))
    if mes:
        return mes, None, "MES tomado de la fecha de emision: verificar"
    return None, None, "No se pudo determinar el mes facturado"


def clave_duplicado(fila):
    """Identidad de una linea del control: factura + local + concepto + periodo."""
    return (
        fila.get("# Factura"),
        normalize(fila.get("Tienda") or ""),
        normalize(fila.get("Concepto") or ""),
        fila.get("Periodo Desde"),
    )


def descartar_duplicados(filas):
    """Quita las lineas repetidas y marca las que se contradicen.

    Una factura si puede tener varios conceptos distintos, y hasta el mismo
    concepto por periodos distintos. Lo que no puede es traer dos veces el
    mismo concepto, el mismo periodo y el mismo importe: eso es que se leyo dos
    veces la misma linea.
    """
    vistas = {}
    resultado = []

    for fila in filas:
        clave = clave_duplicado(fila)
        importe = fila.get("SOLES") if fila.get("SOLES") is not None else fila.get("DOLARES")
        previa = vistas.get(clave)

        if previa is None:
            vistas[clave] = (fila, importe)
            resultado.append(fila)
            continue

        fila_previa, importe_previo = previa
        if importe is not None and importe_previo is not None and abs(importe - importe_previo) < 0.01:
            continue  # linea repetida: se descarta sin ruido

        # Mismo concepto y periodo pero importe distinto: puede ser legitimo,
        # asi que se conserva y se marca para que alguien lo mire.
        aviso = "Mismo concepto y periodo con importe distinto: revisar"
        for objetivo in (fila, fila_previa):
            objetivo["Observaciones"] = "; ".join(
                x for x in [objetivo.get("Observaciones"), aviso] if x
            )
        resultado.append(fila)

    return resultado


def tolerancia_suma(filas):
    """Cuanto se admite de diferencia al cuadrar: un centimo por linea."""
    return 0.10 + 0.01 * len(filas)


def elegir_columna_valor(filas, base_imponible, total_documento=None):
    """Que columna de la tabla es el valor de venta, y si viene con IGV.

    Devuelve (posicion, cuadra, factor). La posicion se cuenta desde el final
    porque cada emisor pone las columnas en un orden distinto.

    Se prueban dos cuadres, en este orden:

    1. Contra la base imponible: la columna cuya suma da las operaciones
       gravadas mas inafectas es el valor de venta sin IGV, que es el que va al
       control.
    2. Contra el importe total: hay emisores -Cencosud es el caso tipico- que
       en la tabla solo imprimen el precio de venta, ya con IGV. Entonces se
       coge esa columna y se devuelve el factor con el que hay que bajarla a
       valor de venta, para que el control no quede inflado un 18%.
    """
    if not filas:
        return -1, False, 1.0

    if base_imponible is None:
        return -1, False, 1.0

    maximo = max(len(fila["numeros"]) for fila in filas)
    tolerancia = tolerancia_suma(filas)

    def suma_columna(posicion):
        total = 0
        for fila in filas:
            if len(fila["numeros"]) < posicion:
                return None
            total += fila["numeros"][-posicion]
        return total

    for posicion in range(1, maximo + 1):
        suma = suma_columna(posicion)
        if suma is not None and abs(suma - base_imponible) <= tolerancia:
            return -posicion, True, 1.0

    # La tabla trae los importes con IGV: se identifican porque suman el total.
    if total_documento and total_documento > base_imponible > 0:
        for posicion in range(1, maximo + 1):
            suma = suma_columna(posicion)
            if suma is not None and abs(suma - total_documento) <= tolerancia:
                return -posicion, True, base_imponible / suma

    return -1, False, 1.0


def valores_sin_igv(filas, posicion, factor, base_imponible):
    """Importe de cada linea ya sin IGV, cuadrando la suma con la base.

    Al bajar cada linea por separado sobran o faltan centimos; el resto se
    carga a la linea mas grande, que es donde menos se nota, para que la suma
    de las filas sea exactamente la base imponible de la factura.
    """
    brutos = [
        fila["numeros"][posicion] if len(fila["numeros"]) >= abs(posicion)
        else fila["numeros"][-1]
        for fila in filas
    ]

    if factor == 1.0:
        return brutos, brutos

    netos = [round(bruto * factor, 2) for bruto in brutos]

    if base_imponible is not None and netos:
        diferencia = round(base_imponible - sum(netos), 2)
        if diferencia:
            mayor = max(range(len(netos)), key=lambda indice: abs(netos[indice]))
            netos[mayor] = round(netos[mayor] + diferencia, 2)

    return netos, brutos


def extraer_cabecera(texto, pagina_inicial):
    tipo = detect_tipo_documento(texto)
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

    ruc = find_ruc(texto, ["R.U.C.", "RUC"])
    local = extraer_local(texto)
    # "LOCAL COMERCIAL" a veces es solo un encabezado de columna y lo que sigue
    # en la linea es otro encabezado, no un valor.
    if local and any(palabra in normalize(local) for palabra in
                     ("CONDICION", "PAGO", "ZONA", "PEDIDO", "ORDEN", "CODIGO")):
        local = None

    fila = {
        "Pagina Inicial": pagina_inicial,
        "# Factura": numerar_documento(texto),
        "Tipo Documento": tipo,
        "Fecha": find_date(texto, [
            "FECHA DE EMISION", "FECHA EMISION", "F. EMISION", "FECHA",
        ]),
        "Fecha Vencimiento": find_date(texto, [
            "FECHA DE VENCIMIENTO", "FECHA VENCIMIENTO", "FECHA VENC",
        ]),
        "RUC Emisor": ruc,
        # El RUC manda: muchos emisores llevan su razon social dentro del logo,
        # que es una imagen, y de la cabecera solo se puede leer el nombre
        # comercial del centro comercial.
        "RAZON SOCIAL": en_mayusculas(
            find_nombre_emisor(texto, excluir=("FORUS",), ruc=ruc)
        ),
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
    total_documento = cabecera.get("Importe Total")
    posicion, cuadro, factor = elegir_columna_valor(filas_detalle, base, total_documento)
    moneda = cabecera.get("Moneda")
    ruc = cabecera.get("RUC Emisor")

    netos, brutos = valores_sin_igv(filas_detalle, posicion, factor, base)
    suma_base = sum(valor for valor in netos if valor is not None)
    igv_total = cabecera.get("IGV") or 0

    # El local de la cabecera solo vale cuando la linea no trae el suyo: una
    # misma factura de Cencosud cobra tres locales distintos, uno por linea, y
    # antes salian los tres con el primero.
    tienda_documento = (
        cabecera.get("Local / Tienda")
        or cabecera.get("Contrato")
        or cabecera.get("Referencia")
    )

    control = []
    for indice, fila in enumerate(filas_detalle):
        valor = netos[indice]
        bruto = brutos[indice]
        tienda = fila.get("local") or tienda_documento

        mes, periodo_desde, aviso_mes = mes_del_concepto(
            fila, mes_documento or "", cabecera
        )
        if periodo_desde and not fila.get("desde"):
            fila["desde"] = periodo_desde
        notas = [aviso_mes] if aviso_mes else []

        # El concepto se traduce a su nombre canonico; si no se reconoce, se
        # deja la descripcion limpia y se marca para revision.
        concepto, reconocido = concepto_o_original(
            fila["concepto"], ruc=ruc, respaldo=fila.get("continuacion"),
        )
        if not reconocido:
            notas.append("Concepto sin normalizar: revisar")
        if not tienda:
            notas.append("Sin local ni contrato en el documento: completar a mano")
        if not cabecera.get("RAZON SOCIAL"):
            notas.append(
                "Sin razon social: este emisor la imprime dentro del logo. "
                f"Anade el RUC {ruc or ''} a forus_emisores.py".strip()
            )
        if factor != 1.0:
            notas.append("La factura solo trae el importe con IGV: se convirtio a valor de venta")
        if not cuadro and base is not None:
            notas.append("El detalle no cuadra con la base imponible")
        if base is None:
            notas.append("Sin totales para verificar el importe")

        # Con IGV: el importe tal como lo imprime la factura cuando ya viene
        # asi, y si no, el valor de venta mas la parte de IGV que le toca.
        if factor != 1.0:
            con_igv = bruto
        elif valor is not None and suma_base:
            con_igv = round(valor + igv_total * (valor / suma_base), 2)
        else:
            con_igv = None

        control.append({
            "Fecha": fecha_es(cabecera.get("Fecha")),
            "Tienda": tienda,
            "MES": mes,
            "Concepto": concepto,
            "SOLES": valor if moneda == "PEN" else None,
            "DOLARES": valor if moneda == "USD" else None,
            "# Factura": cabecera.get("# Factura"),
            "RAZON SOCIAL": cabecera.get("RAZON SOCIAL"),
            "FECHA DE ENTREGA": None,  # no viene en el PDF, se completa a mano
            "Contrato": cabecera.get("Contrato"),
            "Local": tienda,
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

    return descartar_duplicados(control), suma_base, cuadro


def leer_paginas(uploaded_file):
    """Texto de cada pagina; las escaneadas se leen por OCR."""
    datos = uploaded_file.getvalue()
    paginas = []
    with pdfplumber.open(io.BytesIO(datos)) as pdf:
        for numero, page in enumerate(pdf.pages, start=1):
            paginas.append({"numero": numero, "texto": page.extract_text() or ""})

    forus_ocr.completar_paginas_vacias(datos, paginas)
    return paginas


def en_mayusculas(texto):
    """La razon social se lleva en mayusculas, venga de donde venga."""
    limpio = collapse_spaces(texto)
    return limpio.upper() if limpio else None


def razon_social_por_ocr(datos_pdf, pagina, texto):
    """Ultimo recurso para el nombre del emisor: leer el logo por OCR.

    Solo se llega aqui cuando el RUC no esta en el directorio y la cabecera no
    trae el nombre como texto. Se lee unicamente la franja de arriba de la
    pagina, que es donde va el logo, y se busca ahi una razon social.
    """
    texto_cabecera = forus_ocr.texto_de_cabecera_por_ocr(datos_pdf, pagina)
    if not texto_cabecera:
        return None
    return en_mayusculas(find_nombre_emisor(texto_cabecera, excluir=("FORUS",)))


def process_pdf(uploaded_file):
    """Devuelve (control, facturas, auditoria) para un PDF."""
    datos_pdf = uploaded_file.getvalue()
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

        # La razon social del emisor esta dentro del logo en bastantes
        # facturas: si no vino en el texto ni esta en el directorio de RUCs, se
        # intenta leer el logo por OCR antes de dejar la columna vacia.
        if not cabecera.get("RAZON SOCIAL"):
            cabecera["RAZON SOCIAL"] = razon_social_por_ocr(
                datos_pdf, documento["pagina_inicial"], texto,
            )

        filas_detalle = extraer_lineas_detalle(texto)
        control, suma_base, cuadro = construir_filas_control(
            cabecera,
            filas_detalle,
            uploaded_file.name,
            mes_documento=texto,
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
        pd.DataFrame(control).reindex(columns=CONTROL_COLUMNS).to_excel(
            writer, index=False, sheet_name="Control",
        )
        pd.DataFrame(control).reindex(columns=CONTROL_AMPLIADO_COLUMNS).to_excel(
            writer, index=False, sheet_name="Control ampliado",
        )
        pd.DataFrame(facturas).reindex(columns=FACTURA_COLUMNS).to_excel(
            writer, index=False, sheet_name="Facturas",
        )
        pd.DataFrame(resumen).reindex(columns=SUMMARY_COLUMNS).to_excel(
            writer, index=False, sheet_name="Resumen",
        )
        pd.DataFrame(auditoria).reindex(columns=AUDIT_COLUMNS).to_excel(
            writer, index=False, sheet_name="Auditoria",
        )

    output.seek(0)
    return output, control, facturas


def render_sidebar():
    """Ayuda propia del modulo de arriendos."""
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


ICONO_ARRIENDOS = """
<svg viewBox="0 0 96 96" width="72" height="72" aria-label="Alquileres">
    <rect x="14" y="30" width="30" height="46" rx="4" fill="#ffffff" stroke="#d3c0f7"/>
    <rect x="50" y="16" width="32" height="60" rx="4" fill="#6d3fd1"/>
    <rect x="21" y="38" width="7" height="7" rx="2" fill="#d3c0f7"/>
    <rect x="32" y="38" width="7" height="7" rx="2" fill="#d3c0f7"/>
    <rect x="21" y="52" width="7" height="7" rx="2" fill="#d3c0f7"/>
    <rect x="32" y="52" width="7" height="7" rx="2" fill="#d3c0f7"/>
    <rect x="57" y="26" width="8" height="8" rx="2" fill="#ffffff"/>
    <rect x="69" y="26" width="8" height="8" rx="2" fill="#ffffff"/>
    <rect x="57" y="42" width="8" height="8" rx="2" fill="#ffffff"/>
    <rect x="69" y="42" width="8" height="8" rx="2" fill="#ffffff"/>
    <rect x="61" y="58" width="12" height="18" rx="2" fill="#ffffff"/>
</svg>
"""

# Se muestra la hoja Control tal cual saldra en el Excel, sin columnas extra.
# Se muestra la hoja Control tal cual saldra en el Excel.
COLUMNAS_VISTA_PREVIA = CONTROL_COLUMNS


def render():
    """Pantalla completa del modulo de arriendos."""
    render_hero(
        "CONTROL DE ARRIENDOS",
        'Facturas de alquiler <span style="color:#d9c2ff">&rsaquo;</span> Excel de control',
        "Sube las facturas de los centros comerciales y genera el control con una fila por concepto: renta minima, fondo de promociones, gastos comunes y los demas.",
        tags=[("Una fila por concepto", "green")],
        variante="hr",
        icono_svg=ICONO_ARRIENDOS,
    )

    render_pipeline([
        ("Input", "Facturas PDF", "active", "Pend."),
        ("Lectura", "Contrato y conceptos", "ok", "OK"),
        ("Validacion", "Conceptos vs base imponible", "warn", "Revisar"),
        ("Salida", "Excel de control", "", "Pend."),
    ])

    render_rules(
        "Preparar lectura de facturas",
        "Cada concepto de la factura se convierte en una fila. El importe se toma sin IGV y se verifica que la suma de los conceptos cuadre con la base imponible del documento.",
        [
            ("Contrato", "Numero de contrato y local, segun lo que traiga la factura"),
            ("Concepto", "Descripcion limpia y mes del periodo facturado"),
            ("Moneda", "El importe cae en la columna SOLES o DOLARES"),
        ],
        variante="hr",
    )

    st.markdown(f'<div class="work-card upload-wrap hr"><h3>1. Cargar facturas</h3></div>', unsafe_allow_html=True)
    uploaded_files = st.file_uploader(
        "Subir PDFs de facturas de alquiler",
        type=["pdf"],
        accept_multiple_files=True,
        label_visibility="collapsed",
        key="alquileres_uploader",
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
                    f"{format_file_size(getattr(archivo, 'size', None))} - Factura de alquiler",
                    "PDF",
                    "Listo",
                )
                for archivo in uploaded_files
            ],
            variante="hr",
        )
    else:
        render_empty("Carga las facturas de los centros comerciales para comenzar.")

    close_card()

    open_card(
        "3. Procesar y generar Excel",
        kicker="Salida final",
        texto="Genera el Excel con la hoja Control lista para pegar, mas Control ampliado, Facturas, Resumen y Auditoria.",
    )

    if st.button(
        "Procesar facturas",
        type="primary",
        disabled=not uploaded_files,
        key="alquileres_procesar",
    ):
        with st.spinner("Leyendo facturas..."):
            excel_bytes, control, facturas = build_excel(uploaded_files)

        descuadradas = sum(1 for factura in facturas if factura.get("Cuadra Base") == "No")
        sin_conceptos = sum(1 for factura in facturas if not factura.get("Conceptos"))

        if control:
            render_banner("Excel generado correctamente. Ya puedes descargar el control.")
        else:
            render_banner(
                "No se reconocio ninguna factura. Revisa la hoja Auditoria del Excel.",
                tipo="warn",
            )

        render_result_grid([
            ("Facturas leidas", len(facturas)),
            ("Filas de control", len(control)),
        ])

        if descuadradas or sin_conceptos:
            render_banner(
                f"{descuadradas} factura(s) donde los conceptos no cuadran con la base "
                f"imponible y {sin_conceptos} sin conceptos leidos. Estan marcadas en el Excel.",
                tipo="warn",
            )

        if control:
            st.dataframe(
                pd.DataFrame(control).reindex(columns=COLUMNAS_VISTA_PREVIA).head(80),
                use_container_width=True,
                hide_index=True,
            )

        st.download_button(
            "Descargar Excel",
            data=excel_bytes,
            file_name="control_alquileres.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="alquileres_descargar",
        )

    close_card()

    render_benefits([
        ("Listo para pegar", "La hoja Control trae solo tus columnas, en tu orden, sin nada que borrar."),
        ("Importe verificado", "La columna del valor sin IGV se elige comprobando que la suma cuadre con la factura."),
        ("Tienda o contrato", "La columna Tienda toma el local y, cuando la factura no lo trae, el numero de contrato."),
    ])

