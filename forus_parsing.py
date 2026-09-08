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


def aplanar(text):
    """Mayusculas, sin tildes y en una sola linea.

    Las leyendas legales de las facturas vienen partidas donde acabe la caja de
    texto ("OPERACIONES SUJETA AL SISTEMA DE PAGO DE\nOBLIGACIONES TRIBUTARIAS
    CON EL GOBIERNO CENTRAL"). Buscarlas sobre el texto tal cual fallaba y la
    factura salia como no sujeta a detraccion.
    """
    return re.sub(r"\s+", " ", normalize(text or ""))


# Algunas facturas escriben sus campos con puntos guia entre cada letra
# (".F.e..c.h..a. .d.e.. .la.. f.a..c.t.u.r.a....3..1.. j.u.l. .2.0..2.6"). Se
# reconoce la racha -al menos dos caracteres seguidos, cada uno con sus puntos
# detras- y se le quitan los puntos, sin espaciar: asi "3..1.." vuelve a ser
# "31". Un importe como "31,089.51" no forma racha, de modo que sus decimales
# nunca se tocan.
RACHA_PUNTEADA_RE = re.compile(r"\.*(?:[^\s.]\.+){2,}[^\s.]?")

# Para decidir si la linea lleva relleno punteado hace falta una racha larga.
# Con una racha corta bastaria "S.A.C." para dar la linea por punteada, y la
# razon social del proveedor perderia sus puntos.
LINEA_PUNTEADA_RE = re.compile(r"\.*(?:[^\s.]\.+){4,}")

# Restos de la racha: los puntos que quedan pegados a una letra. Solo se quitan
# en las lineas donde ya se detecto el relleno punteado, para no tocar el resto
# del documento. Un punto entre dos cifras nunca entra aqui.
PUNTO_ENTRE_LETRAS_RE = re.compile(r"(?<=[^\W\d_])\.+|\.+(?=[^\W\d_])")


def reparar_lineas_punteadas(text):
    """Quita los puntos guia con los que algunas facturas rellenan sus campos."""
    if not text:
        return text or ""

    lineas = []
    for linea in text.split("\n"):
        if LINEA_PUNTEADA_RE.search(linea):
            linea = RACHA_PUNTEADA_RE.sub(lambda m: m.group(0).replace(".", ""), linea)
            linea = PUNTO_ENTRE_LETRAS_RE.sub("", linea)
        lineas.append(linea)
    return "\n".join(lineas)


def aplanar_con_reverso(text):
    """El texto aplanado, y ademas leido al reves.

    Algunos comprobantes imprimen sus leyendas legales en vertical, al margen de
    la hoja; el extractor las devuelve letra por letra y en orden inverso
    ("otcefa on otnemucoD"). Al darle la vuelta al texto vuelven a leerse, y asi
    se aprovechan avisos que de otro modo se pierden -por ejemplo que el recibo
    no esta afecto al regimen de retencion-.
    """
    plano = aplanar(text)
    return plano + " " + aplanar((text or "")[::-1])


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


# Dentro de una etiqueta y entre la etiqueta y su valor solo se admiten
# espacios de la misma linea. Con \s+ el salto de linea contaba como
# separador y una etiqueta al final de la linea -el encabezado "... Descuento
# Total" de la tabla de detalle- se llevaba el primer numero de la linea
# siguiente, que es la cantidad del item. De ahi salian los importes de 1.00.
ESPACIO_MISMA_LINEA = r"[^\S\n]"


def _patron_etiqueta(label):
    partes = []
    for caracter in normalize(label):
        if caracter in COMODIN_ACENTO:
            partes.append(COMODIN_ACENTO[caracter])
        elif caracter == " ":
            partes.append(ESPACIO_MISMA_LINEA + "+")
        else:
            partes.append(re.escape(caracter))
    cuerpo = "".join(partes)

    # La etiqueta tiene que ser una palabra entera. Sin esto "TOTAL" acertaba
    # dentro de "SUBTOTAL" y devolvia la base imponible en vez del importe
    # total: es el error que bajaba las facturas sin IGV.
    plano = normalize(label)
    if plano[:1].isalnum():
        cuerpo = r"(?<![A-Z0-9])" + cuerpo
    if plano.startswith("TOTAL"):
        # "SUB TOTAL" es la base imponible, no el importe total. Va aparte
        # porque entre las dos palabras hay un espacio y la frontera de palabra
        # no lo distingue.
        cuerpo = r"(?<!SUB )" + cuerpo
    if plano[-1:].isalpha():
        cuerpo += r"(?![A-Z])"
    elif plano[-1:].isdigit():
        cuerpo += r"(?!\d)"
    return cuerpo


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


def iter_label_values(text, labels, max_chars=90, requiere_separador=False,
                     siguiente_linea=False):
    """Todos los valores que siguen a las etiquetas, alias por alias.

    Hace falta porque una etiqueta suele aparecer varias veces: "TOTAL" esta
    primero en el encabezado de la tabla, donde no hay ningun importe detras, y
    solo mas abajo en la fila que interesa. Quedarse con la primera aparicion
    hace perder el dato.
    """
    if not text:
        return

    espacio = ESPACIO_MISMA_LINEA + "*"
    if requiere_separador:
        separador = espacio + r"[:=]" + espacio
    else:
        separador = espacio + r"[:.\-]?" + espacio
    plano = normalize(text)

    for label in labels:
        for match in re.finditer(_patron_etiqueta(label) + separador, plano):
            inicio = match.end()
            resto = text[inicio:inicio + max_chars]
            partes = resto.split("\n")
            valor = collapse_spaces(partes[0])
            if siguiente_linea:
                # Dos lineas: hay formularios que dejan una linea de por medio
                # entre el rotulo de la columna y su dato.
                valor = collapse_spaces(" ".join(partes[1:3]))
            if valor:
                yield valor


PORCENTAJE_RE = re.compile(r"\(?\s*\d+(?:[.,]\d+)?\s*%\s*\)?")


MONEDA_SUELTA_RE = re.compile(r"(?i)(?:S/\.?|US\s*\$|U\$S|\$|\bUSD\b|\bPEN\b|\bEUR\b|\bCLP\b|\bCOP\b|\bSOLES\b|\bD[OÓ]LARES\b)")


def find_money(text, labels, estricto=True):
    """Primer importe que aparece despues de alguna de las etiquetas.

    Descarta los porcentajes para que "IGV (18%): 180.00" devuelva 180.00 y
    no 18, que es el error tipico al leer estas cabeceras.

    Con `estricto` -lo normal- el importe tiene que ser lo primero que sigue a
    la etiqueta, admitiendo por medio solo el simbolo de moneda y los signos de
    puntuacion. Es lo que impide que la etiqueta "TOTAL" se quede con el numero
    de "Total Gravado 110.04", que es la base y no el importe total.
    """
    for crudo in iter_label_values(text, labels):
        limpio = PORCENTAJE_RE.sub(" ", crudo)
        limpio = MONEDA_SUELTA_RE.sub(" ", limpio)
        if estricto:
            limpio = limpio.lstrip(" :=.-·|\t")
            # Preposiciones que quedan entre la etiqueta y el numero al quitar
            # la moneda: "Importe total adeudado en USD USD 31,089.51".
            limpio = re.sub(r"(?i)^(?:[a-z]{1,3}\b[\s:.=-]*)+", "", limpio)
            match = re.match(r"-?\(?\d[\d.,]*\)?-?", limpio)
        else:
            match = re.search(r"-?\(?\d[\d.,]*\)?-?", limpio)
        if match:
            valor = parse_amount(match.group(0))
            if valor is not None:
                return valor
    return None


def find_percent(text, labels):
    """Porcentaje asociado a una etiqueta, por ejemplo la tasa de detraccion.

    Recorre todas las apariciones: la primera suele ser un encabezado sin
    numero detras, y quedarse con ella perdia la tasa.
    """
    for crudo in iter_label_values(text, labels):
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", crudo)
        if match:
            valor = parse_amount(match.group(1))
            if valor is not None:
                return valor
    return None


MESES = {
    "ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5, "JUNIO": 6,
    "JULIO": 7, "AGOSTO": 8, "SETIEMBRE": 9, "SEPTIEMBRE": 9, "OCTUBRE": 10,
    "NOVIEMBRE": 11, "DICIEMBRE": 12,
    # Abreviados, en espanol y en ingles: los usan las facturas del exterior
    # ("01-Aug-2026", "31 jul 2026").
    "ENE": 1, "JAN": 1, "FEB": 2, "MAR": 3, "ABR": 4, "APR": 4, "MAY": 5,
    "JUN": 6, "JUL": 7, "AGO": 8, "AUG": 8, "SET": 9, "SEP": 9, "OCT": 10,
    "NOV": 11, "DIC": 12, "DEC": 12,
}

# "02 de Julio del 2026", "17 de julio de 2026".
FECHA_EN_LETRAS_RE = re.compile(r"\b(\d{1,2})\s+DE\s+([A-Z]+)\s+DE\s*L?\s+(\d{4})\b")
# "01-Aug-2026", "31 jul 2026", "05 SET 2026".
FECHA_MES_ABREVIADO_RE = re.compile(r"\b(\d{1,2})[\s\-/]([A-Z]{3,10})\.?[\s\-/](\d{2,4})\b")


def parse_date(value):
    """Normaliza a AAAA-MM-DD las fechas mas comunes en documentos peruanos."""
    if not value:
        return None

    texto = collapse_spaces(str(value))
    if not texto:
        return None

    # El cierre es (?!\d) y no \b para que la fecha de una marca de tiempo
    # ("2026-08-03T23:51:02") tambien se reconozca: con \b la T pegada al dia
    # impedia el match y la fecha de emision salia vacia.
    match = re.search(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})(?!\d)", texto)
    if match:
        anio, mes, dia = match.groups()
    else:
        match = re.search(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})(?!\d)", texto)
        if match:
            dia, mes, anio = match.groups()
        else:
            plano = normalize(texto)
            match = FECHA_EN_LETRAS_RE.search(plano) or FECHA_MES_ABREVIADO_RE.search(plano)
            if not match:
                return None
            dia, nombre_mes, anio = match.groups()
            mes = MESES.get(nombre_mes)
            if not mes:
                return None

    try:
        dia, mes, anio = int(dia), int(mes), int(anio)
    except (TypeError, ValueError):
        return None

    if anio < 100:
        anio += 2000
    if not (1 <= mes <= 12 and 1 <= dia <= 31):
        return None

    return f"{anio:04d}-{mes:02d}-{dia:02d}"


def find_date(text, labels, excluir=()):
    """Fecha que sigue a alguna etiqueta, probando todas las apariciones.

    `excluir` son palabras que delatan que la fecha encontrada es de otra cosa:
    "Fecha Emisor: 25/02/21" es la del codigo de autorizacion del documento, no
    la de la factura.

    Muchas facturas rotulan la columna ("FECHA DE EMISION (DD/MM/AA)") en una
    linea y ponen el dato mas abajo. Quedarse con la primera aparicion devolvia
    "(DD/MM/AA)" y la fecha salia vacia.

    Si en la misma linea no hay ninguna fecha se mira el principio de la linea
    siguiente, que es donde caen los valores cuando la factura los imprime como
    una tabla de dos filas: rotulos arriba, datos abajo. Con las fechas se puede
    hacer porque el valor se valida -si no es una fecha, no se acepta-; con los
    importes no, y por eso ahi se exige la misma linea.
    """
    def aceptable(crudo):
        # Solo se mira lo que va justo delante de la fecha: en un texto largo
        # -sobre todo si viene de un escaneo, donde los campos salen
        # revueltos- una palabra excluida al otro extremo de la linea no dice
        # nada sobre la fecha que se esta leyendo.
        plano = normalize(crudo)
        posicion = re.search(r"\d", plano)
        antes = plano[max(0, posicion.start() - 25):posicion.start()] if posicion else plano
        return not any(palabra in antes for palabra in excluir)

    for crudo in iter_label_values(text, labels, max_chars=40):
        fecha = parse_date(crudo) if aceptable(crudo) else None
        if fecha:
            return fecha

    for crudo in iter_label_values(text, labels, max_chars=120, siguiente_linea=True):
        fecha = parse_date(crudo) if aceptable(crudo) else None
        if fecha:
            return fecha
    return None


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
    # Un "$" a secas en un documento sin ninguna marca peruana es un dolar:
    # asi se firman las facturas de proveedores del exterior.
    if "$" in (text or ""):
        return "USD"
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
    r"|E\.?\s*I\.?\s*R\.?\s*L\.?|S\.?\s*A\.?\s*S\.?|S\.?\s*A\.?"
    r"|SAC|SRL|SAA|EIRL|SAS|SPA|LTDA|LIMITED|LLC|LTD|INC|GMBH|B\.?V\.?)(?:\s|$)"
)

TITULOS_DOCUMENTO = (
    "FACTURA", "BOLETA", "NOTA DE", "RECIBO", "REPRESENTACION", "R.U.C", "RUC",
    "SENOR", "SE" + REEMPLAZO + "OR", "DIRECCION", "TELEFONO", "TELF", "AV.", "CAL.",
    # Rotulos de las cajas del formulario. Sin esto la razon social salia como
    # "FECHA DE EMISION (DD/MM/AA)" en las facturas que imprimen los titulos de
    # los campos antes que la cabecera del emisor.
    "FECHA", "MONEDA", "FORMA DE PAGO", "CONDICION", "CANTIDAD", "CODIGO",
    "DESCRIPCION", "ITEM", "CLIENTE", "NOTAS", "ELABORADO", "OPERACION",
    "DETRACCION", "DEPARTAMENTO", "NIT", "VAT", "RUT", "TAX", "INVOICE",
    "BILL TO", "PAGINA", "PAGE", "SUBTOTAL", "TOTAL", "IGV", "SON ",
    "BENEFICIARY", "BENEFICIARIO", "BANK", "BANCO", "IBAN", "SWIFT", "REMIT",
    "ACCT", "ACCOUNT", "CUENTA", "PAYMENT",
)


def find_razon_social_emisor(text, excluir=()):
    """Razon social del proveedor: la primera linea que cierra con forma societaria.

    Si la linea anterior es parte del nombre (pasa cuando el RUC lo parte en
    dos), se une. Las lineas de `excluir` -por ejemplo el nombre de la propia
    empresa- se saltan para no confundir al emisor con el cliente.
    """
    # La cabecera no siempre esta arriba del todo: hay proveedores que imprimen
    # antes los rotulos del formulario y hasta una marca de agua vertical. Por
    # eso se recorre buena parte de la primera pagina y no solo su encabezado.
    lineas = split_lines(text)[:LINEAS_CABECERA]
    excluir_plano = [normalize(termino) for termino in excluir]

    for indice, linea in enumerate(lineas):
        limpia = collapse_spaces(linea)
        if not limpia or len(limpia) < 6 or len(limpia) > 90:
            continue

        plano = normalize(limpia)
        if any(termino in plano for termino in excluir_plano):
            continue
        if any(plano.startswith(titulo) for titulo in TITULOS_DOCUMENTO):
            continue
        if re.search(r"\b(?:10|15|16|17|20)\d{9}\b", plano):
            continue

        sufijo = SUFIJO_SOCIETARIO_RE.search(limpia)
        if not sufijo:
            continue
        limpia = collapse_spaces(limpia[:sufijo.end()])
        if not limpia:
            continue

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

    return None


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
    "FECHA", "MONEDA", "FORMA DE PAGO", "CONDICION", "CANTIDAD", "CODIGO",
    "DESCRIPCION", "DETALLE", "ITEM", "NOTAS", "ELABORADO", "OPERACION",
    "DETRACCION", "DEPARTAMENTO", "NIT", "VAT", "RUT", "TAX", "INVOICE",
    "BILL TO", "PAGINA", "PAGE", "SUBTOTAL", "TOTAL", "IGV", "SON ",
    "MZA.", "LOTE", "PERIODO", "VENCIMIENTO", "GIRO", "DATOS", "TIPO ",
    "CONTACTO", "REFERENCIA", "CUENTA", "CICLO", "RESUMEN", "SEGUN",
    "BENEFICIARY", "BENEFICIARIO", "BANK", "BANCO", "IBAN", "SWIFT", "REMIT",
    "DOCUMENTO", "REMIT", "ATTN", "ADVERTISER", "ACCOUNT", "PAYMENT",
    "BILLING", "LINE#", "IDENTIFICADO", "DOMICILIADO", "RECIBI", "POR CONCEPTO",
    "OBSERVACION", "INCISO", "LA SUMA", "SON:",
)

# Coletillas del titulo que a veces quedan pegadas al nombre cuando el PDF
# parte "FACTURA ELECTRONICA" en dos columnas.
COLA_TITULO_RE = re.compile(
    r"\s*(?:ELECTR[OÓ]NICA|ELECTR[OÓ]NICO)\s*$", re.I
)


# Cuantas lineas de la primera pagina se miran buscando el nombre del emisor.
LINEAS_CABECERA = 80


def _nombre_plausible(linea):
    """Si una linea puede ser el nombre del emisor."""
    limpia = collapse_spaces(COLA_TITULO_RE.sub("", linea or "")) or ""
    plano = normalize(limpia)

    if not (6 <= len(limpia) <= 90):
        return None
    if any(plano.startswith(t) for t in NO_ES_NOMBRE):
        return None
    if re.search(r"\b(?:10|15|16|17|20)\d{9}\b", plano):
        return None
    if re.search(r"\d{3,}", plano):          # direcciones y telefonos
        return None
    if len(re.findall(r"[A-Z]", plano)) < 6:
        return None
    # Las leyendas que el PDF imprime en vertical salen del extractor como una
    # sola palabra larga e ilegible ("aicnednetnirepuS"), y sin esta condicion
    # una de ellas acababa de nombre del proveedor. Un nombre de una sola
    # palabra se acepta si es corto o si lleva punto o guion, como las marcas
    # ("Google", "Connectly.ai").
    if " " not in limpia and len(limpia) > 12 and not re.search(r"[.\-]", limpia):
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
    for linea in split_lines(text)[:LINEAS_CABECERA]:
        if any(termino in normalize(linea) for termino in excluir_plano):
            continue
        nombre = _nombre_plausible(linea)
        if nombre:
            return nombre

    return None
