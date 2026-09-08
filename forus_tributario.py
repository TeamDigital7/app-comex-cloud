"""Detraccion y retencion: decide sola si una factura esta afecta y por cuanto.

La regla practica es esta:

1. Si el comprobante dice que esta sujeto a detraccion (SPOT), manda eso. El
   proveedor ya determino el tipo de servicio y su tasa, y ahi no hay que
   adivinar: se lee el porcentaje que trae impreso.
2. Si no hay detraccion y el importe total no pasa de S/ 700, no corresponde
   retencion.
3. Si pasa de S/ 700 y el proveedor no es agente de retencion, buen
   contribuyente ni agente de percepcion, corresponde retencion del IGV.

Detraccion y retencion son excluyentes: nunca se aplican las dos a la vez.

Cuando falta un dato para decidir -por ejemplo una factura en dolares sin tipo
de cambio con el que llevar el importe a soles- no se inventa una respuesta: la
fila sale como REVISAR diciendo que falta.
"""
import re

import streamlit as st

from forus_parsing import aplanar, aplanar_con_reverso, find_money, normalize, parse_amount

UMBRAL_DEFECTO = 700.0
TASA_RETENCION_DEFECTO = 3.0

# La detraccion no se aplica a operaciones de hasta S/ 700: es el minimo del
# SPOT. Sin esto, las facturas de centro comercial -que llevan la leyenda del
# SPOT impresa siempre, tambien en los reembolsos de S/ 130- salian detraidas.
MINIMO_DETRACCION = 700.0

# Renta de cuarta categoria: se retiene el 8% del recibo por honorarios cuando
# pasa de S/ 1,500, salvo que el emisor tenga suspension de retenciones.
TASA_RENTA_CUARTA = 8.0
MINIMO_RENTA_CUARTA = 1500.0

# Tope de plausibilidad para una retencion leida del comprobante. La del IGV es
# del 3%; cualquier cosa muy por encima es que se leyo otro numero.
TASA_RETENCION_MAXIMA = 6.0

# ---------------------------------------------------------------------------
# Tabla de tasas por codigo de bien o servicio.
#
# AVISO: las tasas de detraccion las modifica SUNAT por resolucion. Esta tabla
# es un punto de partida que Contabilidad tiene que confirmar y mantener. Solo
# se usa cuando el comprobante NO imprime el porcentaje; si lo imprime, manda
# siempre el del documento. Se puede corregir desde los secrets sin tocar
# codigo, con una seccion asi:
#
#     [detraccion_tasas]
#     "019" = 10
#     "020" = 12
#
# Cada entrada es codigo: (nombre del bien o servicio, tasa).
# ---------------------------------------------------------------------------
TASAS_DETRACCION = {
    # --- Anexo 2: bienes ---
    "004": ("Recursos hidrobiologicos", 4.0),
    "005": ("Maiz amarillo duro", 4.0),
    "008": ("Madera", 4.0),
    "009": ("Arena y piedra", 10.0),
    "010": ("Residuos, subproductos, desechos, recortes y desperdicios", 15.0),
    "011": ("Bienes gravados con el IGV por renuncia a la exoneracion", 10.0),
    "014": ("Carnes y despojos comestibles", 4.0),
    "015": ("Abonos, cueros y pieles de origen animal", 4.0),
    "016": ("Aceite de pescado", 10.0),
    "017": ("Harina, polvo y pellets de pescado", 4.0),
    "018": ("Embarcaciones pesqueras", 10.0),
    "023": ("Leche", 4.0),
    "029": ("Algodon en rama sin desmotar", 10.0),
    "031": ("Oro gravado con el IGV", 10.0),
    "032": ("Paprika y otros frutos del genero capsicum", 10.0),
    "033": ("Esparragos", 10.0),
    "034": ("Minerales metalicos no auriferos", 10.0),
    "035": ("Bienes exonerados del IGV", 1.5),
    "036": ("Oro y demas minerales metalicos exonerados del IGV", 1.5),
    "039": ("Minerales no metalicos", 10.0),
    "041": ("Plomo", 15.0),
    # --- Anexo 3: servicios ---
    "012": ("Intermediacion laboral y tercerizacion", 12.0),
    "019": ("Arrendamiento de bienes", 10.0),
    "020": ("Mantenimiento y reparacion de bienes muebles", 12.0),
    "021": ("Movimiento de carga", 10.0),
    "022": ("Otros servicios empresariales", 12.0),
    "024": ("Comision mercantil", 10.0),
    "025": ("Fabricacion de bienes por encargo", 10.0),
    "026": ("Servicio de transporte de personas", 10.0),
    "027": ("Servicio de transporte de bienes por via terrestre", 4.0),
    "030": ("Contratos de construccion", 4.0),
    "037": ("Demas servicios gravados con el IGV", 12.0),
}

# Codigos vistos en facturas reales de Forus: su tasa esta contrastada contra
# el propio comprobante. El resto de la tabla sale de la norma y Contabilidad
# tiene que validarlo contra la tabla oficial de SUNAT antes de confiar en el.
CODIGOS_VERIFICADOS = {"019", "021", "022"}

# Ultimo recurso: cuando el comprobante no trae ni tasa ni codigo, se busca el
# concepto en la glosa. Es una inferencia y la fila queda marcada como tal,
# porque la descripcion enganya: un "MANTENIMIENTO" dentro de un arrendamiento
# tributa como arrendamiento (019, 10%) y no como el codigo 020 (12%).
PALABRAS_POR_CODIGO = [
    ("019", ("ARRENDAMIENTO", "ARRIENDO", "ALQUILER", "RENTA MINIMA", "LOCAL COMERCIAL",
             # Como rotulan los centros comerciales lo que facturan por el
             # local: todo forma parte del arrendamiento y detrae al 10%.
             "CONTRAPRESTACION",
             "RENTA VARIABLE", "RENTA FIJA", "GASTO COMUN", "GASTOS COMUNES",
             "FONDO PROMOCION", "FONDO DE PROMOCION", "REEMBOLSO ARBITRIOS",
             "MERCED CONDUCTIVA")),
    ("027", ("TRANSPORTE DE BIENES", "FLETE", "TRANSPORTE DE CARGA",
             "TRANSPORTE DE MERCADERIA", "TRANSPORTE DE MERCANCIA",
             "SERVICIO DE TRANSPORTE DE", "TRANSPORTE TERRESTRE")),
    ("021", ("MOVIMIENTO DE CARGA", "ESTIBA", "DESESTIBA", "CARGA Y DESCARGA")),
    ("026", ("TRANSPORTE DE PERSONAL", "TRANSPORTE DE PERSONAS", "MOVILIDAD DEL PERSONAL")),
    ("012", ("INTERMEDIACION LABORAL", "TERCERIZACION", "DESTAQUE DE PERSONAL")),
    ("020", ("MANTENIMIENTO Y REPARACION", "REPARACION DE", "MANTENIMIENTO DE EQUIPO")),
    ("030", ("CONTRATO DE CONSTRUCCION", "OBRA CIVIL")),
    ("025", ("FABRICACION POR ENCARGO", "BIENES POR ENCARGO")),
    ("024", ("COMISION MERCANTIL",)),
]

DETRACCION = "DETRACCION"
RETENCION = "RETENCION"
RETENCION_RENTA = "RETENCION 4TA"
NO_AFECTO = "NO AFECTO"
REVISAR = "REVISAR"

# Documentos que no generan detraccion ni retencion del IGV.
RECIBO_HONORARIOS = "Recibo por Honorarios"
DOCUMENTO_EXTERIOR = "Documento del Exterior"

# Las leyendas del SPOT se buscan con hueco entre sus partes: las facturas las
# imprimen a dos columnas y el extractor intercala el texto de la otra columna
# ("SISTEMA DE PAGO DE / Autorizado mediante resolucion... / OBLIGACIONES
# TRIBUTARIAS"). Buscando la frase entera y seguida no aparecia nunca, y las
# facturas de centro comercial salian sin detraccion.
HUECO = r"[\s\S]{0,160}?"

SENALES_DETRACCION_RE = re.compile(
    r"DETRACCION|\bSPOT\b"
    rf"|SISTEMA\s+DE\s+PAGO\s+DE{HUECO}OBLIGACIONES\s+TRIBUTARIAS"
    r"|DECRETO\s+LEGISLATIVO\s+940|D\.L\.\s*940",
)

# Si el proveedor ya es agente de retencion, percepcion o buen contribuyente,
# no se le retiene.
SENALES_SIN_RETENCION = (
    "AGENTE DE RETENCION",
    "AGENTES DE RETENCION",
    "AGENTE DE PERCEPCION",
    "AGENTES DE PERCEPCION",
    "BUEN CONTRIBUYENTE",
    "BUENOS CONTRIBUYENTES",
)

# "Porcentaje Detracción: 10.0%", "DETRACCION DEL 10 %" y tambien la forma sin
# simbolo que usa la representacion impresa de SUNAT: "Porcentaje de detracción: 10.00".
PORCENTAJE_DETRACCION_RE = re.compile(
    r"(?:PORCENTAJE\s+(?:DE\s+)?DETRACCION|DETRACCION\s+DEL?|SPOT)"
    r"[^\d%]{0,40}?(\d{1,2}(?:[.,]\d{1,2})?)\s*%",
    re.I,
)
PORCENTAJE_DETRACCION_SIN_SIMBOLO_RE = re.compile(
    r"PORCENTAJE\s+(?:DE\s+)?DETRACCI[OÓ]N\s*[:.]?\s*(\d{1,2}(?:[.,]\d{1,2})?)",
    re.I,
)
PORCENTAJE_SUELTO_RE = re.compile(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%")

# La cuenta del Banco de la Nacion es la cuenta de detracciones: una linea que
# la menciona habla de detraccion aunque no repita la palabra, y es justo donde
# varios proveedores imprimen la tasa.
CUENTA_DETRACCION_RE = re.compile(
    r"BANCO\s+DE\s+LA\s+NACION|B/\s*NACION|\bBN\b|\b940\b"
)
# El codigo identifica el bien o servicio del Anexo que fija la tasa. Cada
# proveedor lo rotula a su manera: "Codigo Detraccion: 019" o, en la
# representacion impresa de SUNAT, "Bien o Servicio: 021 Movimiento de carga".
CODIGO_DETRACCION_RE = re.compile(
    r"(?:C[OÓ]DIGO\s+(?:DE\s+)?DETRACCI[OÓ]N|BIEN\s+O\s+SERVICIO"
    r"|C[OÓ]DIGO\s+(?:DE\s+)?BIEN\s+O\s+SERVICIO)\s*[:.]?\s*(\d{3})\b",
    re.I,
)


def get_parametros():
    """Umbral y tasa de retencion, ajustables desde los secrets sin tocar codigo."""
    try:
        configurado = dict(st.secrets.get("tributario", {}))
    except Exception:
        configurado = {}

    umbral = parse_amount(configurado.get("umbral", UMBRAL_DEFECTO))
    tasa = parse_amount(configurado.get("tasa_retencion", TASA_RETENCION_DEFECTO))

    return {
        "umbral": umbral if umbral is not None else UMBRAL_DEFECTO,
        "tasa_retencion": tasa if tasa is not None else TASA_RETENCION_DEFECTO,
        # Algunos comprobantes dicen que hay detraccion pero no imprimen la
        # tasa. Por defecto no se inventa ninguna; si se configura una aqui, se
        # aplica y queda dicho en el motivo de esa fila.
        "tasa_detraccion_defecto": parse_amount(configurado.get("tasa_detraccion_defecto")),
        "tasas": get_tasas(),
    }


def get_tasas():
    """Tabla de tasas por codigo, con lo que se haya corregido en los secrets."""
    tasas = {codigo: (nombre, tasa) for codigo, (nombre, tasa) in TASAS_DETRACCION.items()}

    try:
        ajustes = dict(st.secrets.get("detraccion_tasas", {}))
    except Exception:
        ajustes = {}

    for codigo, valor in ajustes.items():
        codigo = str(codigo).strip().zfill(3)
        tasa = parse_amount(valor)
        if tasa is None:
            continue
        nombre = tasas.get(codigo, ("Codigo " + codigo, None))[0]
        tasas[codigo] = (nombre, tasa)

    return tasas


def buscar_codigo_por_concepto(glosa):
    """Codigo probable a partir de la descripcion. Es una inferencia, no un dato."""
    plano = normalize(glosa or "")
    if not plano:
        return None
    for codigo, palabras in PALABRAS_POR_CODIGO:
        if any(palabra in plano for palabra in palabras):
            return codigo
    return None


# Leyendas que solo aparecen cuando la operacion esta de verdad sujeta al SPOT.
LEYENDAS_SPOT_RE = re.compile(
    r"\bSPOT\b"
    rf"|SISTEMA\s+DE\s+PAGO\s+DE{HUECO}OBLIGACIONES\s+TRIBUTARIAS"
    r"|DECRETO\s+LEGISLATIVO\s+940|D\.L\.\s*940"
    r"|SUJET[AO]S?\s+A\s+(?:LA\s+)?DETRACCION|AFECT[AO]S?\s+A\s+(?:LA\s+)?DETRACCION"
    r"|PORCENTAJE\s+(?:DE\s+)?DETRACCION|CODIGO\s+(?:DE\s+)?DETRACCION"
    r"|CUENTA\s+DE\s+DETRACCION|DETRACCIONES\s*:",
)


def detectar_detraccion(texto):
    """(sujeta, porcentaje, codigo) segun lo que declare el propio comprobante.

    Se trabaja sobre el texto puesto en una sola linea: la leyenda del SPOT
    viene partida donde acabe la caja del PDF ("OPERACIONES SUJETA AL SISTEMA DE
    PAGO DE / OBLIGACIONES TRIBUTARIAS CON EL GOBIERNO CENTRAL") y buscada tal
    cual no aparecia nunca. Por eso las facturas de centro comercial salian como
    no sujetas a detraccion.
    """
    plano = aplanar(texto)
    if not SENALES_DETRACCION_RE.search(plano):
        return False, None, None

    # Muchas facturas listan "Detraccion: S/ 0.00" en el pie de totales aunque
    # la operacion no este sujeta. Con importe cero y sin ninguna leyenda del
    # SPOT, esa palabra no significa que haya detraccion.
    if not LEYENDAS_SPOT_RE.search(plano):
        # "Detraccion: S/ 0.00" en el pie de totales no significa nada.
        importe = find_money(texto, ["DETRACCION"])
        if importe is not None and importe == 0:
            return False, None, None

    # Sobre el texto aplanado: si no, "Código" con tilde no casa nunca, y las
    # etiquetas partidas en dos lineas tampoco.
    codigo = None
    match_codigo = CODIGO_DETRACCION_RE.search(plano)
    if match_codigo:
        codigo = match_codigo.group(1)

    porcentaje = None
    match = PORCENTAJE_DETRACCION_RE.search(plano)
    if match:
        porcentaje = parse_amount(match.group(1))

    if porcentaje is None:
        match = PORCENTAJE_DETRACCION_SIN_SIMBOLO_RE.search(plano)
        if match:
            porcentaje = parse_amount(match.group(1))

    if porcentaje is None:
        # Algunos lo imprimen suelto, en la misma linea de la cuenta del Banco
        # de la Nacion. Solo se acepta si es una tasa de detraccion plausible.
        for linea in (texto or "").splitlines():
            plano_linea = normalize(linea)
            if not (SENALES_DETRACCION_RE.search(plano_linea)
                    or CUENTA_DETRACCION_RE.search(plano_linea)):
                continue
            for candidato in PORCENTAJE_SUELTO_RE.findall(linea):
                valor = parse_amount(candidato)
                if valor is not None and 1 <= valor <= 15:
                    porcentaje = valor
                    break
            if porcentaje is not None:
                break

    return True, porcentaje, codigo


# El recibo de servicios publicos y algunos comprobantes llevan impreso que no
# entran en el regimen de retencion del IGV. Suele ir en vertical al margen, por
# eso se mira tambien el texto al reves.
NO_AFECTO_RETENCION_RE = re.compile(
    r"NO\s+AFECTO\s+AL?\s+REGIMEN\s+DE\s+RETENCION"
    r"|NO\s+SUJET[AO]S?\s+A(?:L)?\s+(?:REGIMEN\s+DE\s+)?RETENCION",
)


def proveedor_sin_retencion(texto):
    """El comprobante declara una condicion que excluye la retencion."""
    plano = aplanar_con_reverso(texto)

    if NO_AFECTO_RETENCION_RE.search(plano):
        return "Documento no afecto al regimen de retencion del IGV"

    for senal in SENALES_SIN_RETENCION:
        if senal in plano:
            return senal.capitalize()
    return None


def importe_en_soles(total, moneda, tipo_cambio):
    """Lleva el importe a soles, que es donde se mide el umbral."""
    if total is None:
        return None
    if moneda in (None, "PEN"):
        return total
    if tipo_cambio:
        return round(total * tipo_cambio, 2)
    return None


SUSPENSION_RENTA_RE = re.compile(
    r"SUSPENSI[OÓ]N\s+DE\s+RETENCI|4TA\s+CATEGORIA\s*:?\s*AUTORIZADO"
    r"|FORM(?:ULARIO)?\.?\s*1609",
    re.I,
)


def tiene_suspension_renta(texto):
    """El emisor adjunta la constancia de suspension de retenciones de 4ta."""
    return bool(SUSPENSION_RENTA_RE.search(aplanar(texto)))


def evaluar_recibo_honorarios(total, texto, monto_retencion_renta, en_soles,
                              texto_anexos=None):
    """Recibo por honorarios: renta de cuarta, nunca detraccion ni IGV.

    Se respeta lo que el propio recibo imprime en "Retención (8 %) IR", que es
    donde el emisor ya aplico -o no- su suspension de retenciones. Solo cuando
    el recibo no lo dice se calcula: 8% si pasa de S/ 1,500.
    """
    base = {
        "Codigo Detraccion": None,
        "Concepto Detraccion": None,
        "Importe en Soles": en_soles,
    }

    # La constancia de suspension suele venir adjunta al recibo, no dentro de
    # el, asi que se busca tambien en los anexos del mismo PDF.
    con_anexos = f"{texto or ''}\n{texto_anexos or ''}"
    suspendido = tiene_suspension_renta(con_anexos)

    if monto_retencion_renta is not None:
        if monto_retencion_renta > 0:
            porcentaje = round(monto_retencion_renta / total * 100, 2) if total else None
            return {
                **base,
                "Afecto a": RETENCION_RENTA,
                "Motivo": "Recibo por honorarios: retencion de renta de 4ta que trae impresa",
                "Origen Tasa": "comprobante",
                "% Aplicado": porcentaje,
                "Monto Detraccion/Retencion": monto_retencion_renta,
                "Neto a Pagar": round(total - monto_retencion_renta, 2),
            }
        if suspendido:
            return {
                **base,
                "Afecto a": NO_AFECTO,
                "Motivo": ("Recibo por honorarios sin retencion: lo consigna en cero y "
                           "adjunta la constancia de suspension de retenciones"),
                "Origen Tasa": "comprobante",
                "% Aplicado": None,
                "Monto Detraccion/Retencion": 0.0,
                "Neto a Pagar": total,
            }

        # Por encima del minimo, un cero sin constancia de suspension no basta:
        # si de verdad no corresponde retener hay que verlo, y si corresponde y
        # no se retiene, la multa la paga quien paga el recibo.
        if en_soles is not None and en_soles > MINIMO_RENTA_CUARTA:
            return {
                **base,
                "Afecto a": REVISAR,
                "Motivo": (f"El recibo consigna retencion cero pero supera los "
                           f"S/ {MINIMO_RENTA_CUARTA:,.0f} y no se adjunta la constancia "
                           f"de suspension: confirmar antes de pagar "
                           f"(retencion del {TASA_RENTA_CUARTA:.0f}% = "
                           f"S/ {total * TASA_RENTA_CUARTA / 100:,.2f})"),
                "Origen Tasa": "comprobante",
                "% Aplicado": None,
                "Monto Detraccion/Retencion": None,
                "Neto a Pagar": None,
            }

        return {
            **base,
            "Afecto a": NO_AFECTO,
            "Motivo": (f"Recibo por honorarios de S/ {en_soles:,.2f}: lo consigna en cero "
                       f"y no llega al minimo de S/ {MINIMO_RENTA_CUARTA:,.0f} para "
                       f"retener renta de 4ta"),
            "Origen Tasa": "comprobante",
            "% Aplicado": None,
            "Monto Detraccion/Retencion": 0.0,
            "Neto a Pagar": total,
        }

    if suspendido:
        return {
            **base,
            "Afecto a": NO_AFECTO,
            "Motivo": "Recibo por honorarios con constancia de suspension de retenciones (4ta)",
            "Origen Tasa": "comprobante",
            "% Aplicado": None,
            "Monto Detraccion/Retencion": 0.0,
            "Neto a Pagar": total,
        }

    if en_soles is not None and en_soles <= MINIMO_RENTA_CUARTA:
        return {
            **base,
            "Afecto a": NO_AFECTO,
            "Motivo": (f"Recibo por honorarios de S/ {en_soles:,.2f}: no llega al minimo "
                       f"de S/ {MINIMO_RENTA_CUARTA:,.0f} para retener renta de 4ta"),
            "Origen Tasa": "regla",
            "% Aplicado": None,
            "Monto Detraccion/Retencion": 0.0,
            "Neto a Pagar": total,
        }

    monto = round(total * TASA_RENTA_CUARTA / 100, 2)
    return {
        **base,
        "Afecto a": RETENCION_RENTA,
        "Motivo": (f"Recibo por honorarios de mas de S/ {MINIMO_RENTA_CUARTA:,.0f} sin "
                   f"suspension: retencion de renta de 4ta del {TASA_RENTA_CUARTA:.0f}%"),
        "Origen Tasa": "regla",
        "% Aplicado": TASA_RENTA_CUARTA,
        "Monto Detraccion/Retencion": monto,
        "Neto a Pagar": round(total - monto, 2),
    }


def evaluar(total, moneda, tipo_cambio, texto, monto_detraccion=None, parametros=None,
            igv=None, tipo_documento=None, glosa=None, monto_retencion=None,
            monto_retencion_renta=None, exterior=False, texto_anexos=None):
    """Decide si la factura va con detraccion, con retencion o con ninguna.

    Devuelve las columnas listas para el Excel, incluido el motivo, para que no
    haya que rehacer el razonamiento a mano factura por factura.
    """
    parametros = parametros or get_parametros()
    umbral = parametros["umbral"]
    tasa_retencion = parametros["tasa_retencion"]

    en_soles = importe_en_soles(total, moneda, tipo_cambio)
    sujeta, porcentaje, codigo = detectar_detraccion(texto)

    vacio = {
        "Afecto a": REVISAR,
        "Motivo": None,
        "Codigo Detraccion": codigo,
        "Concepto Detraccion": None,
        "Origen Tasa": None,
        "% Aplicado": None,
        "Monto Detraccion/Retencion": None,
        "Neto a Pagar": None,
        "Importe en Soles": en_soles,
    }

    if total is None:
        vacio["Motivo"] = "No se pudo leer el importe total"
        return vacio

    # 0.a Un comprobante del exterior no esta en el sistema tributario peruano:
    # ni detraccion ni retencion del IGV. Lo que si puede corresponder -renta de
    # no domiciliados, IGV por utilizacion de servicios- se decide fuera de este
    # control y por eso se dice en el motivo.
    if exterior:
        return {
            "Afecto a": NO_AFECTO,
            "Motivo": ("Comprobante del exterior (proveedor no domiciliado): no aplica "
                       "detraccion ni retencion de IGV. Revisar aparte renta de no "
                       "domiciliados e IGV por utilizacion de servicios"),
            "Codigo Detraccion": None,
            "Concepto Detraccion": None,
            "Origen Tasa": None,
            "% Aplicado": None,
            "Monto Detraccion/Retencion": 0.0,
            "Neto a Pagar": total,
            "Importe en Soles": en_soles,
        }

    # 0.b El recibo por honorarios es renta de cuarta categoria: nunca lleva
    # detraccion ni retencion del IGV. Antes se le aplicaba el 3% del IGV, que
    # es un impuesto que este comprobante ni siquiera tiene.
    if tipo_documento == RECIBO_HONORARIOS:
        return evaluar_recibo_honorarios(
            total, texto, monto_retencion_renta, en_soles, texto_anexos
        )

    # 0.c Una nota de credito no genera detraccion ni retencion propias: lo que
    # hace es restar del total a pagar al proveedor.
    if tipo_documento == "Nota de Credito":
        return {
            "Afecto a": NO_AFECTO,
            "Motivo": "Nota de credito: resta del total a pagar, sin detraccion ni retencion propia",
            "Codigo Detraccion": None,
            "% Aplicado": None,
            "Monto Detraccion/Retencion": 0.0,
            "Neto a Pagar": total,
            "Importe en Soles": en_soles,
        }

    # 1. La detraccion declarada en el comprobante manda sobre cualquier calculo,
    # pero solo por encima del minimo del SPOT: las facturas de centro comercial
    # llevan la leyenda impresa siempre, tambien en un reembolso de S/ 130.
    if sujeta and en_soles is not None and en_soles <= MINIMO_DETRACCION:
        return {
            "Afecto a": NO_AFECTO,
            "Motivo": (f"Operacion sujeta a detraccion pero de S/ {en_soles:,.2f}: no pasa "
                       f"el minimo de S/ {MINIMO_DETRACCION:,.0f} del SPOT"),
            "Codigo Detraccion": codigo,
            "Concepto Detraccion": None,
            "Origen Tasa": None,
            "% Aplicado": None,
            "Monto Detraccion/Retencion": 0.0,
            "Neto a Pagar": total,
            "Importe en Soles": en_soles,
        }

    if sujeta:
        monto = monto_detraccion
        tasas = parametros.get("tasas") or get_tasas()
        origen = "comprobante" if porcentaje is not None else None
        concepto = tasas.get(codigo, (None, None))[0] if codigo else None

        # Nivel 2: el comprobante trae el codigo pero no la tasa.
        if porcentaje is None and codigo and codigo in tasas:
            porcentaje = tasas[codigo][1]
            origen = "tabla"

        # Nivel 3: no trae ninguno de los dos; se deduce del concepto.
        if porcentaje is None:
            codigo_probable = buscar_codigo_por_concepto(glosa)
            if codigo_probable and codigo_probable in tasas:
                codigo = codigo or codigo_probable
                concepto = tasas[codigo_probable][0]
                porcentaje = tasas[codigo_probable][1]
                origen = "concepto"

        if porcentaje is None and parametros.get("tasa_detraccion_defecto"):
            porcentaje = parametros["tasa_detraccion_defecto"]
            origen = "configuracion"

        por_defecto = origen == "configuracion"

        if monto is None and porcentaje is not None:
            bruto = total * porcentaje / 100
            # El deposito de la detraccion se hace en soles enteros. En una
            # factura en dolares el importe se conserva con sus decimales: el
            # redondeo corresponde al monto en soles del dia del deposito.
            monto = float(round(bruto)) if moneda in (None, "PEN") else round(bruto, 2)

        if monto is None:
            return {
                **vacio,
                "Afecto a": DETRACCION,
                "Motivo": "El comprobante indica detraccion pero no imprime el porcentaje: completar a mano",
                "% Aplicado": None,
            }

        if origen == "tabla":
            motivo = (f"Detraccion sin porcentaje impreso: {porcentaje}% del codigo "
                      f"{codigo} ({concepto}) segun la tabla")
        elif origen == "concepto":
            motivo = (f"Detraccion sin porcentaje ni codigo impresos: {porcentaje}% "
                      f"deducido del concepto ({concepto}). VERIFICAR")
        elif origen == "configuracion":
            motivo = f"Detraccion sin porcentaje impreso: se aplico el {porcentaje}% configurado"
        elif porcentaje is not None:
            motivo = f"El comprobante indica detraccion del {porcentaje}%"
        else:
            motivo = "El comprobante indica detraccion"

        return {
            "Afecto a": DETRACCION,
            "Motivo": motivo,
            "Codigo Detraccion": codigo,
            "Concepto Detraccion": concepto,
            "Origen Tasa": origen,
            "% Aplicado": porcentaje,
            "Monto Detraccion/Retencion": monto,
            "Neto a Pagar": round(total - monto, 2),
            "Importe en Soles": en_soles,
        }

    # 2. Sin detraccion, el umbral se mide en soles.
    if en_soles is None:
        vacio["Motivo"] = f"Factura en {moneda or 'moneda desconocida'} sin tipo de cambio: no se puede comparar con el umbral"
        return vacio

    if en_soles <= umbral:
        return {
            "Afecto a": NO_AFECTO,
            "Motivo": f"Importe de S/ {en_soles:,.2f} menor o igual al umbral de S/ {umbral:,.0f}",
            "Codigo Detraccion": None,
            "% Aplicado": None,
            "Monto Detraccion/Retencion": 0.0,
            "Neto a Pagar": total,
            "Importe en Soles": en_soles,
        }

    # 3. La retencion es del IGV: sin IGV que retener, no corresponde. Pasa con
    # los intereses moratorios y demas operaciones inafectas.
    if igv is not None and igv == 0:
        return {
            "Afecto a": NO_AFECTO,
            "Motivo": "Operacion sin IGV (inafecta o exonerada): no hay retencion que aplicar",
            "Codigo Detraccion": None,
            "% Aplicado": None,
            "Monto Detraccion/Retencion": 0.0,
            "Neto a Pagar": total,
            "Importe en Soles": en_soles,
        }

    # 4. Si el comprobante ya trae la retencion calculada, se respeta: es el
    # proveedor quien sabe si nos toca retenerle y por cuanto. Pero solo si el
    # importe es plausible: al leer un escaneo es facil confundir la retencion
    # con el importe a pagar, que esta justo al lado.
    if monto_retencion and total and not (
        0.005 <= monto_retencion / total <= TASA_RETENCION_MAXIMA / 100
    ):
        monto_retencion = None

    if monto_retencion:
        porcentaje = round(monto_retencion / total * 100, 2) if total else None
        return {
            "Afecto a": RETENCION,
            "Motivo": "El comprobante indica la retencion ya calculada",
            "Codigo Detraccion": None,
            "Concepto Detraccion": None,
            "Origen Tasa": "comprobante",
            "% Aplicado": porcentaje,
            "Monto Detraccion/Retencion": monto_retencion,
            "Neto a Pagar": round(total - monto_retencion, 2),
            "Importe en Soles": en_soles,
        }

    # 5. Pasa el umbral: retencion, salvo que el proveedor este excluido.
    # El recibo de servicios publicos esta fuera del regimen de retencion del
    # IGV (Res. 037-2002/SUNAT). Los recibos de luz, agua y telefono lo llevan
    # impreso al margen, en vertical, donde casi nunca se puede leer.
    if tipo_documento == "Recibo de Servicios":
        return {
            "Afecto a": NO_AFECTO,
            "Motivo": ("Recibo de servicios publicos: fuera del regimen de retencion "
                       "del IGV (Res. 037-2002/SUNAT)"),
            "Codigo Detraccion": None,
            "Concepto Detraccion": None,
            "Origen Tasa": None,
            "% Aplicado": None,
            "Monto Detraccion/Retencion": 0.0,
            "Neto a Pagar": total,
            "Importe en Soles": en_soles,
        }

    excluido = proveedor_sin_retencion(texto)
    if excluido:
        return {
            "Afecto a": NO_AFECTO,
            "Motivo": f"Supera el umbral pero el proveedor figura como {excluido}",
            "Codigo Detraccion": None,
            "% Aplicado": None,
            "Monto Detraccion/Retencion": 0.0,
            "Neto a Pagar": total,
            "Importe en Soles": en_soles,
        }

    monto = round(total * tasa_retencion / 100, 2)
    return {
        "Afecto a": RETENCION,
        "Motivo": f"Supera el umbral de S/ {umbral:,.0f} y el proveedor no esta excluido",
        "Codigo Detraccion": None,
        "% Aplicado": tasa_retencion,
        "Monto Detraccion/Retencion": monto,
        "Neto a Pagar": round(total - monto, 2),
        "Importe en Soles": en_soles,
    }
