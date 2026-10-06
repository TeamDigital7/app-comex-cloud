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

from forus_parsing import find_money, normalize, parse_amount

UMBRAL_DEFECTO = 700.0
TASA_RETENCION_DEFECTO = 3.0

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
    ("019", ("ARRENDAMIENTO", "ARRIENDO", "ALQUILER", "RENTA MINIMA", "LOCAL COMERCIAL")),
    ("027", ("TRANSPORTE DE BIENES", "FLETE", "TRANSPORTE DE CARGA")),
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
NO_AFECTO = "NO AFECTO"
REVISAR = "REVISAR"

SENALES_DETRACCION = (
    "DETRACCION",
    "SPOT",
    "SISTEMA DE PAGO DE OBLIGACIONES TRIBUTARIAS",
    "DECRETO LEGISLATIVO 940",
    "D.L. 940",
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
# "% Detracción: 12.00" (Bizlinks) y "TASA 12%" en el bloque de comentarios.
PORCENTAJE_DETRACCION_OTROS_RE = [
    re.compile(r"%\s*DETRACCI[OÓ]N\s*[:.]?\s*(\d{1,2}(?:[.,]\d{1,2})?)", re.I),
    re.compile(r"^\s*TASA\s*(?:DE\s+DETRACCI[OÓ]N)?\s*[:.]?\s*(\d{1,2}(?:[.,]\d{1,2})?)\s*%", re.I | re.M),
]
PORCENTAJE_SUELTO_RE = re.compile(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%")
# El codigo identifica el bien o servicio del Anexo que fija la tasa. Cada
# proveedor lo rotula a su manera: "Codigo Detraccion: 019" o, en la
# representacion impresa de SUNAT, "Bien o Servicio: 021 Movimiento de carga".
CODIGO_DETRACCION_RE = re.compile(
    r"(?:C[OÓ]DIGO\s+(?:DE\s+)?DETRACCI[OÓ]N|BIEN\s+O\s+SERVICIO"
    r"|C[OÓ]DIGO\s+(?:DE\s+)?BIEN\s+O\s+SERVICIO)\s*[:.]?\s*(\d{3})\b",
    re.I,
)
# "CODIGO 037" en una linea sola, dentro del bloque de la detraccion.
CODIGO_SUELTO_RE = re.compile(r"^\s*C[OÓ]DIGO\s*[:.]?\s*(\d{3})\s*$", re.I | re.M)

# Tasas que existen en las tablas de SUNAT. Sirven para reconocer la tasa
# cuando el comprobante imprime el monto pero no el porcentaje.
TASAS_CONOCIDAS = (1.5, 4.0, 10.0, 12.0, 15.0)

# Diferencia admitida entre el monto de detraccion impreso y el que sale de
# aplicar la tasa al total. El deposito se redondea a soles enteros, asi que
# por debajo de un sol es solo redondeo.
TOLERANCIA_MONTO_DETRACCION = 1.0


def tasa_desde_monto(monto, total):
    """Tasa que explica el monto impreso, si coincide con una tasa real."""
    if not monto or not total:
        return None
    for conocida in TASAS_CONOCIDAS:
        if abs(abs(total) * conocida / 100 - abs(monto)) <= TOLERANCIA_MONTO_DETRACCION:
            return conocida
    return None


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
LEYENDAS_SPOT = (
    "SPOT",
    "SISTEMA DE PAGO DE OBLIGACIONES TRIBUTARIAS",
    "DECRETO LEGISLATIVO 940",
    "D.L. 940",
    "SUJETA A DETRACCION",
    "SUJETO A DETRACCION",
    "AFECTO A LA DETRACCION",
    "PORCENTAJE DETRACCION",
    "PORCENTAJE DE DETRACCION",
    "CODIGO DETRACCION",
)


def detectar_detraccion(texto):
    """(sujeta, porcentaje, codigo) segun lo que declare el propio comprobante."""
    plano = normalize(texto or "")
    sujeta = any(senal in plano for senal in SENALES_DETRACCION)
    if not sujeta:
        return False, None, None

    # Muchas facturas listan "Detraccion: S/ 0.00" en el pie de totales aunque
    # la operacion no este sujeta. Con importe cero y sin ninguna leyenda del
    # SPOT, esa palabra no significa que haya detraccion.
    if not any(leyenda in plano for leyenda in LEYENDAS_SPOT):
        importe = find_money(texto, ["DETRACCION"])
        if importe is not None and importe == 0:
            return False, None, None

    # Sobre el texto normalizado: si no, "Código" con tilde no casa nunca.
    codigo = None
    match_codigo = CODIGO_DETRACCION_RE.search(normalize(texto or ""))
    if not match_codigo:
        match_codigo = CODIGO_SUELTO_RE.search(normalize(texto or ""))
    if match_codigo:
        codigo = match_codigo.group(1)

    porcentaje = None
    match = PORCENTAJE_DETRACCION_RE.search(texto or "")
    if match:
        porcentaje = parse_amount(match.group(1))

    if porcentaje is None:
        match = PORCENTAJE_DETRACCION_SIN_SIMBOLO_RE.search(texto or "")
        if match:
            porcentaje = parse_amount(match.group(1))

    if porcentaje is None:
        for patron in PORCENTAJE_DETRACCION_OTROS_RE:
            match = patron.search(texto or "")
            if match:
                porcentaje = parse_amount(match.group(1))
                break

    if porcentaje is not None and not (1 <= porcentaje <= 15):
        porcentaje = None  # se leyo otro numero

    if porcentaje is None:
        # Algunos lo imprimen suelto, en la misma linea de la cuenta del Banco
        # de la Nacion. Solo se acepta si es una tasa de detraccion plausible.
        for linea in (texto or "").splitlines():
            if not any(senal in normalize(linea) for senal in SENALES_DETRACCION):
                continue
            for candidato in PORCENTAJE_SUELTO_RE.findall(linea):
                valor = parse_amount(candidato)
                if valor is not None and 1 <= valor <= 15:
                    porcentaje = valor
                    break
            if porcentaje is not None:
                break

    return True, porcentaje, codigo


def proveedor_sin_retencion(texto):
    """El comprobante declara una condicion que excluye la retencion."""
    plano = normalize(texto or "")
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


def evaluar(total, moneda, tipo_cambio, texto, monto_detraccion=None, parametros=None,
            igv=None, tipo_documento=None, glosa=None, monto_retencion=None):
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

    # 0. Una nota de credito no genera detraccion ni retencion propias: lo que
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

    # 1. La detraccion declarada en el comprobante manda sobre cualquier calculo.
    if sujeta:
        monto = monto_detraccion
        tasas = parametros.get("tasas") or get_tasas()
        origen = "comprobante" if porcentaje is not None else None
        concepto = tasas.get(codigo, (None, None))[0] if codigo else None

        # Nivel 2: trae el monto pero no la tasa; la tasa es la que explica
        # ese monto.
        if porcentaje is None and monto:
            deducida = tasa_desde_monto(monto, total)
            if deducida is not None:
                porcentaje = deducida
                origen = "monto impreso"

        # Nivel 3: el comprobante trae el codigo pero no la tasa.
        if porcentaje is None and codigo and codigo in tasas:
            porcentaje = tasas[codigo][1]
            origen = "tabla"

        # Nivel 4: no trae ninguno de los dos; se deduce del concepto.
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

        calculado = None
        if porcentaje is not None:
            bruto = abs(total) * porcentaje / 100
            # El deposito de la detraccion se hace en soles enteros. En una
            # factura en dolares el importe se conserva con sus decimales: el
            # redondeo corresponde al monto en soles del dia del deposito.
            calculado = float(round(bruto)) if moneda in (None, "PEN") else round(bruto, 2)

        # Control del monto impreso: el proveedor puede equivocarse (se han
        # visto facturas con "12%" y un monto que no es el 12% del total). Lo
        # que hay que depositar es la tasa sobre el total, asi que se usa el
        # calculado y la fila queda marcada para que se vea.
        alerta = None
        if monto is not None and calculado is not None and origen in ("comprobante", "tabla"):
            if abs(abs(monto) - calculado) > TOLERANCIA_MONTO_DETRACCION:
                if origen == "comprobante":
                    alerta = (
                        f"REVISAR detraccion: el comprobante imprime {monto:,.2f} pero el "
                        f"{porcentaje:g}% de {total:,.2f} es {calculado:,.2f}; se uso el calculado"
                    )
                    monto = calculado
                else:
                    # La tasa de la tabla esta por validar: no se impone al
                    # monto del comprobante, solo se avisa.
                    alerta = (
                        f"REVISAR detraccion: el comprobante imprime {monto:,.2f} y la tabla "
                        f"({porcentaje:g}% del codigo {codigo}) daria {calculado:,.2f}"
                    )

        if monto is None:
            monto = calculado

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
        elif origen == "monto impreso":
            motivo = (f"El comprobante indica detraccion de {monto:,.2f} sin imprimir el "
                      f"porcentaje: corresponde al {porcentaje:g}% del total")
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
            "Alerta Tributaria": alerta,
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
