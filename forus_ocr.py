"""Lectura de facturas escaneadas.

Un PDF electronico lleva el texto dentro y se lee directo. Uno escaneado es una
foto del papel: no hay texto que extraer. Para esos se rasteriza la pagina y se
pasa por OCR.

El OCR devuelve trozos sueltos con sus coordenadas, no lineas. Aqui se vuelven
a juntar por su posicion vertical y se ordenan de izquierda a derecha, de modo
que el resultado se parece a lo que habria devuelto un PDF con texto: eso es lo
que permite que el resto del programa -que busca "IMPORTE TOTAL" y lee lo que
sigue en la misma linea- funcione igual con un escaneo.

Todo esto es opcional: si las librerias no estan instaladas la aplicacion sigue
funcionando con los PDFs normales y avisa de los que no pudo leer.
"""
import io

import streamlit as st

# Resolucion de rasterizado. 200 ppp es el punto donde el OCR ya lee bien los
# importes sin que la pagina tarde de mas.
RESOLUCION = 200

# Por debajo de esta confianza el trozo se descarta: mas vale un hueco que un
# numero inventado.
CONFIANZA_MINIMA = 0.5


# Ultimo fallo del OCR, para poder contarlo en la hoja Auditoria.
_ULTIMO_ERROR = {"motivo": None}


def ultimo_error():
    return _ULTIMO_ERROR["motivo"]


def estado_ocr():
    """(disponible, motivo). El motivo explica que falta cuando no se puede usar.

    Importa de verdad las librerias en vez de solo comprobar que esten: OpenCV,
    que va por debajo del OCR, necesita libGL del sistema y falla al importarse
    si no esta. Sin este detalle el OCR se apagaba en silencio y las facturas
    escaneadas salian como ilegibles sin decir por que.
    """
    try:
        import numpy  # noqa: F401
        import pypdfium2  # noqa: F401
    except ImportError as error:
        return False, (
            f"Falta una libreria de lectura de PDF ({error.name}). "
            "Anade pypdfium2 a requirements.txt"
        )

    try:
        _clase_motor()
    except ImportError as error:
        if "libGL" in str(error) or "libglib" in str(error) or "libgthread" in str(error):
            return False, (
                f"OpenCV no encuentra una libreria del sistema ({error}). Falta el "
                "archivo packages.txt con libgl1 en la raiz del repositorio"
            )
        return False, (
            f"Falta el motor de OCR ({error.name or error}). Anade rapidocr a requirements.txt"
        )
    except Exception as error:
        return False, (
            f"El motor de OCR no arranca: {error}. Si menciona libGL, anade "
            "opencv-python-headless a requirements.txt"
        )

    return True, None


def ocr_disponible():
    return estado_ocr()[0]


def _clase_motor():
    """La clase RapidOCR, sea de la version nueva o de la antigua.

    La 3.x se llama `rapidocr` y es la unica con soporte para Python 3.13 y
    posteriores; `rapidocr_onnxruntime` es la anterior. Se aceptan las dos para
    que la aplicacion funcione con cualquiera de las que haya instalada.
    """
    try:
        from rapidocr import RapidOCR
    except ImportError as error_nuevo:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            # El error que importa es el de la version nueva, que es la que esta
            # en requirements.txt. Antes se informaba el de la antigua y el
            # aviso decia "falta rapidocr_onnxruntime" cuando lo que faltaba de
            # verdad era libGL, que OpenCV necesita para cargarse.
            raise error_nuevo from None
    return RapidOCR


@st.cache_resource(show_spinner=False)
def _motor():
    """El motor de OCR se carga una sola vez y se reutiliza.

    La primera vez descarga sus modelos, asi que tarda mas que las siguientes.
    """
    import logging

    logging.getLogger("RapidOCR").setLevel(logging.WARNING)
    return _clase_motor()()


def _bloques(resultado):
    """Normaliza la salida a [(caja, texto, confianza)].

    La version 3.x devuelve un objeto con boxes, txts y scores por separado; la
    antigua, una lista de tuplas.
    """
    if resultado is None:
        return []

    cajas = getattr(resultado, "boxes", None)
    if cajas is None:
        return list(resultado)

    textos = getattr(resultado, "txts", None) or []
    scores = getattr(resultado, "scores", None) or []
    return [
        (cajas[indice], textos[indice] if indice < len(textos) else "",
         scores[indice] if indice < len(scores) else None)
        for indice in range(len(cajas))
    ]


def _confianza(valor):
    try:  # la confianza llega unas veces como numero y otras como texto
        return float(valor)
    except (TypeError, ValueError):
        return None


def _inclinacion(bloques):
    """Angulo (radianes) en que esta torcida la hoja, a partir de las cajas.

    Cada caja de una palabra larga es casi un rectangulo alineado con su
    renglon; la mediana de sus angulos es la inclinacion de la foto.
    """
    import math

    angulos = []
    for caja, _, _ in bloques:
        (x0, y0), (x1, y1) = caja[0], caja[1]
        ancho = math.hypot(x1 - x0, y1 - y0)
        alto = math.hypot(caja[3][0] - x0, caja[3][1] - y0)
        if ancho > 3 * alto and ancho > 0:
            angulos.append(math.atan2(y1 - y0, x1 - x0))
    if len(angulos) < 5:
        return 0.0
    angulos.sort()
    return angulos[len(angulos) // 2]


def _agrupar_en_lineas(bloques):
    """Reconstruye las lineas del documento a partir de las cajas del OCR.

    En una foto la hoja casi nunca esta derecha: con unos grados de giro, el
    final de un renglon queda a la altura del siguiente y antes se juntaba toda
    la cabecera en una sola linea, con la fecha lejos de su rotulo. Por eso las
    coordenadas se enderezan primero y cada trozo se compara con la altura media
    del renglon, no con el ultimo trozo anadido.
    """
    import math

    angulo = _inclinacion(bloques)
    coseno, seno = math.cos(-angulo), math.sin(-angulo)

    elementos = []
    for caja, texto, confianza in bloques:
        if not texto:
            continue
        valor = _confianza(confianza)
        if valor is not None and valor < CONFIANZA_MINIMA:
            continue
        puntos = [(x * coseno - y * seno, x * seno + y * coseno) for x, y in caja]
        ys = [punto[1] for punto in puntos]
        xs = [punto[0] for punto in puntos]
        elementos.append({
            "centro": sum(ys) / len(ys),
            "alto": max(ys) - min(ys),
            "x": min(xs),
            "texto": str(texto).strip(),
        })

    if not elementos:
        return ""

    altos = sorted(e["alto"] for e in elementos)
    alto_tipico = altos[len(altos) // 2] or 1

    elementos.sort(key=lambda e: (e["centro"], e["x"]))

    lineas = []
    actual = [elementos[0]]
    centro_linea = elementos[0]["centro"]
    for elemento in elementos[1:]:
        # Dos trozos son del mismo renglon si el centro del nuevo cae a menos
        # de media altura de texto del centro medio del renglon.
        tolerancia = max(alto_tipico, min(elemento["alto"], 2 * alto_tipico)) * 0.55
        if abs(elemento["centro"] - centro_linea) <= tolerancia:
            actual.append(elemento)
            centro_linea = sum(e["centro"] for e in actual) / len(actual)
        else:
            lineas.append(actual)
            actual = [elemento]
            centro_linea = elemento["centro"]
    lineas.append(actual)

    return "\n".join(
        " ".join(trozo["texto"] for trozo in sorted(linea, key=lambda e: e["x"]))
        for linea in lineas
    )


# ---------------------------------------------------------------------------
# Fotos de facturas: hoja sobre la mesa, girada, con perspectiva.
# ---------------------------------------------------------------------------

# Palabras que tiene cualquier comprobante. Sirven para saber si una lectura
# salio bien: leida al reves o de lado, el OCR no las reconoce.
PALABRAS_COMPROBANTE = (
    "RUC", "FACTURA", "BOLETA", "TOTAL", "IGV", "FECHA", "SON", "SOLES",
    "IMPORTE", "EMISION", "ELECTRONICA", "CLIENTE", "SENOR", "MONEDA",
)


def _puntaje(bloques):
    """Que tan bien se leyo una imagen: caracteres leidos con confianza y
    palabras de comprobante reconocidas."""
    import unicodedata

    caracteres = 0.0
    texto = []
    for _, contenido, confianza in bloques:
        valor = _confianza(confianza)
        if not contenido or (valor is not None and valor < CONFIANZA_MINIMA):
            continue
        caracteres += len(str(contenido)) * (valor if valor is not None else 1.0)
        texto.append(str(contenido))
    plano = unicodedata.normalize("NFKD", " ".join(texto).upper())
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    palabras = sum(1 for palabra in PALABRAS_COMPROBANTE if palabra in plano)
    return caracteres + 60 * palabras, palabras


def _leer(motor, imagen):
    """OCR de una imagen sin el clasificador de giro de renglones.

    El clasificador endereza cada renglon por separado y deja la hoja leida de
    abajo arriba; sin el, una hoja al reves sale con poca confianza, y eso es
    justo lo que permite detectar que hay que girarla.
    """
    try:
        return _bloques(motor(imagen, use_cls=False))
    except TypeError:  # version antigua, sin ese parametro
        return _bloques(motor(imagen))


def _recortar_hoja(imagen):
    """Endereza la perspectiva de una foto recortando la hoja.

    Busca el contorno de cuatro lados mas grande y claro de la imagen -la hoja
    sobre la mesa- y lo lleva a un rectangulo. Si no hay una hoja clara (un
    escaneo de borde a borde) devuelve la imagen tal cual.
    """
    import cv2
    import numpy as np

    alto, ancho = imagen.shape[:2]
    escala = 1000.0 / max(alto, ancho)
    chica = cv2.resize(imagen, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
    gris = cv2.GaussianBlur(cv2.cvtColor(chica, cv2.COLOR_RGB2GRAY), (5, 5), 0)

    area_total = chica.shape[0] * chica.shape[1]
    candidatos = []
    _, binaria = cv2.threshold(gris, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bordes = cv2.dilate(cv2.Canny(gris, 50, 150), np.ones((3, 3), np.uint8))
    for mascara in (binaria, bordes):
        contornos, _ = cv2.findContours(mascara, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contorno in sorted(contornos, key=cv2.contourArea, reverse=True)[:3]:
            perimetro = cv2.arcLength(contorno, True)
            aproximado = cv2.approxPolyDP(contorno, 0.02 * perimetro, True)
            area = cv2.contourArea(aproximado)
            if len(aproximado) == 4 and 0.25 * area_total <= area <= 0.97 * area_total:
                candidatos.append((area, aproximado.reshape(4, 2).astype("float32")))
        if candidatos:
            break

    if not candidatos:
        return imagen

    # Solo es una hoja sobre la mesa si por fuera del contorno es mas oscuro
    # que por dentro. Asi un recuadro o una tabla de un escaneo normal, que
    # tienen papel blanco alrededor, no se recortan por error.
    contorno_chico = max(candidatos, key=lambda c: c[0])[1]
    mascara = np.zeros(gris.shape, np.uint8)
    cv2.fillConvexPoly(mascara, contorno_chico.astype(np.int32), 255)
    dentro = cv2.mean(gris, mask=mascara)[0]
    fuera = cv2.mean(gris, mask=cv2.bitwise_not(mascara))[0]
    if dentro - fuera < 30:
        return imagen

    puntos = contorno_chico / escala
    # Orden: arriba-izquierda, arriba-derecha, abajo-derecha, abajo-izquierda.
    suma = puntos.sum(axis=1)
    resta = np.diff(puntos, axis=1).ravel()
    ordenados = np.array([
        puntos[np.argmin(suma)], puntos[np.argmin(resta)],
        puntos[np.argmax(suma)], puntos[np.argmax(resta)],
    ], dtype="float32")
    if len({tuple(p) for p in ordenados.tolist()}) < 4:
        return imagen

    def distancia(a, b):
        return float(np.hypot(*(a - b)))

    nuevo_ancho = int(max(distancia(ordenados[0], ordenados[1]), distancia(ordenados[3], ordenados[2])))
    nuevo_alto = int(max(distancia(ordenados[0], ordenados[3]), distancia(ordenados[1], ordenados[2])))
    if nuevo_ancho < 300 or nuevo_alto < 300:
        return imagen

    destino = np.array(
        [[0, 0], [nuevo_ancho - 1, 0], [nuevo_ancho - 1, nuevo_alto - 1], [0, nuevo_alto - 1]],
        dtype="float32",
    )
    matriz = cv2.getPerspectiveTransform(ordenados, destino)
    return cv2.warpPerspective(imagen, matriz, (nuevo_ancho, nuevo_alto))


# Rotulos de la cabecera y del pie de un comprobante. En una hoja derecha la
# cabecera queda arriba del pie; si sale al reves, la hoja esta de cabeza.
PALABRAS_CABECERA = ("RUC", "SENOR", "EMISION", "CLIENTE")
PALABRAS_PIE = ("SON", "IMPORTE TOTAL", "REPRESENTACION", "TOTAL A PAGAR", "OP. GRAVADA")


def _plano(texto):
    import unicodedata

    plano = unicodedata.normalize("NFKD", str(texto or "").upper())
    return "".join(c for c in plano if not unicodedata.combining(c))


def _geometria(bloques):
    """(fraccion de renglones horizontales, orden) de una lectura.

    El OCR lee el texto aunque la hoja este de lado o de cabeza -gira cada
    renglon por su cuenta-, asi que lo leido no sirve para saber el giro. Lo
    que si lo dice es la forma: de lado, las cajas son verticales; de cabeza,
    el pie aparece encima de la cabecera. `orden` vale 1 si la cabecera esta
    arriba, -1 si esta abajo y 0 si no se puede saber.
    """
    horizontales = verticales = 0
    cabecera, pie = [], []
    for caja, texto, confianza in bloques:
        if not texto or len(str(texto).strip()) < 4:
            continue
        xs = [punto[0] for punto in caja]
        ys = [punto[1] for punto in caja]
        if max(xs) - min(xs) >= max(ys) - min(ys):
            horizontales += 1
        else:
            verticales += 1
        plano = _plano(texto)
        centro = sum(ys) / len(ys)
        if any(palabra in plano for palabra in PALABRAS_CABECERA):
            cabecera.append(centro)
        if any(palabra in plano for palabra in PALABRAS_PIE):
            pie.append(centro)

    total = horizontales + verticales
    fraccion = horizontales / total if total else 0.0

    orden = 0
    if cabecera and pie:
        cabecera.sort()
        pie.sort()
        orden = 1 if cabecera[len(cabecera) // 2] < pie[len(pie) // 2] else -1
    return fraccion, orden


def _girar(imagen, giro):
    import numpy as np

    return np.ascontiguousarray(np.rot90(imagen, giro)) if giro else imagen


def _leer_imagen(motor, imagen):
    """Bloques de la mejor lectura posible de la imagen de una pagina.

    1. Si es una foto de la hoja sobre la mesa, se recorta la hoja y se le
       quita la perspectiva.
    2. Se lee. Si los renglones salen horizontales y la cabecera arriba, ya
       esta: es el caso de casi todos los escaneos y cuesta una sola lectura.
    3. Si no, se gira lo que haga falta (90, 180 o 270 grados) y se vuelve a
       leer. Si ninguna lectura queda clara, gana la de mejor puntaje.
    """
    base = _recortar_hoja(imagen)

    lecturas = {}

    def leer(giro):
        if giro not in lecturas:
            bloques = _leer(motor, _girar(base, giro))
            puntaje, palabras = _puntaje(bloques)
            fraccion, orden = _geometria(bloques)
            lecturas[giro] = {
                "bloques": bloques,
                "puntaje": puntaje,
                "horizontal": fraccion >= 0.6,
                # Derecha de verdad: renglones horizontales, se reconocen las
                # palabras de un comprobante y la cabecera no queda abajo. De
                # cabeza el OCR devuelve basura sin esas palabras.
                "clara": fraccion >= 0.6 and palabras >= 6 and orden > 0,
            }
        return lecturas[giro]

    if leer(0)["clara"]:
        return lecturas[0]["bloques"]

    # Horizontal pero no clara: esta de cabeza. Si no, esta de lado.
    for giro in ((2,) if lecturas[0]["horizontal"] else (1, 3)):
        if leer(giro)["clara"]:
            return lecturas[giro]["bloques"]

    # Nada quedo claro: gana el mejor puntaje entre las lecturas con renglones
    # horizontales, probando antes los giros que falten.
    for giro in (0, 1, 2, 3):
        leer(giro)
    horizontales = [v for v in lecturas.values() if v["horizontal"]] or list(lecturas.values())
    return max(horizontales, key=lambda v: v["puntaje"])["bloques"]


def texto_por_ocr(datos_pdf, numeros_pagina):
    """Texto de las paginas indicadas de un PDF, leidas por OCR.

    Devuelve {numero_de_pagina: texto}. Las paginas que fallen no aparecen.
    """
    if not numeros_pagina or not ocr_disponible():
        return {}

    import numpy as np
    import pypdfium2 as pdfium

    try:
        motor = _motor()
    except Exception as error:
        # Sin motor no hay nada que hacer, pero el fallo tiene que verse: antes
        # se tragaba en silencio y las facturas salian como ilegibles sin decir
        # que lo que faltaba era una libreria.
        _ULTIMO_ERROR["motivo"] = f"El motor de OCR no se pudo cargar: {error}"
        return {}

    resultados = {}
    documento = pdfium.PdfDocument(io.BytesIO(datos_pdf))
    try:
        for numero in numeros_pagina:
            indice = numero - 1
            if indice < 0 or indice >= len(documento):
                continue
            try:
                imagen = np.array(
                    documento[indice].render(scale=RESOLUCION / 72).to_pil().convert("RGB")
                )
                bloques = _leer_imagen(motor, imagen)
            except Exception as error:  # una pagina ilegible no frena a las demas
                _ULTIMO_ERROR["motivo"] = f"El OCR fallo en la pagina {numero}: {error}"
                continue
            texto = _agrupar_en_lineas(bloques)
            if texto.strip():
                resultados[numero] = texto
    finally:
        documento.close()

    return resultados


# Una pagina que es casi toda imagen y trae poco texto es una foto o un escaneo
# aunque tenga algo de texto: las apps de escaneo le ponen su marca ("Escaneado
# con CamScanner") y antes eso bastaba para que no se pasara por OCR.
COBERTURA_FOTO = 0.5
TEXTO_MINIMO = 150


def _cobertura_imagenes(datos_pdf):
    """{numero_de_pagina: fraccion de la pagina cubierta por imagenes}."""
    try:
        import pypdfium2 as pdfium
        import pypdfium2.raw as pdfium_c
    except ImportError:
        return {}

    cobertura = {}
    try:
        documento = pdfium.PdfDocument(io.BytesIO(datos_pdf))
    except Exception:
        return {}
    try:
        for indice in range(len(documento)):
            pagina = documento[indice]
            ancho, alto = pagina.get_size()
            area = (ancho * alto) or 1
            cubierta = 0.0
            for objeto in pagina.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=3):
                # get_bounds en pypdfium2 5.x; get_pos en la 4.x.
                limites = getattr(objeto, "get_bounds", None) or getattr(objeto, "get_pos")
                izquierda, abajo, derecha, arriba = limites()
                cubierta += max(0.0, min(derecha, ancho) - max(izquierda, 0)) * max(
                    0.0, min(arriba, alto) - max(abajo, 0)
                )
            cobertura[indice + 1] = min(cubierta / area, 1.0)
    except Exception:  # si no se puede medir, la pagina se trata como antes
        pass
    finally:
        documento.close()
    return cobertura


def _necesita_ocr(pagina, cobertura):
    texto = (pagina.get("texto") or "").strip()
    if not texto:
        return True
    util = len("".join(texto.split()))
    return util < TEXTO_MINIMO and cobertura.get(pagina["numero"], 0) >= COBERTURA_FOTO


def completar_paginas_vacias(datos_pdf, paginas):
    """Rellena por OCR las paginas escaneadas o fotografiadas.

    Son las que no traen texto y las que son casi solo una imagen con un texto
    minimo encima. Devuelve la lista de paginas leidas asi, para poder avisar
    de que esas cifras vienen de un escaneo y conviene revisarlas.
    """
    con_texto_minimo = [
        p for p in paginas
        if (p.get("texto") or "").strip() and len("".join(p["texto"].split())) < TEXTO_MINIMO
    ]
    cobertura = _cobertura_imagenes(datos_pdf) if con_texto_minimo else {}

    a_leer = [p["numero"] for p in paginas if _necesita_ocr(p, cobertura)]
    if not a_leer:
        return []

    leidas = texto_por_ocr(datos_pdf, a_leer)
    for pagina in paginas:
        if pagina["numero"] in leidas:
            original = (pagina.get("texto") or "").strip()
            # El texto minimo que traia la pagina (la marca de la app) se
            # conserva al final, donde no estorba a la lectura de la factura.
            pagina["texto"] = leidas[pagina["numero"]] + (f"\n{original}" if original else "")
            pagina["ocr"] = True

    return sorted(leidas)
