"""Razon social de los proveedores recurrentes, por RUC.

Hay proveedores que no escriben su razon social como texto en el PDF: la
dibujan como parte del logo (Cencosud y sus sociedades, Mall Service) o la
imprimen partida en dos lineas con el RUC en medio. Para esos el texto del PDF
no alcanza, asi que el nombre se toma de aqui, que es la fuente fiable.

Para anadir un proveedor basta con una linea mas: el RUC y la razon social tal
como figura en la cabecera de su factura.
"""
import re

RAZON_SOCIAL_POR_RUC = {
    # Cencosud: el nombre va en el logo, no en el texto del PDF.
    "20109072177": "CENCOSUD RETAIL PERU S.A.",
    "20208388500": "TRES PALMERAS S.A.",
    "20604119066": "CENCOSUD PERU SHOPPING S.A.",
    # Plaza San Miguel: el texto solo trae el nombre comercial del centro.
    "20113126087": "MALL SERVICE S.A.C.",
    # Real Plaza: la forma societaria va en otra linea, debajo del RUC.
    "20478058137": "INMOBILIARIA PUERTA DEL SOL S.A.C.",
    "20421526258": "MULTIMERCADOS ZONALES SAC",
    "20523173716": "PARQUE LAMBRAMANI S.A.C.",
    "20550880041": "STRIP CENTERS DEL PERU S.A.C.",
}


def razon_social_por_ruc(ruc):
    """Razon social registrada para ese RUC, o None si no esta en la lista."""
    if not ruc:
        return None
    return RAZON_SOCIAL_POR_RUC.get(re.sub(r"\D", "", str(ruc)))


RUC_EN_NOMBRE_RE = re.compile(r"^((?:10|15|16|17|20)\d{9})(?=\D|$)")


def ruc_de_nombre_archivo(nombre):
    """RUC del emisor en el nombre del archivo que genera SUNAT.

    Los XML/PDF oficiales se llaman "20109072177_01_FA26-277696.pdf" o
    "20113126087-01-F002-43717.pdf": el RUC del emisor va siempre primero.
    """
    match = RUC_EN_NOMBRE_RE.search(str(nombre or ""))
    return match.group(1) if match else None
