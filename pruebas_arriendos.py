"""Pruebas de lectura de facturas de arriendo.

Se ejecuta con `python pruebas_arriendos.py`. No hace falta instalar nada
aparte de lo que ya usa la aplicacion.

Cada caso es el texto tal como sale de una factura de verdad, con lo que tiene
que salir en el control al lado. Sirve para dos cosas: comprobar que un cambio
no rompe el formato de otro centro comercial, y dejar por escrito como factura
cada uno. Para anadir un centro comercial nuevo, se copia el texto que
devuelve el PDF y se escribe el resultado esperado.
"""
import sys

from forus_comprobante import extraer_lineas_detalle
from modules.arriendos import construir_filas_control, extraer_cabecera


# --------------------------------------------------------------- facturas

MALL_SERVICE = """RUC: 20113126087
FACTURA
CENTRO COMERCIAL PLAZA SAN MIGUEL ELECTRONICA
AV. LA MARINA N 2000 URB. PANDO
F002-43717
SAN MIGUEL - LIMA - LIMA
Cliente : PERU FORUS S.A. Nombre comercial : PARFOIS
RUC : 20514811271 Moneda : SOL
Fecha de Emision : 02-09-2026 Fecha de Vencimiento : 17-09-2026
Local : S49A
Precio Valor Precio Valor
Descripcion Cantidad
Unitario Unitario Total Total
Mantenimiento mes Septiembre 2026 1 708.00 600.00 708.00 600.00
Total Valor de Venta - Operaciones Gravadas: S/ 600.00
IGV: S/ 108.00
Importe Total: S/ 708.00
"""

# Tres locales en una sola factura y la tabla con el precio ya con IGV.
CENCOSUD_GASTOS_COMUNES = """OFICINA: CALLE AUGUSTO ANGULO N 130 INT 251 R.U.C. 20604119066
URB. SAN ANTONIO - MIRAFLORES - LIMA - LIMA
FACTURA ELECTRONICA
FA16 N 00075700
SENORES :PERU FORUS S.A. CODIGO CLIENTE:3000002050
RUC :RUC 20514811271 ORGANIZACION DE VENTAS:P016
CONDICIONES:Credito - 0005 5 Dias sin DPP TIPO DE MONEDA:SOL
CODIGO DESCRIPCION UM CANTIDAD P. UNITARIO P. VENTA
ZPGJ Gasto Comun fijo - ST 1 4,344.03 4,344.03
*01.09.2026-30.09.2026-Gastos Fijos - LC Local S2012
ZPGJ Gasto Comun fijo - ST 1 3,683.06 3,683.06
*01.09.2026-30.09.2026-Gastos Fijos - LC Local S2016
ZPGJ Gasto Comun fijo - ST 1 5,032.00 5,032.00
*01.09.2026-30.09.2026-Gastos Fijos - LC Local S3024
SON: TRECE MIL CINCUENTA Y NUEVE Y 10 /100 SOLES OP. GRAVADA S/ 11,067.03
OP. INAFECTA S/ 0.00
OP. EXONERADA S/ 0.00
I.G.V. S/ 1,992.07
IMPORTE TOTAL S/ 13,059.10
"""

# Exonerada: aqui no hay IGV que descontar y los importes van tal cual.
CENCOSUD_INTERESES = """OFICINA: CALLE AUGUSTO ANGULO N 130 INT 251 R.U.C. 20604119066
FACTURA ELECTRONICA
FA16 N 00076176
RUC :RUC 20514811271 ORGANIZACION DE VENTAS:P016
CONDICIONES:Credito - 0005 5 Dias sin DPP TIPO DE MONEDA:SOL
CODIGO DESCRIPCION UM CANTIDAD P. UNITARIO P. VENTA
ZINT Intereses Punitorios - ST 1 14.98 14.98
*01.08.2026-31.08.2026-Intereses - LC Local S2012
ZINT Intereses Punitorios - ST 1 17.19 17.19
*01.08.2026-31.08.2026-Intereses - LC Local S2016
SON: TREINTA Y DOS Y 17 /100 SOLES OP. GRAVADA S/ 0.00
OP. INAFECTA S/ 0.00
OP. EXONERADA S/ 32.17
I.G.V. S/ 0.00
IMPORTE TOTAL S/ 32.17
"""

# La segunda linea es el calculo de la renta variable, no un cobro aparte.
PUERTA_DEL_SOL = """INMOBILIARIA PUERTA DEL SOL
R.U.C. 20478058137
S.A.C
FACTURA ELECTRONICA
F001 N 00038134
SENOR PERU FORUS S.A.
TIPO DE MONEDA : SOL
DIRECCION FECHA DE EMISION : 08/09/2026
DOC. IDENTIDAD RUC 20514811271 NRO. INTERNO : 7800038908
CODIGO CLIENTE ZONA NUMERO PEDIDO ORDEN DE COMPRA LOCAL COMERCIAL CONDICIONES DE PAGO
2051481127 6100038931 LC-163A/161 A 10 Dias
VALOR DE VENTA VALOR DE VALOR DE VENTA
CODIGO DESCRIPCION DEL ARTICULO CANTIDAD
UNITARIO DESCUENTO TOTAL
90000251 RENTA VARIABLE Real Plaza Cusco 1 984.23 0.00 984.23
AGOSTO 2026 - RENTA VARIABLE - %RV: 8.50 - VTAS: 253525.77 .
OP. EXONERADA OP. INAFECTA OP. GRAVADA TOT. DSCTO. I.S.C. I.G.V. 18% IMPORTE TOTAL
S/ 984.23 S/ 177.16 S/ 1,161.39
"""

STRIP_CENTERS = """R.U.C. 20550880041
FACTURA ELECTRONICA
PARQUE LA MOLINA
F005 N 00004033
STRIP CENTERS DEL PERU S.A.C.
FECHA :03/09/2026 N. CONTRATO:0000000000172
SENOR (ES) :PERU FORUS S.A . R.U.C.:20514811271
LOCAL :LC405 COLUMBIA
Moneda Fecha Venc. Hecho Por Revisado Por Autorizado Por
PEN 2026-09-13
VALOR
DESCRIPCION UM CANT. DSCTO VALOR NETO
UNITARIO
RENTA MINIMA 01.09.2026 - 30.09.2026 LOCALES 1 2,315.60 0.00 2,315.60
FONDO DE PROMOCION 01.09.2026 - 30.09.2026 LOCALES 1 231.56 0.00 231.56
SON: TRES MIL CINCO y 65/100 SOLES. OP. GRAVADA S/ 2,547.16
I.G.V. 18% S/ 458.49
IMPORTE TOTAL S/ 3,005.65
"""

# Una sola columna de importes: ninguna linea se puede descartar por traer un
# numero solo, que es como se reconocen las continuaciones en las demas.
UNA_SOLA_COLUMNA = """PONTIFICIA UNIVERSIDAD CATOLICA DEL PERU
R.U.C. 20155945860
FACTURA ELECTRONICA
F036 N 00042130
SENOR (ES) : PERU FORUS S.A.
LOCAL : 99
Descripcion Importe
Energia mes de Agosto 2026 929.20
Agua mes de Agosto 2026 120.80
OP. GRAVADA S/ 1,050.00
I.G.V. 18% S/ 189.00
IMPORTE TOTAL S/ 1,239.00
"""


def control_de(texto):
    cabecera = extraer_cabecera(texto, 1)
    filas = extraer_lineas_detalle(texto)
    control, _, _ = construir_filas_control(cabecera, filas, "prueba.pdf", texto)
    return cabecera, control


def resumen(control):
    """Lo que se mira de cada fila: tienda, mes, concepto e importe."""
    return [
        (fila["Tienda"], fila["MES"], fila["Concepto"], fila["SOLES"])
        for fila in control
    ]


CASOS = [
    (
        "Plaza San Miguel: mantenimiento es gasto comun y el nombre sale del RUC",
        MALL_SERVICE,
        "MALL SERVICE S.A.C.",
        [("S49A", "SETIEMBRE", "GASTOS COMUNES", 600.00)],
    ),
    (
        "Cencosud: un local por linea y el importe sin IGV",
        CENCOSUD_GASTOS_COMUNES,
        "CENCOSUD PERU SHOPPING S.A.",
        [
            ("S2012", "SETIEMBRE", "GASTOS COMUNES", 3681.38),
            ("S2016", "SETIEMBRE", "GASTOS COMUNES", 3121.24),
            ("S3024", "SETIEMBRE", "GASTOS COMUNES", 4264.41),
        ],
    ),
    (
        "Cencosud: los intereses van exonerados y no se les descuenta nada",
        CENCOSUD_INTERESES,
        "CENCOSUD PERU SHOPPING S.A.",
        [
            ("S2012", "AGOSTO", "INTERESES", 14.98),
            ("S2016", "AGOSTO", "INTERESES", 17.19),
        ],
    ),
    (
        "Real Plaza: el detalle del calculo no es una segunda linea de cobro",
        PUERTA_DEL_SOL,
        "INMOBILIARIA PUERTA DEL SOL S.A.C.",
        [("LC-163A/161", "AGOSTO", "RENTA VARIABLE", 984.23)],
    ),
    (
        "Strip Centers: renta minima y fondo de promociones, valor neto",
        STRIP_CENTERS,
        "STRIP CENTERS DEL PERU S.A.C.",
        [
            ("LC405 COLUMBIA", "SETIEMBRE", "RENTA MINIMA", 2315.60),
            ("LC405 COLUMBIA", "SETIEMBRE", "FONDO DE PROMOCIONES", 231.56),
        ],
    ),
    (
        "Tabla de una sola columna: no se pierde ninguna linea",
        UNA_SOLA_COLUMNA,
        "PONTIFICIA UNIVERSIDAD CATOLICA DEL PERU",
        [
            ("99", "AGOSTO", "ENERGIA", 929.20),
            ("99", "AGOSTO", "AGUA", 120.80),
        ],
    ),
]


def main():
    fallos = 0

    for titulo, texto, razon_social, esperado in CASOS:
        cabecera, control = control_de(texto)
        obtenido = resumen(control)

        if cabecera.get("RAZON SOCIAL") != razon_social:
            fallos += 1
            print(f"FALLA  {titulo}")
            print(f"       RAZON SOCIAL esperada: {razon_social}")
            print(f"       RAZON SOCIAL leida:    {cabecera.get('RAZON SOCIAL')}")
            continue

        if obtenido != esperado:
            fallos += 1
            print(f"FALLA  {titulo}")
            for linea in esperado:
                print(f"       esperado: {linea}")
            for linea in obtenido:
                print(f"       leido:    {linea}")
            continue

        print(f"OK     {titulo}")

    print()
    print(f"{len(CASOS) - fallos} de {len(CASOS)} casos correctos")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
