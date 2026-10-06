"""Utilidades de parseo compartidas por Comex, Contabilidad y Recursos Humanos."""
import re


def parse_money(value):
    if value is None:
        return None

    text = str(value).strip().replace(" ", "")
    if not text:
        return None

    is_negative = text.endswith("-")
    if is_negative:
        text = text[:-1]

    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "")

    try:
        number = float(text)
    except ValueError:
        return None

    return -number if is_negative else number


def parse_quantity(value):
    if value is None:
        return None

    text = str(value).strip().replace(" ", "")
    if not text:
        return None

    # En cantidades, 1.300 significa 1300.
    text = text.replace(".", "").replace(",", "")

    try:
        return int(text)
    except ValueError:
        return None

def clean_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None

def clean_composition_text(value):
    text = clean_text(value)
    if not text:
        return None
    text = re.sub(r"\s+\bFOOTWEAR\b\s*$", "", text, flags=re.I)
    text = re.sub(r"\bMade\s+in\s*:?\s*[A-Za-z ]+", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" :-;|")
    return text or None


def split_lines(value):
    if value is None:
        return []
    return [line.strip() for line in str(value).splitlines() if line.strip()]


# Algunos PDFs devuelven el texto con la codificacion cruzada ("PÃ¡g." en vez de
# "Pág."). Se corrige antes de buscar etiquetas para que los patrones acierten.
def fix_mojibake(text):
    if not text or "Ã" not in text:
        return text or ""
    try:
        return text.encode("latin-1", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


# Tabla 1 a 1: quita tildes sin alterar la longitud, de modo que las posiciones
# del texto normalizado siguen siendo validas sobre el texto original.
TILDES = str.maketrans(
    "áéíóúÁÉÍÓÚàèìòùÀÈÌÒÙäëïöüÄËÏÖÜñÑ",
    "aeiouAEIOUaeiouAEIOUaeiouAEIOUnN",
)


def normalize(text):
    """Mayusculas y sin tildes, conservando la longitud del texto original."""
    if not text:
        return ""
    return text.translate(TILDES).upper()


def collapse_spaces(text):
    if text is None:
        return None
    limpio = re.sub(r"\s+", " ", str(text)).strip()
    return limpio or None


def parse_amount(value):
    """Importe con separadores mixtos, como se usa en documentos peruanos.

    A diferencia de parse_money (heredado de Comex, que asume el punto como
    separador de miles), aqui se decide cual es el separador decimal mirando
    cual aparece de ultimo: "1,234.56" y "1.234,56" dan los dos 1234.56.
    Acepta el negativo delante, detras o entre parentesis.
    """
    if value is None:
        return None

    texto = str(value).strip().replace(" ", "").replace(" ", "")
    if not texto:
        return None

    negativo = texto.startswith("-") or texto.endswith("-") or (
        texto.startswith("(") and texto.endswith(")")
    )

    texto = re.sub(r"[^\d.,]", "", texto)
    if not texto or not re.search(r"\d", texto):
        return None

    ultima_coma = texto.rfind(",")
    ultimo_punto = texto.rfind(".")

    if ultima_coma > ultimo_punto:
        decimal, miles = ",", "."
    elif ultimo_punto > ultima_coma:
        decimal, miles = ".", ","
    else:
        decimal, miles = None, None

    if decimal:
        decimales = len(texto) - texto.rfind(decimal) - 1
        if decimales == 3 and texto.count(decimal) == 1 and miles not in texto:
            # "1.234" o "1,234": tres cifras detras es separador de miles.
            texto = texto.replace(decimal, "")
        elif texto.count(decimal) > 1 and decimales == 3 and miles not in texto:
            # "1.000.000": todos son separadores de miles.
            texto = texto.replace(decimal, "")
        elif texto.count(decimal) > 1 and decimales == 2 and miles not in texto:
            # "1.004.48": el OCR usa el mismo signo para miles y decimales.
            entero, _, centimos = texto.rpartition(decimal)
            texto = entero.replace(decimal, "") + "." + centimos
        else:
            texto = texto.replace(miles, "").replace(decimal, ".")

    try:
        numero = float(texto)
    except ValueError:
        return None

    return -numero if negativo else numero


# Red de seguridad: si un PDF pierde las tildes al extraer el texto, las deja
# como caracter de reemplazo ("SE?OR(ES)", "DESCRIPCI?N") y ya no se pueden
# recuperar. Por eso las vocales y la ene de cada etiqueta se buscan
# admitiendolo como alternativa. Las facturas revisadas hasta ahora extraen
# bien las tildes; esto solo cubre a los proveedores que no lo hagan.
REEMPLAZO = "�"

COMODIN_ACENTO = {letra: f"[{letra}{REEMPLAZO}]" for letra in "AEIOUN"}


def _patron_etiqueta(label):
    partes = []
    for caracter in normalize(label):
        if caracter in COMODIN_ACENTO:
            partes.append(COMODIN_ACENTO[caracter])
        elif caracter == " ":
            partes.append(r"\s+")
        else:
            partes.append(re.escape(caracter))
    return "".join(partes)


def find_label_value(text, labels, max_chars=90, requiere_separador=False):
    """Devuelve lo que sigue a la primera etiqueta encontrada, en la misma linea.

    `labels` es una lista de alias en orden de preferencia. La busqueda ignora
    tildes, mayusculas y tildes perdidas; el valor se devuelve tal como aparece
    en el original.

    Con `requiere_separador` la etiqueta solo cuenta si va seguida de dos puntos.
    Hace falta para palabras que tambien aparecen dentro del texto corriente:
    sin esto, "LOCAL" acierta dentro de "RENTA MINIMA LOCAL DEL 01/07/2026".
    """
    if not text:
        return None

    for valor in iter_label_values(text, labels, max_chars, requiere_separador):
        return valor
    return None


def iter_label_values(text, labels, max_chars=90, requiere_separador=False):
    """Todos los valores que siguen a las etiquetas, alias por alias.

    Hace falta porque una etiqueta suele aparecer varias veces: "TOTAL" esta
    primero en el encabezado de la tabla, donde no hay ningun importe detras, y
    solo mas abajo en la fila que interesa. Quedarse con la primera aparicion
    hace perder el dato.
    """
    if not text:
        return

    separador = r"\s*[:=]\s*" if requiere_separador else r"\s*[:.\-]?\s*"
    plano = normalize(text)

    for label in labels:
        for match in re.finditer(_patron_etiqueta(label) + separador, plano):
            inicio = match.end()
            resto = text[inicio:inicio + max_chars]
            valor = collapse_spaces(resto.split("\n", 1)[0])
            if valor:
                yield valor


PORCENTAJE_RE = re.compile(r"\(?\s*\d+(?:[.,]\d+)?\s*%\s*\)?")


def find_money(text, labels):
    """Primer importe que aparece despues de alguna de las etiquetas.

    Descarta los porcentajes para que "IGV (18%): 180.00" devuelva 180.00 y
    no 18, que es el error tipico al leer estas cabeceras.
    """
    for crudo in iter_label_values(text, labels):
        limpio = PORCENTAJE_RE.sub(" ", crudo)
        limpio = re.sub(r"(?i)(?:S/\.?|US\$|\$)", " ", limpio)
        limpio = re.sub(r"(?i)\b(?:USD|PEN|EUR|SOLES|DOLARES)\b", " ", limpio)
        match = re.search(r"-?\(?\d[\d.,]*\)?-?", limpio)
        if match:
            valor = parse_amount(match.group(0))
            if valor is not None:
                return valor
    return None


def find_percent(text, labels):
    """Porcentaje asociado a una etiqueta, por ejemplo la tasa de detraccion."""
    crudo = find_label_value(text, labels)
    if not crudo:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", crudo)
    return parse_amount(match.group(1)) if match else None


MESES = {
    "ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5, "JUNIO": 6,
    "JULIO": 7, "AGOSTO": 8, "SETIEMBRE": 9, "SEPTIEMBRE": 9, "OCTUBRE": 10,
    "NOVIEMBRE": 11, "DICIEMBRE": 12,
}


MESES_ABREVIADOS = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6, "JUL": 7,
    "AGO": 8, "SET": 9, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}

# Cada formato devuelve (dia, mes, anio). Se prueban todos y gana el que
# aparece primero en el texto: con "07-09-2026 Fecha vencimiento: 2026-09-17"
# la fecha que vale es la primera, no la que tiene el formato preferido.
FORMATOS_FECHA = [
    (re.compile(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b"),
     lambda g: (g[2], g[1], g[0])),
    (re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4}|\d{2})\b"),
     lambda g: (g[0], g[1], g[2])),
    (re.compile(r"\b(\d{1,2})\s+DE\s+([A-Z]+)\s+DE\s+(\d{4})\b"),
     lambda g: (g[0], MESES.get(g[1]), g[2])),
    # "07-sep-2026", "07/SET/2026", "07 SEP. 2026".
    (re.compile(r"\b(\d{1,2})[\s/\-.]+(" + "|".join(MESES_ABREVIADOS) + r")[A-Z]*\.?[\s/\-.]+(\d{4})\b"),
     lambda g: (g[0], MESES_ABREVIADOS.get(g[1]), g[2])),
]


def parse_date(value):
    """Normaliza a AAAA-MM-DD las fechas mas comunes en documentos peruanos."""
    if not value:
        return None

    texto = normalize(collapse_spaces(str(value)) or "")
    if not texto:
        return None

    candidatos = []
    for patron, partes in FORMATOS_FECHA:
        for match in patron.finditer(texto):
            dia, mes, anio = partes(match.groups())
            try:
                dia, mes, anio = int(dia), int(mes), int(anio)
            except (TypeError, ValueError):
                continue
            if anio < 100:
                anio += 2000
            if 1 <= mes <= 12 and 1 <= dia <= 31 and 1990 <= anio <= 2100:
                candidatos.append((match.start(), f"{anio:04d}-{mes:02d}-{dia:02d}"))
                break

    if not candidatos:
        return None
    return min(candidatos)[1]


def find_date(text, labels, linea_siguiente=False):
    """Primera fecha valida que acompana a alguna de las etiquetas.

    Con `linea_siguiente`, si la etiqueta no lleva la fecha en su misma linea
    -cabeceras en forma de tabla, con todos los rotulos en una fila y los
    valores en la siguiente- se toma la primera fecha de la linea de abajo.
    Solo sirve para la etiqueta que va primero en esa fila (la de emision).
    """
    if not text:
        return None

    lineas = text.split("\n")
    plano = normalize(text)
    for label in labels:
        for match in re.finditer(_patron_etiqueta(label) + r"\s*[:.\-]?\s*", plano):
            resto = text[match.end():match.end() + 40].split("\n", 1)[0]
            fecha = parse_date(resto)
            if fecha:
                return fecha
            if not linea_siguiente or re.search(r"\d", resto):
                continue
            numero_linea = text.count("\n", 0, match.end())
            if numero_linea + 1 < len(lineas):
                fecha = parse_date(lineas[numero_linea + 1][:40])
                if fecha:
                    return fecha
    return None


# ---------------------------------------------------------------------------
# Importe en letras ("SON: UN MIL CUATROCIENTOS SETENTA Y CINCO Y 00/100 SOLES").
# Toda factura peruana lo trae, y es el control mas fiable del importe total:
# no depende de como el PDF arme la tabla de totales ni de que el OCR ponga
# cada cifra en su sitio.
# ---------------------------------------------------------------------------
_UNIDADES = {
    "CERO": 0, "UN": 1, "UNO": 1, "UNA": 1, "DOS": 2, "TRES": 3, "CUATRO": 4,
    "CINCO": 5, "SEIS": 6, "SIETE": 7, "OCHO": 8, "NUEVE": 9, "DIEZ": 10,
    "ONCE": 11, "DOCE": 12, "TRECE": 13, "CATORCE": 14, "QUINCE": 15,
    "DIECISEIS": 16, "DIECISIETE": 17, "DIECIOCHO": 18, "DIECINUEVE": 19,
    "VEINTE": 20, "VEINTIUN": 21, "VEINTIUNO": 21, "VEINTIDOS": 22,
    "VEINTITRES": 23, "VEINTICUATRO": 24, "VEINTICINCO": 25, "VEINTISEIS": 26,
    "VEINTISIETE": 27, "VEINTIOCHO": 28, "VEINTINUEVE": 29, "TREINTA": 30,
    "CUARENTA": 40, "CINCUENTA": 50, "SESENTA": 60, "SETENTA": 70,
    "OCHENTA": 80, "NOVENTA": 90, "CIEN": 100, "CIENTO": 100,
    "DOSCIENTOS": 200, "TRESCIENTOS": 300, "CUATROCIENTOS": 400,
    "QUINIENTOS": 500, "SEISCIENTOS": 600, "SETECIENTOS": 700,
    "SETESCIENTOS": 700, "OCHOCIENTOS": 800, "NOVECIENTOS": 900,
    "NOVESCIENTOS": 900,
}

IMPORTE_LETRAS_RE = re.compile(
    r"\bSON\s*[:.]?\s*([A-Z\s]+?)\s+(?:Y|CON)\s+(\d{1,2})\s*/\s*100"
)


def _entero_en_letras(palabras):
    total = 0
    actual = 0
    for palabra in palabras:
        if palabra == "Y":
            continue
        if palabra in _UNIDADES:
            actual += _UNIDADES[palabra]
        elif palabra == "MIL":
            total += (actual or 1) * 1000
            actual = 0
        elif palabra in ("MILLON", "MILLONES"):
            total = (total + (actual or 1)) * 1_000_000
            actual = 0
        else:
            return None  # palabra que no es un numero: mejor no adivinar
    return total + actual


def importe_en_letras(text):
    """El importe escrito en letras, como numero. None si no se puede leer."""
    match = IMPORTE_LETRAS_RE.search(normalize(text or ""))
    if not match:
        return None
    palabras = match.group(1).split()
    if not palabras:
        return None
    entero = _entero_en_letras(palabras)
    if entero is None:
        return None
    return round(entero + int(match.group(2)) / 100, 2)


def importes_del_texto(text):
    """Todos los importes con decimales que aparecen escritos en el texto."""
    valores = set()
    for crudo in re.findall(r"\d[\d.,]*[.,]\d{2}\b", text or ""):
        valor = parse_amount(crudo)
        if valor is not None:
            valores.add(round(valor, 2))
    return valores


def find_ruc(text, labels=None):
    """RUC peruano: 11 digitos que empiezan en 10, 15, 16, 17 o 20."""
    patron = r"\b((?:10|15|16|17|20)\d{9})\b"

    if labels:
        crudo = find_label_value(text, labels)
        if crudo:
            match = re.search(patron, crudo.replace("-", "").replace(" ", ""))
            if match:
                return match.group(1)

    match = re.search(patron, text or "")
    return match.group(1) if match else None


def detect_currency(text):
    """Moneda del documento a partir de las menciones mas frecuentes."""
    plano = normalize(text)
    if re.search(r"\bD[OÓ]LAR", plano) or "USD" in plano or "US$" in plano:
        return "USD"
    if "EUR" in plano:
        return "EUR"
    if "SOLES" in plano or "PEN" in plano or "S/" in (text or ""):
        return "PEN"
    return None


def cuadra(esperado, obtenido, tolerancia=0.05):
    """Compara dos importes. Devuelve 'Si', 'No' o None si falta alguno."""
    if esperado is None or obtenido is None:
        return None
    return "Si" if abs(float(esperado) - float(obtenido)) <= tolerancia else "No"


def faltantes(fila, campos):
    """Lista los campos obligatorios que quedaron vacios en una fila."""
    vacios = [campo for campo in campos if fila.get(campo) in (None, "")]
    return ", ".join(vacios) if vacios else None


TOTAL_RE = re.compile(r"(?i)^.*\b(?:IMPORTE\s+TOTAL|TOTAL\s+A\s+PAGAR|TOTAL)\b.*$", re.M)


def detect_currency_document(text):
    """Moneda del comprobante, mirando primero donde es fiable.

    El texto suele mencionar las dos monedas (por las cuentas bancarias del pie),
    asi que se empieza por la linea del importe total y solo al final se mira
    el documento entero.
    """
    if not text:
        return None

    for linea in TOTAL_RE.findall(text):
        if re.search(r"US\s*\$|\bUSD\b", linea, flags=re.I):
            return "USD"
        if re.search(r"S/|\bPEN\b|\bSOLES\b", linea, flags=re.I):
            return "PEN"

    etiqueta = find_label_value(text, ["TIPO DE MONEDA", "MONEDA"], max_chars=30)
    if etiqueta:
        plano = normalize(etiqueta)
        if plano.startswith(("DOLAR", "USD", "US$")):
            return "USD"
        if plano.startswith(("SOL", "PEN", "NUEVO SOL")):
            return "PEN"

    for linea in split_lines(text):
        if len(linea) <= 20:
            if re.fullmatch(r"(?i)\s*PEN\s*", linea):
                return "PEN"
            if re.fullmatch(r"(?i)\s*USD\s*", linea):
                return "USD"

    return detect_currency(text)


# Sufijos de razon social peruana, tolerando puntos y espacios sueltos. No se
# exige que cierren la linea: hay proveedores que imprimen el nombre y el
# numero de comprobante juntos ("STRIP CENTERS DEL PERU S.A.C. F001 N 00002037"),
# y en ese caso el nombre termina donde termina el sufijo.
SUFIJO_SOCIETARIO_RE = re.compile(
    r"(?i)\b(?:S\.?\s*A\.?\s*C\.?|S\.?\s*A\.?\s*A\.?|S\.?\s*R\.?\s*L\.?"
    r"|E\.?\s*I\.?\s*R\.?\s*L\.?|S\.?\s*A\.?|SAC|SRL|SAA|EIRL)(?:\s|$)"
)

TITULOS_DOCUMENTO = (
    "FACTURA", "BOLETA", "NOTA DE", "RECIBO", "REPRESENTACION", "R.U.C", "RUC",
    "SENOR", "SE" + REEMPLAZO + "OR", "DIRECCION", "TELEFONO", "TELF", "AV.", "CAL.",
)


def find_razon_social_emisor(text, excluir=()):
    """Razon social del proveedor: la primera linea que cierra con forma societaria.

    Si la linea anterior es parte del nombre (pasa cuando el RUC lo parte en
    dos), se une. Las lineas de `excluir` -por ejemplo el nombre de la propia
    empresa- se saltan para no confundir al emisor con el cliente.
    """
    lineas = split_lines(text)[:15]
    candidatos = []
    for indice in range(len(lineas)):
        nombre = _razon_social_en_linea(lineas, indice, excluir)
        if nombre:
            candidatos.append(nombre)

    if not candidatos:
        return None

    # Muchos proveedores imprimen arriba el nombre comercial con forma
    # societaria ("AUTOMOTRIZ J.P EIRL") y debajo la razon social completa
    # ("CONTRATISTA GENERALES AUTOMOTRIZ J.P. E.I.R.L."). Si una contiene a la
    # otra, vale la completa.
    elegido = candidatos[0]
    clave = _clave_nombre(elegido)
    for otro in candidatos[1:]:
        clave_otro = _clave_nombre(otro)
        if len(clave_otro) > len(clave) and clave_otro.endswith(clave):
            elegido, clave = otro, clave_otro
    return elegido


def _clave_nombre(nombre):
    return re.sub(r"[^A-Z0-9]", "", normalize(nombre or ""))


# Lo que precede a la razon social en la misma linea y no es parte de ella:
# sellos de recepcion ("39655/28806") y restos del logo.
PREFIJO_RUIDO_RE = re.compile(r"^(?:[\d/\-.:,]+\s+)+")


def _limpiar_prefijo(nombre):
    nombre = PREFIJO_RUIDO_RE.sub("", nombre or "").strip()
    # El OCR suele leer el logo pegado al nombre: "IBDO BDO OUTSOURCING".
    palabras = nombre.split()
    if len(palabras) >= 3 and len(palabras[0]) == len(palabras[1]) + 1 and palabras[0].endswith(palabras[1]):
        nombre = " ".join(palabras[1:])
    return nombre


# Rotulos de la linea del cliente: esa tambien lleva RUC y forma societaria.
ROTULOS_CLIENTE = (
    "CLIENTE", "ADQUIRIENTE", "SENOR", "SE" + REEMPLAZO + "OR", "RAZON SOCIAL",
    "DENOMINACION", "NOMBRE",
)


def _razon_social_en_linea(lineas, indice, excluir=()):
    """Razon social con forma societaria en la linea `indice`, o None.

    Si la linea anterior es parte del nombre (pasa cuando el RUC lo parte en
    dos), se une.
    """
    excluir_plano = [normalize(termino) for termino in excluir]
    linea = lineas[indice]

    # "TAILOY S.A. R.U.C. Nro 20100049181": el nombre va antes del RUC. Antes
    # esta linea se descartaba entera por llevar el RUC y se acababa tomando
    # como proveedor el nombre del vendedor.
    corte = re.search(r"(?i)\bR\.?\s*U\.?\s*C\b", linea)
    if corte and corte.start() >= 6:
        linea = linea[:corte.start()]

    limpia = collapse_spaces(linea)
    if not limpia or len(limpia) < 6 or len(limpia) > 90:
        return None

    plano = normalize(limpia)
    if any(termino in plano for termino in excluir_plano):
        return None
    if any(plano.startswith(titulo) for titulo in TITULOS_DOCUMENTO):
        return None
    if any(rotulo in plano for rotulo in ROTULOS_CLIENTE):
        return None
    if re.search(r"\b(?:10|15|16|17|20)\d{9}\b", plano):
        return None

    sufijo = SUFIJO_SOCIETARIO_RE.search(limpia)
    if not sufijo:
        return None
    limpia = _limpiar_prefijo(collapse_spaces(limpia[:sufijo.end()]))
    if not limpia or len(re.findall(r"[A-Za-z]", limpia)) < 4:
        return None

    # El RUC suele meterse entre las dos lineas del nombre, asi que se mira
    # hacia arriba saltando la linea del propio RUC.
    for salto in (1, 2):
        if indice - salto < 0:
            break

        previa = collapse_spaces(lineas[indice - salto]) or ""
        plano_previa = normalize(previa)

        if plano_previa.startswith(("R.U.C", "RUC")) or re.search(
            r"\b(?:10|15|16|17|20)\d{9}\b", plano_previa
        ):
            continue

        # Solo se une cuando lo que separa las dos mitades del nombre es la
        # linea del RUC. Si la linea de arriba es adyacente, no forma parte
        # del nombre: suele ser el centro comercial ("INOUTLET FAUCETT").
        es_continuacion = (
            salto == 2
            and 6 <= len(previa) <= 60
            and not any(plano_previa.startswith(t) for t in TITULOS_DOCUMENTO)
            and not re.search(r"\d", previa)
            and not any(termino in plano_previa for termino in excluir_plano)
        )
        if es_continuacion:
            return collapse_spaces(f"{previa} {limpia}")
        break

    return limpia


NOMBRES_MES = [
    "ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO",
    "JULIO", "AGOSTO", "SETIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE",
]

PERIODO_RE = re.compile(
    r"(\d{1,2}[./\-]\d{1,2}[./\-]\d{4})\s*(?:AL?|HASTA|[-–])\s*(\d{1,2}[./\-]\d{1,2}[./\-]\d{4})",
    re.I,
)


def extraer_periodo(texto):
    """Rango de fechas dentro de una descripcion: '01.06.2026 - 30.06.2026'."""
    if not texto:
        return None, None, None

    match = PERIODO_RE.search(texto)
    if not match:
        return None, None, None

    return parse_date(match.group(1)), parse_date(match.group(2)), match.group(0)


def mes_de_fecha(fecha_iso):
    """Nombre del mes en espanol a partir de una fecha AAAA-MM-DD."""
    if not fecha_iso:
        return None
    try:
        mes = int(str(fecha_iso)[5:7])
    except (ValueError, IndexError):
        return None
    return NOMBRES_MES[mes - 1] if 1 <= mes <= 12 else None


MES_ANIO_RE = re.compile(r"\b(" + "|".join(NOMBRES_MES + ["SEPTIEMBRE"]) + r")\s+(\d{4})\b")


def buscar_mes_escrito(texto):
    """Mes escrito con letras, como 'JUNIO 2026' dentro del detalle."""
    match = MES_ANIO_RE.search(normalize(texto or ""))
    if not match:
        return None
    nombre = match.group(1)
    return "SETIEMBRE" if nombre == "SEPTIEMBRE" else nombre


def fecha_es(fecha_iso):
    """AAAA-MM-DD -> DD/MM/AAAA, que es como se lleva el control."""
    if not fecha_iso:
        return None
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(fecha_iso).strip())
    if not match:
        return fecha_iso
    anio, mes, dia = match.groups()
    return f"{dia}/{mes}/{anio}"


PERIODO_TEXTO_RE = re.compile(
    r"(?:PERIODO|CORRESPONDIENTE\s+(?:AL?|DEL)|DEL|MES\s+DE)\s*[:.]?\s*"
    r"(\d{1,2}[./\-]\d{1,2}[./\-]\d{4})",
    re.I,
)


def buscar_periodo_documento(texto):
    """Periodo facturado en cualquier parte del documento.

    Se usa cuando el concepto no trae fechas propias: casi todas las facturas
    de arriendo dicen en alguna linea a que mes corresponden, y ese mes es el
    que vale, no el de emision.
    """
    desde, hasta, _ = extraer_periodo(texto)
    if desde:
        return desde, hasta

    match = PERIODO_TEXTO_RE.search(texto or "")
    if match:
        return parse_date(match.group(1)), None

    return None, None


# Lo que nunca es el nombre del emisor: titulos del documento, direcciones y
# datos de contacto.
NO_ES_NOMBRE = (
    "FACTURA", "BOLETA", "NOTA DE", "RECIBO", "R.U.C", "RUC", "SENOR",
    "DIRECCION", "TELEFONO", "TELEFONOS", "TELF", "FAX", "PAGINA WEB",
    "CLIENTE", "AV.", "AV ", "CAL.", "CALLE", "JR.", "JIRON", "URB.",
    "NRO.", "CENTRAL TELEFONICA", "REPRESENTACION", "AUTORIZADO",
)

# Coletillas del titulo que a veces quedan pegadas al nombre cuando el PDF
# parte "FACTURA ELECTRONICA" en dos columnas.
COLA_TITULO_RE = re.compile(
    r"\s*(?:ELECTR[OÓ]NICA|ELECTR[OÓ]NICO)\s*$", re.I
)


def _nombre_plausible(linea):
    """Si una linea puede ser el nombre del emisor."""
    limpia = collapse_spaces(COLA_TITULO_RE.sub("", linea or "")) or ""
    plano = normalize(limpia)

    if not (6 <= len(limpia) <= 90):
        return None
    if any(plano.startswith(t) for t in NO_ES_NOMBRE):
        return None
    # El titulo del comprobante en medio de la linea: "CENTRO DE SALUD
    # OCUPACIONAL FACTURA ELECTRONICA" es el lema del logo junto al titulo, no
    # el nombre del emisor.
    if re.search(r"\b(?:FACTURA|BOLETA|NOTA DE (?:CREDITO|DEBITO))\b", plano):
        return None
    if re.search(r"\b(?:10|15|16|17|20)\d{9}\b", plano):
        return None
    if re.search(r"\d{3,}", plano):          # direcciones y telefonos
        return None
    if len(re.findall(r"[A-Z]", plano)) < 6:
        return None
    return limpia


def find_nombre_emisor(text, excluir=()):
    """Nombre del emisor, tenga o no forma societaria.

    Primero se busca una razon social con su sufijo (S.A.C., S.R.L. ...). Si no
    la hay -universidades, nombres comerciales- se toma la primera linea de la
    cabecera que pueda ser un nombre y no sea un titulo ni una direccion.
    """
    societaria = find_razon_social_emisor(text, excluir=excluir)
    if societaria:
        return societaria

    excluir_plano = [normalize(termino) for termino in excluir]
    for linea in split_lines(text)[:12]:
        if any(termino in normalize(linea) for termino in excluir_plano):
            continue
        nombre = _nombre_plausible(linea)
        if nombre:
            return nombre

    return None
