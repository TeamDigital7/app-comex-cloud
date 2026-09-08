"""Pruebas de la lectura de comprobantes.

Cada prueba deja fijado un error real que se detecto leyendo facturas de
Contabilidad, con un trozo del comprobante que lo provocaba. Si alguna vuelve a
fallar es que el error ha vuelto.

Se ejecutan sin PDFs ni red:

    python -m unittest pruebas_contabilidad -v
"""
import unittest

import forus_ocr
import forus_tributario as tributario
from forus_comprobante import (
    agrupar_documentos,
    detect_numero_exterior,
    detect_numero_factura,
    detect_tipo_documento,
    es_documento_exterior,
)
from forus_parsing import (
    fechas_a_formato_local,
    find_date,
    find_money,
    parse_date,
    reparar_lineas_punteadas,
)
from modules.contabilidad import (
    ETIQUETAS_GRAVADAS,
    ETIQUETAS_TOTAL,
    conciliar_total,
    extraer_rucs,
)


# --- Trozos de comprobantes reales -----------------------------------------

FACTURA_CON_SUBTOTAL = """\
Cantidad Unidad de Medida Item Valor Unitario Descuento Total
1 Unidad Servicio de entrega en Lima Metropolitana S/3,022.72 S/3,022.72
BCP C.Cte 1932171482060 Detracciones:BN00-093-010833 Subtotal S/3,022.72
Op. gravada S/3,022.72
IGV (18.00%) S/544.09
Total S/3,566.81
"""

FACTURA_CENTRO_COMERCIAL = """\
R.U.C.: 20514020907
CENTRO COMERCIAL PLAZA NORTE S.A.C.
FACTURA ELECTRONICA
AV. TUPAC AMARU S/n Nº F105- 00207262
SEÑOR(ES) :PERU FORUS S.A. FECHA :2026-08-03
R.U.C. :20514811271 MONEDA :SOLES
Codigo Cantidad Unidad Descripcion Pv. Unit Importe
PD61 1.000 ZZ CONTRAPRESTACIÓN MÍNIMA 21,218.40 21,218.40
SON :VEINTICINCO MIL TREINTA Y SIETE CON 71/100 SOLES. Total Gravado 21,218.40
Total IGV 18% 3,819.31
Importe Total S/ 25,037.71
AGENTE DE RETENCION Art. No. Res sup N 037-2002 Sunat Representación impresa
OPERACIONES SUJETA AL SISTEMA DE PAGO DE
Autorizado mediante resolución: N°300-2014-SUNAT
OBLIGACIONES TRIBUTARIAS CON EL GOBIERNO CENTRAL
N° de Cuenta: Banco de la Nación 00-068-112354
"""

RECIBO_HONORARIOS = """\
CASTRO CUNEO DANILO MARTIN
R.U.C. 10099294260
RECIBO POR HONORARIOS ELECTRONICO
CAL. DIONISIO DERTEANO NRO. 1558 Nro: E001- 19
Recibí de: PERU FORUS S.A.
Identificado con RUC número 20514811271
Fecha de emisión 02 de Julio del 2026
Total por honorarios: 2,079.17
Retención (8 %) IR: (0.00)
Total Neto Recibido: 2,079.17 SOLES
"""

RECIBO_SERVICIOS = """\
Recibo :SB01-0645497702
Período :01/JUL/26 - 31/JUL/26
Fecha Emisión :01/AGO/2026
Total a Pagar :S/ 159.89
DATOS DEL CLIENTE
Razón Social : PERU FORUS S.A.
R.U.C. : 20514811271
Cargos del Mes 135.50
I.G.V. 18% 24.39
America Móvil Perú S.A.C. Av. Nicolas Arriola 480
R.U.C.: 20467534026 Urb. Santa Catalina, La Victoria - Lima
"""

FACTURA_EXTERIOR = """\
Meta Platforms Ireland Limited
TAX INVOICE INVOICE
VAT: IE9692928F
Invoice #: 254084766
Invoice Date: 01-Aug-2026
BILL TO: PERU FORUS S.A.
RUC: 20514811271
Invoice Total: 29,775.16
"""


class LecturaDeImportes(unittest.TestCase):
    """El error que hacia bajar las facturas por su valor sin IGV."""

    def test_subtotal_no_es_el_importe_total(self):
        total = find_money(FACTURA_CON_SUBTOTAL, ETIQUETAS_TOTAL)
        self.assertEqual(total, 3566.81)

    def test_el_encabezado_no_se_lleva_el_numero_de_la_linea_siguiente(self):
        # "... Descuento Total" cierra la linea: detras no hay ningun importe,
        # y la cantidad del item de abajo no vale como total.
        self.assertIsNone(find_money(
            "Cantidad Unidad Item Valor Unitario Descuento Total\n"
            "1 Unidad Servicio de entrega S/3,022.72",
            ["TOTAL"],
        ))

    def test_total_gravado_no_es_el_importe_total(self):
        self.assertEqual(
            find_money(FACTURA_CENTRO_COMERCIAL, ETIQUETAS_TOTAL), 25037.71
        )

    def test_la_base_se_lee_de_total_gravado(self):
        self.assertEqual(
            find_money(FACTURA_CENTRO_COMERCIAL, ETIQUETAS_GRAVADAS), 21218.40
        )

    def test_sub_total_no_cuenta_como_total(self):
        self.assertEqual(
            find_money("SUB TOTAL S/ 23,234.01\nI.G.V S/4,182.12\nTOTAL S/27,416.13",
                       ETIQUETAS_TOTAL),
            27416.13,
        )

    def test_el_porcentaje_no_se_confunde_con_el_importe(self):
        self.assertEqual(find_money("IGV (18%): 180.00", ["IGV"]), 180.00)


class ComprobacionDeTotales(unittest.TestCase):
    """La red de seguridad: base + IGV tiene que dar el total."""

    def test_corrige_el_total_cuando_se_leyo_la_base(self):
        total, aviso = conciliar_total(3022.72, 3022.72, 544.09, 0)
        self.assertEqual(total, 3566.81)
        self.assertIn("base sin IGV", aviso)

    def test_respeta_el_total_cuando_cuadra(self):
        total, aviso = conciliar_total(3566.81, 3022.72, 544.09, 0)
        self.assertEqual(total, 3566.81)
        self.assertIsNone(aviso)

    def test_avisa_cuando_no_cuadra_ni_es_la_base(self):
        total, aviso = conciliar_total(9999.00, 3022.72, 544.09, 0)
        self.assertEqual(total, 9999.00)
        self.assertIn("no cuadra", aviso)


class NumeracionDeComprobantes(unittest.TestCase):

    def test_recibo_por_honorarios_de_dos_cifras(self):
        # "E001-19" se descartaba por exigir tres cifras de correlativo, y el
        # archivo entero salia como ilegible.
        self.assertEqual(detect_numero_factura("Nro: E001- 19"), "E001-19")

    def test_recibo_de_servicios_publicos(self):
        self.assertEqual(
            detect_numero_factura("Recibo :SB01-0645497702"), "SB01-0645497702"
        )

    def test_factura_con_simbolo_de_numero(self):
        self.assertEqual(
            detect_numero_factura("AV. TUPAC AMARU S/n Nº F105- 00206001"),
            "F105-00206001",
        )

    def test_una_direccion_no_es_un_numero_de_factura(self):
        self.assertIsNone(detect_numero_factura("AV. JAVIER PRADO ESTE 4200"))

    def test_numero_del_exterior(self):
        self.assertEqual(detect_numero_exterior("Invoice #: 254084766"), "254084766")

    def test_la_direccion_no_da_el_numero_del_exterior(self):
        self.assertIsNone(detect_numero_exterior(
            "Dirección: CAL. AMADOR MERINO REYNA NRO. 267 INT. 1101"
        ))

    def test_el_ruc_del_cliente_no_es_el_numero_del_exterior(self):
        self.assertNotEqual(
            detect_numero_exterior("Invoice No.: 6012482 VAT No.:20514811271"),
            "20514811271",
        )


class TipoDeComprobante(unittest.TestCase):

    def test_recibo_por_honorarios(self):
        self.assertEqual(
            detect_tipo_documento(RECIBO_HONORARIOS), "Recibo por Honorarios"
        )

    def test_factura(self):
        self.assertEqual(detect_tipo_documento(FACTURA_CENTRO_COMERCIAL), "Factura")

    def test_comprobante_del_exterior(self):
        self.assertTrue(es_documento_exterior(FACTURA_EXTERIOR))

    def test_una_factura_peruana_no_es_del_exterior(self):
        self.assertFalse(es_documento_exterior(FACTURA_CENTRO_COMERCIAL))


class PaginasDelDocumento(unittest.TestCase):

    def test_la_segunda_hoja_es_continuacion_y_no_anexo(self):
        # Muchas facturas imprimen el pie de totales en su segunda hoja, que
        # repite titulo y numero. Tratarla como anexo perdia los importes.
        paginas = [
            {"numero": 1, "texto": "FACTURA ELECTRONICA\nNo. FE01-00005368"},
            {"numero": 2, "texto": "FACTURA ELECTRONICA\nNo. FE01-00005368\n"
                                   "Total S/29,068.47"},
        ]
        documentos = agrupar_documentos(paginas)
        self.assertEqual(len(documentos), 1)
        self.assertEqual(len(documentos[0]["paginas"]), 2)

    def test_la_orden_de_compra_adjunta_no_es_un_comprobante(self):
        paginas = [
            {"numero": 1, "texto": RECIBO_HONORARIOS},
            {"numero": 2, "texto": "ORDEN DE COMPRA Nro. 39334\nSub Total: $2.000"},
        ]
        documentos = agrupar_documentos(paginas)
        self.assertEqual(len(documentos), 1)
        self.assertEqual(documentos[0]["numero"], "E001-19")
        self.assertEqual([p["numero"] for p in documentos[0]["anexos"]], [2])


class IdentificacionDeRuc(unittest.TestCase):

    def test_el_ruc_del_cliente_no_pasa_por_el_del_proveedor(self):
        emisor, cliente = extraer_rucs(RECIBO_SERVICIOS)
        self.assertEqual(emisor, "20467534026")
        self.assertEqual(cliente, "20514811271")


class Fechas(unittest.TestCase):

    def test_el_rotulo_de_la_columna_no_tapa_la_fecha(self):
        texto = ("FECHA DE EMISIÓN (DD/MM/AA)\n"
                 "Fecha y hora de emisión: 2026-08-03T23:51:02")
        self.assertEqual(find_date(texto, ["FECHA DE EMISION", "FECHA"]), "2026-08-03")

    def test_fecha_bajo_su_rotulo(self):
        texto = "FECHA DE EMISIÓN FORMA Y CONDICIÓN DE PAGO\n31/07/2026 CREDITO 30 DIAS"
        self.assertEqual(find_date(texto, ["FECHA DE EMISION"]), "2026-07-31")

    def test_fecha_de_otra_cosa_descartada(self):
        self.assertIsNone(find_date("Fecha Emisor: 25/02/21", ["FECHA"],
                                    excluir=("EMISOR",)))

    def test_formatos(self):
        self.assertEqual(parse_date("02 de Julio del 2026"), "2026-07-02")
        self.assertEqual(parse_date("01-Aug-2026"), "2026-08-01")
        self.assertEqual(parse_date("31 jul 2026"), "2026-07-31")
        self.assertEqual(parse_date("2026-08-03T23:51:02"), "2026-08-03")


class FormatoDeFecha(unittest.TestCase):
    """En pantalla y en el Excel las fechas van como se leen: dia/mes/año."""

    def test_las_fechas_salen_como_dia_mes_anio(self):
        filas = fechas_a_formato_local([
            {"FECHA": "2026-07-21", "Fecha Vencimiento": "2026-08-30"},
        ])
        self.assertEqual(filas[0]["FECHA"], "21/07/2026")
        self.assertEqual(filas[0]["Fecha Vencimiento"], "30/08/2026")

    def test_no_toca_lo_que_no_es_una_fecha(self):
        filas = fechas_a_formato_local([
            {"DOCUMENTO": "F105-00206001", "IMPORTE": 129.85, "GLOSA": None,
             "RUC": "20514020907"},
        ])
        self.assertEqual(filas[0]["DOCUMENTO"], "F105-00206001")
        self.assertEqual(filas[0]["IMPORTE"], 129.85)
        self.assertIsNone(filas[0]["GLOSA"])
        self.assertEqual(filas[0]["RUC"], "20514020907")

    def test_una_fecha_ya_convertida_se_queda_igual(self):
        filas = fechas_a_formato_local([{"FECHA": "21/07/2026"}])
        self.assertEqual(filas[0]["FECHA"], "21/07/2026")


class TextoPunteado(unittest.TestCase):

    def test_recupera_el_campo_y_conserva_los_decimales(self):
        linea = (".F.e..c.h..a. .d.e.. .la.. f.a..c.t.u.r.a......3..1.. j.u.l. "
                 ".2.0..2.6.... Importe total adeudado en USD USD 31,089.51")
        reparada = reparar_lineas_punteadas(linea)
        self.assertIn("31 jul 2026", reparada)
        self.assertIn("31,089.51", reparada)

    def test_no_toca_la_razon_social(self):
        linea = "CENTRO COMERCIAL PLAZA NORTE S.A.C."
        self.assertEqual(reparar_lineas_punteadas(linea), linea)


class Detraccion(unittest.TestCase):

    def test_la_leyenda_partida_en_dos_columnas_se_reconoce(self):
        sujeta, _, _ = tributario.detectar_detraccion(FACTURA_CENTRO_COMERCIAL)
        self.assertTrue(sujeta)

    def test_detraccion_de_arrendamiento_al_diez_por_ciento(self):
        resultado = tributario.evaluar(
            25037.71, "PEN", None, FACTURA_CENTRO_COMERCIAL,
            glosa="CONTRAPRESTACIÓN MÍNIMA", tipo_documento="Factura",
        )
        self.assertEqual(resultado["Afecto a"], tributario.DETRACCION)
        self.assertEqual(resultado["% Aplicado"], 10.0)
        self.assertEqual(resultado["Monto Detraccion/Retencion"], 2504.0)

    def test_por_debajo_del_minimo_no_hay_detraccion(self):
        # La misma factura, con la misma leyenda impresa, pero de S/ 129.85.
        resultado = tributario.evaluar(
            129.85, "PEN", None, FACTURA_CENTRO_COMERCIAL,
            glosa="REEMBOLSO ARBITRIOS", tipo_documento="Factura",
        )
        self.assertEqual(resultado["Afecto a"], tributario.NO_AFECTO)
        self.assertEqual(resultado["Monto Detraccion/Retencion"], 0.0)

    def test_manda_el_porcentaje_impreso(self):
        texto = "OPERACIÓN SUJETA A DETRACCIÓN\nDetracción(4%):S/143.00"
        resultado = tributario.evaluar(3566.81, "PEN", None, texto,
                                       monto_detraccion=143.0, tipo_documento="Factura")
        self.assertEqual(resultado["Afecto a"], tributario.DETRACCION)
        self.assertEqual(resultado["Monto Detraccion/Retencion"], 143.0)
        self.assertEqual(resultado["Neto a Pagar"], 3423.81)


class RecibosPorHonorarios(unittest.TestCase):
    """Renta de cuarta categoria: nunca detraccion ni retencion del IGV."""

    def test_nunca_lleva_la_retencion_del_igv(self):
        resultado = tributario.evaluar(
            918.00, "PEN", None, RECIBO_HONORARIOS,
            tipo_documento=tributario.RECIBO_HONORARIOS, monto_retencion_renta=0.0,
        )
        self.assertNotEqual(resultado["Afecto a"], tributario.RETENCION)
        self.assertEqual(resultado["Monto Detraccion/Retencion"], 0.0)

    def test_con_constancia_de_suspension_no_se_retiene(self):
        resultado = tributario.evaluar(
            2000.00, "PEN", None, RECIBO_HONORARIOS,
            tipo_documento=tributario.RECIBO_HONORARIOS, monto_retencion_renta=0.0,
            texto_anexos="RENTA - 4ta CATEGORIA : AUTORIZADO Formulario 1609",
        )
        self.assertEqual(resultado["Afecto a"], tributario.NO_AFECTO)

    def test_cero_sin_constancia_y_por_encima_del_minimo_se_revisa(self):
        resultado = tributario.evaluar(
            2079.17, "PEN", None, RECIBO_HONORARIOS,
            tipo_documento=tributario.RECIBO_HONORARIOS, monto_retencion_renta=0.0,
        )
        self.assertEqual(resultado["Afecto a"], tributario.REVISAR)

    def test_retencion_impresa_se_respeta(self):
        resultado = tributario.evaluar(
            3000.00, "PEN", None, RECIBO_HONORARIOS,
            tipo_documento=tributario.RECIBO_HONORARIOS, monto_retencion_renta=240.0,
        )
        self.assertEqual(resultado["Afecto a"], tributario.RETENCION_RENTA)
        self.assertEqual(resultado["Neto a Pagar"], 2760.0)


class OtrosComprobantes(unittest.TestCase):

    def test_recibo_de_servicios_publicos_fuera_del_regimen_de_retencion(self):
        resultado = tributario.evaluar(
            1982.82, "PEN", None, RECIBO_SERVICIOS,
            tipo_documento="Recibo de Servicios",
        )
        self.assertEqual(resultado["Afecto a"], tributario.NO_AFECTO)
        self.assertEqual(resultado["Monto Detraccion/Retencion"], 0.0)

    def test_comprobante_del_exterior_sin_detraccion_ni_retencion(self):
        resultado = tributario.evaluar(
            29775.16, "USD", None, FACTURA_EXTERIOR, exterior=True,
        )
        self.assertEqual(resultado["Afecto a"], tributario.NO_AFECTO)
        self.assertIn("exterior", resultado["Motivo"])

    def test_factura_normal_sobre_el_umbral_lleva_retencion(self):
        resultado = tributario.evaluar(
            5000.00, "PEN", None, "FACTURA ELECTRONICA\nImporte Total S/ 5,000.00",
            tipo_documento="Factura", igv=762.71,
        )
        self.assertEqual(resultado["Afecto a"], tributario.RETENCION)
        self.assertEqual(resultado["Monto Detraccion/Retencion"], 150.0)


class SalidaDelOcr(unittest.TestCase):
    """El motor devuelve objetos distintos segun la version y la pagina."""

    def test_pagina_en_blanco_no_rompe_la_lectura(self):
        class ResultadoVacio:
            boxes = None
            txts = None
            scores = None

        self.assertEqual(forus_ocr._bloques(ResultadoVacio()), [])

    def test_admite_campos_de_numpy(self):
        class Resultado:
            boxes = [[(0, 0), (10, 0), (10, 5), (0, 5)]]
            txts = ("IMPORTE TOTAL 100.00",)
            scores = (0.99,)

        bloques = forus_ocr._bloques(Resultado())
        self.assertEqual(len(bloques), 1)
        self.assertEqual(forus_ocr._agrupar_en_lineas(bloques), "IMPORTE TOTAL 100.00")

    def test_formato_antiguo_de_lista(self):
        antiguo = [([(0, 0), (10, 0), (10, 5), (0, 5)], "TOTAL 1.00", 0.9)]
        self.assertEqual(len(forus_ocr._bloques(antiguo)), 1)


if __name__ == "__main__":
    unittest.main()
