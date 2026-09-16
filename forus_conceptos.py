"""Normalizacion de los conceptos de una factura de arriendo.

Cada centro comercial nombra lo mismo de forma distinta: "CONTRAPRESTACION
MINIMA", "RENTA MINIMA LOCAL", "ARRIENDO MINIMO" y "TAM x MT" son todos la
renta minima. Aqui se traducen a un nombre unico para que el control se pueda
sumar y filtrar.

Para anadir un concepto nuevo basta con una linea en NORMALIZACION: no hace
falta tocar el codigo que la usa. El orden importa, porque gana la primera
regla que encaja, y por eso las mas especificas van arriba.
"""
import re

from forus_parsing import collapse_spaces, normalize

# Coletillas que no aportan nada al nombre del gasto y solo estorban al
# agrupar: modalidades de cobro, la unidad, y las palabras de tramite.
RUIDO = [
    r"\bR\s*/\s*FIJA\b",
    r"\bMIN\s*/\s*FIJA\b",
    r"\bV\s*/\s*VARIABLE\b",
    r"\bR\s*/\s*VARIABLE\b",
    r"\bMIN\b(?=\s*$)",
    r"\bLOCALES?\b",
    r"\bREEMBOLSO\s+DE\b",
    r"\bREEMBOLSO\b",
    r"\bCORRESPONDIENTE\s+(?:AL?|DEL)\b",
    r"\bSEG[UÚ]N\s+CONTRATO\b",
    r"\bOBLIGACIONES\s+EMANADAS\b",
    r"\bCON\s+REFERENCIA\s+A\b.*$",
    r"\bDEL?\s*$",
    r"\bAL\s*$",
]

# Trimestres, que se conservan porque distinguen un arbitrio de otro.
ORDINALES = [
    (r"\b(?:PRIMER(?:O|A)?|1ER|1RO|1RA|\bI\b)\s+TRIMESTRE", "PRIMER TRIMESTRE"),
    (r"\b(?:SEGUND[OA]|2DO|2DA|\bII\b)\s+TRIMESTRE", "SEGUNDO TRIMESTRE"),
    (r"\b(?:TERCER(?:O|A)?|3ER|3RO|3RA|\bIII\b)\s+TRIMESTRE", "TERCER TRIMESTRE"),
    (r"\b(?:CUART[OA]|4TO|4TA|\bIV\b)\s+TRIMESTRE", "CUARTO TRIMESTRE"),
]

# Nombre canonico de cada gasto. Gana la primera regla que encaja.
NORMALIZACION = [
    # --- descuentos de notas de credito: van primero para no confundirlos
    #     con el concepto que descuentan ---
    (r"DESCUENTO.*(?:ARRIENDO|RENTA|CONTRAPRESTACION)\s+MINIM", "DESCUENTO RENTA MINIMA"),
    (r"DESCUENTO.*(?:PROMOCION|PUBLICIDAD)", "DESCUENTO FONDO DE PROMOCIONES"),
    (r"DESCUENTO.*GASTO.*COMUN", "DESCUENTO GASTOS COMUNES"),
    (r"\bDESCUENTO\b", "DESCUENTO COMERCIAL"),
    (r"\bNOTA\s+DE\s+CREDITO\b", "NOTA DE CREDITO"),

    # --- arbitrios, con su trimestre si lo trae ---
    (r"ARBITRIO", "ARBITRIOS"),

    # --- renta ---
    (r"(?:CONTRAPRESTACION|RENTA|ARRIENDO|ALQUILER).*VARIABLE", "RENTA VARIABLE"),
    (r"VALOR\s+PORCENTUAL", "RENTA VARIABLE"),
    (r"VARIABLE.*(?:CONTRAPRESTACION|RENTA|ARRIENDO)", "RENTA VARIABLE"),
    (r"(?:CONTRAPRESTACION|RENTA|ARRIENDO|ALQUILER).*MINIM", "RENTA MINIMA"),
    (r"MINIM.*(?:CONTRAPRESTACION|RENTA|ARRIENDO)", "RENTA MINIMA"),
    (r"\bTAM\s*X\s*MT\b", "RENTA MINIMA"),
    (r"\bARRIENDO\b|\bCONTRAPRESTACION\b|\bRENTA\b", "RENTA MINIMA"),

    # --- promocion ---
    (r"(?:FONDO|GASTO|APORTE).*PROMOCION", "FONDO DE PROMOCIONES"),
    (r"PROMOCION", "FONDO DE PROMOCIONES"),
    (r"\bPUBLICIDAD\b|\bMARKETING\b", "FONDO DE PROMOCIONES"),

    # --- gastos del centro comercial ---
    (r"GASTO.*COMUN|COMUN.*GASTO|\bGGCC\b", "GASTOS COMUNES"),
    (r"ADM.*(?:GC|GASTO.*COMUN)", "GASTOS COMUNES"),
    (r"CONTRIBUCION", "CONTRIBUCIONES"),
    (r"AGUA\s+HELADA", "AGUA HELADA"),
    (r"\bENERGIA\b|\bELECTRICIDAD\b|\bLUZ\b", "ENERGIA"),
    (r"ELECTRIFICACION\s+RURAL", "ELECTRIFICACION RURAL"),
    (r"\bAGUA\b|CONSUMO\s+DE\s+AGUA", "AGUA"),
    (r"MANTENIMIENTO|CONSERVACION", "MANTENIMIENTO"),
    (r"VIGILANCIA|SEGURIDAD", "VIGILANCIA"),
    (r"LIMPIEZA", "LIMPIEZA"),
    (r"CLIMATIZACION|AIRE\s+ACONDIC", "AIRE ACONDICIONADO"),
    (r"SERVICIOS\s+COMERCIALES", "SERVICIOS COMERCIALES"),
    (r"ESTACIONAMIENTO|PLAYA\s+DE\s+ESTACIONAMIENTO", "ESTACIONAMIENTO"),

    # --- gastos de administracion del centro comercial ---
    (r"GASTO.*ADMINISTRA|ADMINISTRACION\s+DEL?\s+CENTRO", "GASTOS ADMINISTRATIVOS"),

    # --- financieros ---
    (r"INTERES.*PUNITORIO|PUNITORIO", "INTERESES"),
    (r"INTERES.*MORATORIO|MORA\b|PAGO\s+TARDIO", "INTERESES MORATORIOS"),
    (r"\bPENALIDAD\b", "PENALIDAD"),
    (r"\bREAJUSTE\b|INFLACION|\bIPC\b", "REAJUSTE"),
]


# Cada emisor tiene su manera de llamar a lo mismo, y a veces choca con el
# nombre general. Plaza San Miguel factura los gastos comunes del local bajo el
# titulo "Mantenimiento": en su factura eso no es el mantenimiento del local,
# es el gasto comun, y asi tiene que entrar al control.
#
# PARA ANADIR UNA EXCEPCION: el RUC del emisor y la lista de (patron, nombre).
# Se revisan antes que la tabla general y gana la primera que encaja.
REGLAS_POR_EMISOR = {
    "20113126087": [  # MALL SERVICE S.A.C. - C.C. Plaza San Miguel
        (r"MANTENIMIENTO", "GASTOS COMUNES"),
    ],
}


def _reglas_del_emisor(ruc):
    if not ruc:
        return []
    digitos = "".join(caracter for caracter in str(ruc) if caracter.isdigit())
    return REGLAS_POR_EMISOR.get(digitos, [])


def _limpiar(texto):
    """Quita periodos, coletillas y adornos, dejando el nombre del gasto."""
    plano = normalize(texto or "")

    # Fuera los rangos de fechas y las fechas sueltas.
    plano = re.sub(r"\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}", " ", plano)
    plano = re.sub(r"\b\d{4}\s*[-–]\s*\d{4}\b", " ", plano)
    # Fuera los porcentajes y referencias tipo %GPV:1.00 - VTAS:487763.61
    plano = re.sub(r"%\s*[A-Z]{2,6}\s*:\s*[\d.,]+", " ", plano)
    plano = re.sub(r"\b[A-Z]{3,6}\s*:\s*[\d.,]+", " ", plano)

    for patron in RUIDO:
        plano = re.sub(patron, " ", plano)

    return collapse_spaces(plano) or ""


# El sufijo distingue "RENTA MINIMA" de "RENTA MINIMA ALMACEN", que en el
# control de Contabilidad son dos lineas distintas.
SUFIJOS = [(r"\bALMACEN\b|\bDEPOSITO\b", "ALMACEN")]


def normalizar_concepto(texto, ruc=None):
    """Nombre canonico del gasto, o None si no se reconoce.

    Devuelve None a proposito cuando no encaja en ninguna regla: es preferible
    dejarlo en blanco y marcarlo para revision que quedarse con un texto que no
    se puede agrupar.
    """
    limpio = _limpiar(texto)
    if not limpio:
        return None

    # Lo que este emisor llama distinto al resto manda sobre la tabla general.
    for patron, canonico in _reglas_del_emisor(ruc):
        if re.search(patron, limpio):
            return canonico

    trimestre = None
    for patron, etiqueta in ORDINALES:
        if re.search(patron, limpio):
            trimestre = etiqueta
            break

    sufijo = None
    for patron, etiqueta in SUFIJOS:
        if re.search(patron, limpio):
            sufijo = etiqueta
            break

    for patron, canonico in NORMALIZACION:
        if re.search(patron, limpio):
            if canonico == "ARBITRIOS" and trimestre:
                return f"ARBITRIOS {trimestre}"
            if sufijo:
                return f"{canonico} {sufijo}"
            return canonico

    return None


def concepto_o_original(texto, ruc=None, respaldo=None):
    """El nombre canonico y, si no lo hay, la descripcion limpia.

    Devuelve (concepto, reconocido) para que quien llame pueda marcar la fila
    cuando el concepto no se pudo normalizar. `respaldo` es la continuacion de
    la linea: algunas facturas parten la descripcion en dos y el nombre del
    gasto esta en la segunda mitad.
    """
    canonico = normalizar_concepto(texto, ruc=ruc)
    if canonico:
        return canonico, True

    if respaldo:
        canonico = normalizar_concepto(f"{texto} {respaldo}", ruc=ruc)
        if canonico:
            return canonico, True

    limpio = _limpiar(texto)
    return (limpio or collapse_spaces(texto)), False
