"""Directorio de emisores: del RUC a la razon social.

Varios centros comerciales imprimen su razon social dentro del logo, y el logo
es una imagen: ese texto no existe dentro del PDF. Cuando pasa eso la lectura
se quedaba con lo primero que si era texto -el nombre comercial del centro
comercial ("CENTRO COMERCIAL PLAZA SAN MIGUEL") o, peor, el encabezado de la
tabla ("CODIGO DESCRIPCION UM CANTIDAD...")-, que no es el proveedor al que se
le paga.

El RUC, en cambio, siempre viene como texto porque SUNAT lo exige en la
representacion impresa. Por eso el nombre se resuelve por RUC y no por lo que
se lea en la cabecera.

PARA ANADIR UN PROVEEDOR NUEVO: una linea en RAZON_SOCIAL_POR_RUC con el RUC
de 11 digitos y la razon social tal como debe salir en el control. No hace
falta tocar nada mas.
"""
from forus_parsing import collapse_spaces, normalize

# RUC -> razon social del emisor. El comentario de al lado es el nombre
# comercial con el que se conoce al centro comercial, que es lo que aparece
# impreso en la factura.
RAZON_SOCIAL_POR_RUC = {
    "20113126087": "MALL SERVICE S.A.C.",              # C.C. Plaza San Miguel
    "20604119066": "CENCOSUD PERU SHOPPING S.A.",      # Malls Cencosud (FA16)
    "20109072177": "CENCOSUD RETAIL PERU S.A.",        # Metro / Wong (FA26)
    "20478058137": "INMOBILIARIA PUERTA DEL SOL S.A.C.",  # Real Plaza
    "20523173716": "PARQUE LAMBRAMANI S.A.C.",         # Larcomar
    "20550880041": "STRIP CENTERS DEL PERU S.A.C.",    # Parque La Molina
    "20421526258": "MULTIMERCADOS ZONALES S.A.C.",     # Mega Plaza
    "20208388500": "TRES PALMERAS S.A.",               # C.C. Camacho (FA23)
}

# Nombres comerciales de centros comerciales. No son razones sociales: si una
# linea es solo esto, no puede ir a la columna RAZON SOCIAL.
NOMBRES_COMERCIALES = (
    "CENTRO COMERCIAL", "C.C.", "MALL AVENTURA", "REAL PLAZA", "LARCOMAR",
    "JOCKEY PLAZA", "PLAZA NORTE", "MEGA PLAZA", "MEGAPLAZA", "OPEN PLAZA",
    "PARQUE LA MOLINA", "PLAZA SAN MIGUEL", "MINKA", "PLAZA LIMA SUR",
    "PLAZA DEL SOL", "BOULEVARD", "OUTLET",
)

# Palabras del encabezado de la tabla de detalle. Dos o mas juntas en una linea
# corta significan que es una fila de titulos, nunca el nombre del proveedor.
PALABRAS_ENCABEZADO = (
    "CODIGO", "DESCRIPCION", "CANTIDAD", "CANT.", "UNITARIO", "PRECIO",
    "VALOR", "IMPORTE", "DSCTO", "DESCUENTO", "ARTICULO", "UM", "U.M.",
)


def razon_social_por_ruc(ruc):
    """Razon social registrada para ese RUC, o None si no esta en el directorio."""
    if not ruc:
        return None
    digitos = "".join(caracter for caracter in str(ruc) if caracter.isdigit())
    return RAZON_SOCIAL_POR_RUC.get(digitos)


def es_encabezado_de_tabla(linea):
    """Si la linea es la fila de titulos de la tabla de detalle."""
    plano = normalize(collapse_spaces(linea) or "")
    if not plano:
        return False
    coincidencias = sum(1 for palabra in PALABRAS_ENCABEZADO
                        if f" {palabra} " in f" {plano} ")
    return coincidencias >= 2


def es_nombre_comercial(linea):
    """Si la linea es el nombre del centro comercial y no el del proveedor."""
    plano = normalize(collapse_spaces(linea) or "")
    if not plano:
        return False
    return any(plano.startswith(nombre) for nombre in NOMBRES_COMERCIALES)
