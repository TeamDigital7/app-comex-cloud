"""Modulo Recursos Humanos: lectura de boletas de pago y salida a Excel.

Una boleta peruana se organiza en tres bloques (ingresos, descuentos y aportes
del empleador) y cada empresa nombra los conceptos a su manera. Por eso se lee
en dos pasadas: primero los totales por etiqueta, que son fiables, y despues
cada linea de concepto segun el bloque en que aparece. Todos los conceptos, se
reconozcan o no, quedan en la hoja Conceptos para poder verificarlos.

Los archivos se procesan en memoria y no se guardan en ningun lado.
"""
import io
import re

import pandas as pd
import pdfplumber
import streamlit as st

from forus_parsing import (
    collapse_spaces,
    cuadra,
    faltantes,
    fechas_a_formato_local,
    find_date,
    find_dni,
    find_int,
    find_label_value,
    find_money,
    find_ruc,
    fix_mojibake,
    normalize,
    parse_amount,
    split_lines,
)
from forus_ui import (
    close_card,
    format_file_size,
    open_card,
    render_banner,
    render_benefits,
    render_empty,
    render_file_list,
    render_hero,
    render_pipeline,
    render_result_grid,
    render_rules,
    render_stat_grid,
)
from modules import alquileres

# El modulo tiene dos pantallas y se elige en el panel lateral.
PROCESOS = {
    "boletas": "Boletas de pago",
    "alquileres": "Facturas de alquiler",
}

INGRESOS = [
    "Sueldo Basico",
    "Asignacion Familiar",
    "Horas Extras",
    "Bonificaciones",
    "Gratificacion",
    "Vacaciones",
    "Comisiones",
    "Movilidad",
    "Otros Ingresos",
]

DESCUENTOS = [
    "AFP Aporte Obligatorio",
    "AFP Comision",
    "AFP Prima Seguro",
    "ONP",
    "Renta Quinta",
    "Adelantos",
    "Prestamos",
    "Descuento Judicial",
    "Tardanzas e Inasistencias",
    "Otros Descuentos",
]

APORTES = [
    "EsSalud",
    "SCTR",
    "Senati",
    "Vida Ley",
    "Otros Aportes",
]

BOLETA_COLUMNS = (
    [
        "PDF File",
        "Pagina Inicial",
        "Paginas",
        "Periodo",
        "RUC Empleador",
        "Razon Social Empleador",
        "Codigo Trabajador",
        "Trabajador",
        "DNI",
        "Cargo",
        "Area",
        "Fecha Ingreso",
        "Situacion",
        "Regimen Pensionario",
        "CUSPP",
        "Dias Trabajados",
        "Dias No Laborados",
        "Dias Subsidiados",
        "Horas Trabajadas",
    ]
    + INGRESOS
    + ["Total Ingresos"]
    + DESCUENTOS
    + ["Total Descuentos"]
    + APORTES
    + [
        "Total Aportes",
        "Neto a Pagar",
        "Cuadra Neto",
        "Observaciones",
    ]
)

CONCEPTO_COLUMNS = [
    "PDF File",
    "Periodo",
    "DNI",
    "Trabajador",
    "Pagina",
    "Bloque",
    "Codigo",
    "Concepto",
    "Campo Asignado",
    "Monto",
]

SUMMARY_COLUMNS = ["Metric", "Value"]

AUDIT_COLUMNS = ["PDF File", "Pagina", "Rol", "Trabajador", "DNI", "Detalle"]

CAMPOS_OBLIGATORIOS = ["Trabajador", "DNI", "Periodo", "Neto a Pagar"]

MARCADORES_BOLETA = (
    "BOLETA DE PAGO",
    "BOLETA DE REMUNERACION",
    "BOLETA DE REMUNERACIONES",
    "HOJA DE LIQUIDACION",
    "LIQUIDACION DE REMUNERACIONES",
)

# Encabezados que abren cada bloque de la boleta.
ENCABEZADOS_BLOQUE = [
    ("APORTES DEL EMPLEADOR", "aportes"),
    ("APORTACIONES DEL EMPLEADOR", "aportes"),
    ("APORTES EMPLEADOR", "aportes"),
    ("CONTRIBUCIONES", "aportes"),
    ("APORTACIONES", "aportes"),
    ("APORTES", "aportes"),
    ("DESCUENTOS", "descuentos"),
    ("RETENCIONES", "descuentos"),
    ("INGRESOS", "ingresos"),
    ("REMUNERACIONES", "ingresos"),
    ("HABERES", "ingresos"),
]

# Los alias mas especificos van primero: "COMISION AFP" tiene que ganarle a
# "COMISION", que en el bloque de ingresos significa otra cosa.
MAPA_INGRESOS = [
    ("Asignacion Familiar", ["ASIGNACION FAMILIAR", "ASIG. FAMILIAR", "ASIG FAMILIAR"]),
    ("Horas Extras", ["HORAS EXTRAS", "HORA EXTRA", "H. EXTRAS", "SOBRETIEMPO"]),
    ("Gratificacion", ["GRATIFICACION", "GRATIF."]),
    ("Vacaciones", ["VACACIONES", "VACACIONAL"]),
    ("Movilidad", ["MOVILIDAD"]),
    ("Comisiones", ["COMISION"]),
    ("Bonificaciones", ["BONIFICACION", "BONO"]),
    ("Sueldo Basico", ["SUELDO BASICO", "REMUNERACION BASICA", "JORNAL BASICO", "BASICO", "SUELDO"]),
]

MAPA_DESCUENTOS = [
    ("AFP Prima Seguro", ["PRIMA DE SEGURO", "PRIMA SEGURO", "SEGURO INVALIDEZ"]),
    ("AFP Comision", ["COMISION SOBRE FLUJO", "COMISION VARIABLE", "COMISION MIXTA", "COMISION AFP", "COMISION"]),
    ("AFP Aporte Obligatorio", ["APORTE OBLIGATORIO", "APORTACION OBLIGATORIA", "FONDO DE PENSIONES", "AFP"]),
    ("ONP", ["ONP", "SISTEMA NACIONAL DE PENSIONES"]),
    ("Renta Quinta", ["RENTA DE QUINTA", "QUINTA CATEGORIA", "IMPUESTO A LA RENTA", "RENTA 5TA"]),
    ("Adelantos", ["ADELANTO", "ANTICIPO"]),
    ("Prestamos", ["PRESTAMO"]),
    ("Descuento Judicial", ["JUDICIAL", "ALIMENTOS", "RETENCION JUDICIAL"]),
    ("Tardanzas e Inasistencias", ["TARDANZA", "INASISTENCIA", "FALTAS", "DESCUENTO POR FALTA"]),
]

MAPA_APORTES = [
    ("EsSalud", ["ESSALUD", "ES SALUD", "SEGURO SOCIAL", "REGIMEN CONTRIBUTIVO"]),
    ("SCTR", ["SCTR", "RIESGO"]),
    ("Senati", ["SENATI"]),
    ("Vida Ley", ["VIDA LEY", "SEGURO DE VIDA"]),
]

MAPA_POR_BLOQUE = {
    "ingresos": (MAPA_INGRESOS, "Otros Ingresos"),
    "descuentos": (MAPA_DESCUENTOS, "Otros Descuentos"),
    "aportes": (MAPA_APORTES, "Otros Aportes"),
}

# Lineas que parecen concepto pero son totales o cabeceras.
LINEAS_NO_CONCEPTO = (
    "TOTAL", "NETO", "SUB TOTAL", "SUBTOTAL", "SON ", "PERIODO", "FECHA",
    "DIAS", "HORAS", "CONCEPTO", "CODIGO", "DESCRIPCION", "MONTO", "IMPORTE",
    "TRABAJADOR", "EMPLEADOR", "RUC", "DNI", "CARGO", "AREA", "REGIMEN",
    "CUSPP", "AFILIADO", "FIRMA", "PAGINA", "BOLETA",
)

CONCEPTO_RE = re.compile(r"^(?P<cuerpo>.+?)\s+(?P<monto>-?\(?\d[\d.,]*\)?-?)$")
CODIGO_RE = re.compile(r"^(?P<codigo>\d{2,6})\s+(?P<resto>.+)$")


def detectar_bloque(linea):
    """Si la linea es un encabezado de bloque, devuelve cual. Si no, None."""
    plano = normalize(collapse_spaces(linea) or "")
    if len(plano) > 45:
        return None
    for etiqueta, bloque in ENCABEZADOS_BLOQUE:
        if etiqueta in plano:
            return bloque
    return None


def asignar_campo(descripcion, bloque):
    """Campo fijo al que corresponde un concepto, segun el bloque donde aparece."""
    mapa, por_defecto = MAPA_POR_BLOQUE.get(bloque, (None, None))
    if not mapa:
        return None

    plano = normalize(descripcion or "")
    for campo, alias in mapa:
        if any(nombre in plano for nombre in alias):
            return campo
    return por_defecto


def parse_concepto(linea):
    """Separa una linea de boleta en (codigo, descripcion, monto)."""
    texto = collapse_spaces(linea)
    if not texto:
        return None

    plano = normalize(texto)
    if plano.startswith(LINEAS_NO_CONCEPTO):
        return None

    match = CONCEPTO_RE.match(texto)
    if not match:
        return None

    monto = parse_amount(match.group("monto"))
    if monto is None:
        return None

    cuerpo = collapse_spaces(match.group("cuerpo")) or ""
    codigo = None
    match_codigo = CODIGO_RE.match(cuerpo)
    if match_codigo:
        codigo = match_codigo.group("codigo")
        cuerpo = collapse_spaces(match_codigo.group("resto")) or ""

    # Sin letras suficientes no es un concepto, es una fila de numeros.
    if len(re.findall(r"[A-Za-z]", cuerpo)) < 4:
        return None

    return codigo, cuerpo, monto


def extraer_conceptos(texto):
    """Recorre la boleta bloque por bloque y devuelve todos los conceptos."""
    conceptos = []
    bloque = None

    for linea in split_lines(texto):
        nuevo_bloque = detectar_bloque(linea)
        if nuevo_bloque:
            bloque = nuevo_bloque
            continue

        parseado = parse_concepto(linea)
        if not parseado:
            continue

        codigo, descripcion, monto = parseado
        conceptos.append({
            "Codigo": codigo,
            "Concepto": descripcion,
            "Monto": monto,
            "Bloque": bloque,
            "Campo Asignado": asignar_campo(descripcion, bloque),
        })

    return conceptos


def extraer_datos_trabajador(texto):
    return {
        "Periodo": find_label_value(texto, [
            "PERIODO DE PAGO", "PERIODO", "MES DE PAGO", "REMUNERACION DEL MES DE",
            "MES", "CORRESPONDIENTE A",
        ], max_chars=30),
        "RUC Empleador": find_ruc(texto, ["RUC DEL EMPLEADOR", "R.U.C.", "RUC"]),
        "Razon Social Empleador": find_label_value(texto, [
            "RAZON SOCIAL", "EMPLEADOR", "EMPRESA",
        ]),
        "Codigo Trabajador": find_label_value(texto, [
            "CODIGO DE TRABAJADOR", "COD. TRABAJADOR", "CODIGO TRABAJADOR",
            "FICHA", "LEGAJO",
        ], max_chars=25),
        "Trabajador": find_label_value(texto, [
            "APELLIDOS Y NOMBRES", "NOMBRE DEL TRABAJADOR", "TRABAJADOR",
            "APELLIDOS Y NOMBRE", "NOMBRES Y APELLIDOS", "COLABORADOR",
        ]),
        "DNI": find_dni(texto, [
            "DNI", "D.N.I.", "DOCUMENTO DE IDENTIDAD", "N. DOCUMENTO",
            "NRO. DOCUMENTO", "DOC. IDENTIDAD",
        ]),
        "Cargo": find_label_value(texto, ["CARGO", "OCUPACION", "PUESTO"], max_chars=45),
        "Area": find_label_value(texto, [
            "AREA", "CENTRO DE COSTO", "SECCION", "DEPARTAMENTO", "TIENDA",
        ], max_chars=45),
        "Fecha Ingreso": find_date(texto, [
            "FECHA DE INGRESO", "FECHA INGRESO", "F. INGRESO",
        ]),
        "Situacion": find_label_value(texto, [
            "SITUACION", "ESTADO", "TIPO DE CONTRATO", "CONDICION",
        ], max_chars=30),
        "Regimen Pensionario": find_label_value(texto, [
            "REGIMEN PENSIONARIO", "SISTEMA PENSIONARIO", "REGIMEN DE PENSIONES",
            "AFP / ONP", "SISTEMA DE PENSIONES",
        ], max_chars=40),
        "CUSPP": find_label_value(texto, ["CUSPP", "CODIGO CUSPP"], max_chars=25),
        "Dias Trabajados": find_int(texto, [
            "DIAS TRABAJADOS", "DIAS LABORADOS", "DIAS EFECTIVOS",
        ]),
        "Dias No Laborados": find_int(texto, [
            "DIAS NO LABORADOS", "DIAS NO TRABAJADOS", "FALTAS",
        ]),
        "Dias Subsidiados": find_int(texto, ["DIAS SUBSIDIADOS", "SUBSIDIOS"]),
        "Horas Trabajadas": find_int(texto, ["HORAS TRABAJADAS", "TOTAL HORAS", "HORAS"]),
    }


def extraer_boleta(texto, pagina_inicial, paginas):
    fila = extraer_datos_trabajador(texto)
    fila["Pagina Inicial"] = pagina_inicial
    fila["Paginas"] = paginas

    for campo in INGRESOS + DESCUENTOS + APORTES:
        fila[campo] = None

    conceptos = extraer_conceptos(texto)
    for concepto in conceptos:
        campo = concepto["Campo Asignado"]
        if campo:
            fila[campo] = round((fila.get(campo) or 0) + concepto["Monto"], 2)

    total_ingresos = find_money(texto, [
        "TOTAL INGRESOS", "TOTAL DE INGRESOS", "TOTAL REMUNERACION",
        "TOTAL HABERES", "TOTAL BRUTO",
    ])
    total_descuentos = find_money(texto, [
        "TOTAL DESCUENTOS", "TOTAL DE DESCUENTOS", "TOTAL RETENCIONES",
    ])
    total_aportes = find_money(texto, [
        "TOTAL APORTES", "TOTAL DE APORTES", "TOTAL APORTACIONES",
        "TOTAL CONTRIBUCIONES",
    ])
    neto = find_money(texto, [
        "NETO A PAGAR", "TOTAL NETO", "NETO", "LIQUIDO A PAGAR", "TOTAL A PAGAR",
    ])

    # Si la boleta no trae el total escrito, se usa la suma de los conceptos.
    if total_ingresos is None:
        suma = sum(concepto["Monto"] for concepto in conceptos if concepto["Bloque"] == "ingresos")
        total_ingresos = round(suma, 2) if suma else None
    if total_descuentos is None:
        suma = sum(concepto["Monto"] for concepto in conceptos if concepto["Bloque"] == "descuentos")
        total_descuentos = round(suma, 2) if suma else None
    if total_aportes is None:
        suma = sum(concepto["Monto"] for concepto in conceptos if concepto["Bloque"] == "aportes")
        total_aportes = round(suma, 2) if suma else None

    fila["Total Ingresos"] = total_ingresos
    fila["Total Descuentos"] = total_descuentos
    fila["Total Aportes"] = total_aportes
    fila["Neto a Pagar"] = neto

    esperado = None
    if total_ingresos is not None:
        esperado = total_ingresos - (total_descuentos or 0)
    fila["Cuadra Neto"] = cuadra(esperado, neto, tolerancia=0.05)
    fila["Observaciones"] = faltantes(fila, CAMPOS_OBLIGATORIOS)

    return fila, conceptos


def leer_paginas(uploaded_file):
    paginas = []
    with pdfplumber.open(io.BytesIO(uploaded_file.getvalue())) as pdf:
        for numero, page in enumerate(pdf.pages, start=1):
            texto = fix_mojibake(page.extract_text() or "")
            paginas.append({
                "numero": numero,
                "texto": texto,
                "dni": find_dni(texto, [
                    "DNI", "D.N.I.", "DOCUMENTO DE IDENTIDAD", "N. DOCUMENTO",
                    "NRO. DOCUMENTO", "DOC. IDENTIDAD",
                ]),
                "marcador": any(
                    marcador in normalize(texto) for marcador in MARCADORES_BOLETA
                ),
            })
    return paginas


def agrupar_boletas(paginas):
    """Reparte las paginas entre trabajadores. Lo normal es una boleta por pagina."""
    boletas = []

    for pagina in paginas:
        if not boletas:
            abre = True
        elif pagina["marcador"]:
            abre = True
        elif pagina["dni"] and pagina["dni"] != boletas[-1]["dni"]:
            abre = True
        else:
            abre = False

        if abre:
            boletas.append({
                "dni": pagina["dni"],
                "pagina_inicial": pagina["numero"],
                "paginas": [pagina],
            })
        else:
            boletas[-1]["paginas"].append(pagina)
            if not boletas[-1]["dni"]:
                boletas[-1]["dni"] = pagina["dni"]

    return boletas


def process_pdf(uploaded_file):
    """Devuelve (boletas, conceptos, auditoria) para un PDF."""
    paginas = leer_paginas(uploaded_file)
    agrupadas = agrupar_boletas(paginas)

    filas_boleta = []
    filas_concepto = []
    filas_auditoria = []

    for boleta in agrupadas:
        texto = "\n".join(pagina["texto"] for pagina in boleta["paginas"])
        fila, conceptos = extraer_boleta(
            texto,
            boleta["pagina_inicial"],
            len(boleta["paginas"]),
        )
        fila["PDF File"] = uploaded_file.name
        filas_boleta.append(fila)

        for concepto in conceptos:
            concepto.update({
                "PDF File": uploaded_file.name,
                "Periodo": fila["Periodo"],
                "DNI": fila["DNI"],
                "Trabajador": fila["Trabajador"],
                "Pagina": boleta["pagina_inicial"],
            })
        filas_concepto.extend(conceptos)

        sin_bloque = sum(1 for concepto in conceptos if not concepto["Bloque"])
        detalle = fila["Observaciones"] and f"Sin leer: {fila['Observaciones']}"
        if sin_bloque and not detalle:
            detalle = f"{sin_bloque} concepto(s) sin bloque identificado"

        for indice, pagina in enumerate(boleta["paginas"]):
            filas_auditoria.append({
                "PDF File": uploaded_file.name,
                "Pagina": pagina["numero"],
                "Rol": "inicio_boleta" if indice == 0 else "continuacion",
                "Trabajador": fila["Trabajador"],
                "DNI": fila["DNI"],
                "Detalle": detalle,
            })

    return filas_boleta, filas_concepto, filas_auditoria


def build_summary_rows(boletas, conceptos, auditoria, archivos):
    periodos = sorted({boleta["Periodo"] for boleta in boletas if boleta.get("Periodo")})

    return [
        {"Metric": "Archivos procesados", "Value": archivos},
        {"Metric": "Paginas leidas", "Value": len(auditoria)},
        {"Metric": "Boletas", "Value": len(boletas)},
        {"Metric": "Trabajadores distintos", "Value": len({b["DNI"] for b in boletas if b.get("DNI")})},
        {"Metric": "Periodos", "Value": ", ".join(periodos) if periodos else None},
        {"Metric": "Conceptos leidos", "Value": len(conceptos)},
        {"Metric": "Total ingresos", "Value": round(sum(b.get("Total Ingresos") or 0 for b in boletas), 2)},
        {"Metric": "Total descuentos", "Value": round(sum(b.get("Total Descuentos") or 0 for b in boletas), 2)},
        {"Metric": "Total aportes empleador", "Value": round(sum(b.get("Total Aportes") or 0 for b in boletas), 2)},
        {"Metric": "Total neto a pagar", "Value": round(sum(b.get("Neto a Pagar") or 0 for b in boletas), 2)},
        {"Metric": "Boletas que no cuadran", "Value": sum(1 for b in boletas if b.get("Cuadra Neto") == "No")},
        {"Metric": "Boletas con campos sin leer", "Value": sum(1 for b in boletas if b.get("Observaciones"))},
    ]


def build_excel(files):
    boletas = []
    conceptos = []
    auditoria = []

    for uploaded_file in files:
        try:
            filas_boleta, filas_concepto, filas_auditoria = process_pdf(uploaded_file)
        except Exception as error:  # un PDF danado no debe tumbar el lote entero
            auditoria.append({
                "PDF File": uploaded_file.name,
                "Pagina": None,
                "Rol": "error",
                "Trabajador": None,
                "DNI": None,
                "Detalle": f"No se pudo leer: {error}",
            })
            continue

        boletas.extend(filas_boleta)
        conceptos.extend(filas_concepto)
        auditoria.extend(filas_auditoria)

    resumen = build_summary_rows(boletas, conceptos, auditoria, len(files))

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(fechas_a_formato_local(boletas)).reindex(columns=BOLETA_COLUMNS).to_excel(
            writer, index=False, sheet_name="Boletas",
        )
        pd.DataFrame(fechas_a_formato_local(conceptos)).reindex(columns=CONCEPTO_COLUMNS).to_excel(
            writer, index=False, sheet_name="Conceptos",
        )
        pd.DataFrame(resumen).reindex(columns=SUMMARY_COLUMNS).to_excel(
            writer, index=False, sheet_name="Resumen",
        )
        pd.DataFrame(fechas_a_formato_local(auditoria)).reindex(columns=AUDIT_COLUMNS).to_excel(
            writer, index=False, sheet_name="Auditoria",
        )

    output.seek(0)
    return output, boletas, conceptos


def render_sidebar_boletas():
    """Ayuda propia de la pantalla de boletas."""
    with st.sidebar:
        st.markdown('<div class="side-title">Bloques que se leen</div>', unsafe_allow_html=True)
        st.markdown(
            """
            <div class="side-card">
                INGRESOS<br>
                DESCUENTOS<br>
                APORTES DEL EMPLEADOR
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown('<div class="side-title">Operacion</div>', unsafe_allow_html=True)
        st.markdown(
            """
            <div class="side-note">
                Un PDF puede traer todas las boletas del mes.<br>
                Lo normal es una boleta por pagina.
            </div>
            """,
            unsafe_allow_html=True,
        )


ICONO_RRHH = """
<svg viewBox="0 0 96 96" width="72" height="72" aria-label="Recursos Humanos">
    <circle cx="38" cy="32" r="13" fill="#ffffff" stroke="#d3c0f7"/>
    <path d="M16 74c0-12 10-19 22-19s22 7 22 19z" fill="#ffffff" stroke="#d3c0f7"/>
    <circle cx="66" cy="38" r="10" fill="#6d3fd1"/>
    <path d="M52 74c0-10 7-16 14-16s14 6 14 16z" fill="#6d3fd1"/>
</svg>
"""


def render_boletas():
    """Pantalla de lectura de boletas de pago."""
    st.markdown('<div class="app-shell">', unsafe_allow_html=True)

    render_hero(
        "RECURSOS HUMANOS DOCUMENT CENTER",
        'Boletas de pago <span style="color:#d9c2ff">&rsaquo;</span> Excel consolidado',
        "Sube las boletas del periodo y genera un Excel con Boletas, Conceptos, Resumen y Auditoria.",
        tags=[("Lectura por bloques", "green")],
        variante="hr",
        icono_svg=ICONO_RRHH,
    )

    render_pipeline([
        ("Input", "Boletas PDF", "active", "Pend."),
        ("Lectura", "Ingresos y descuentos", "ok", "OK"),
        ("Validacion", "Ingresos - descuentos vs neto", "warn", "Revisar"),
        ("Salida", "Excel Planilla", "", "Pend."),
    ])

    render_rules(
        "Preparar lectura de boletas",
        "El sistema separa una boleta por trabajador y clasifica cada concepto segun el bloque de la boleta en que aparece.",
        [
            ("Trabajador", "Nombre, DNI, cargo, area, ingreso y regimen"),
            ("Planilla", "Basico, asignacion familiar, extras y gratificacion"),
            ("Descuentos", "AFP u ONP, renta de quinta, adelantos y prestamos"),
        ],
        variante="hr",
    )

    st.markdown('<div class="work-card upload-wrap hr"><h3>1. Cargar boletas</h3>', unsafe_allow_html=True)
    uploaded_files = st.file_uploader(
        "Subir PDFs de boletas",
        type=["pdf"],
        accept_multiple_files=True,
        label_visibility="collapsed",
        key="rrhh_uploader",
    )
    st.markdown("</div>", unsafe_allow_html=True)

    open_card("2. Archivos cargados", kicker="Control de entrada")

    if uploaded_files:
        render_stat_grid([
            ("PDFs cargados", len(uploaded_files)),
            ("Tamano total", format_file_size(
                sum(getattr(archivo, "size", 0) or 0 for archivo in uploaded_files)
            )),
            ("Estado", "Listos"),
        ])
        render_file_list(
            [
                (
                    archivo.name,
                    f"{format_file_size(getattr(archivo, 'size', None))} - Boletas",
                    "PDF",
                    "Listo",
                )
                for archivo in uploaded_files
            ],
            variante="hr",
        )
    else:
        render_empty("Carga las boletas de pago del periodo para comenzar.")

    st.markdown(
        '<div class="privacy-note">Las boletas contienen datos personales. '
        "Los archivos se procesan en memoria durante la sesion y no se guardan "
        "en el servidor; el Excel se descarga a tu equipo.</div>",
        unsafe_allow_html=True,
    )

    close_card()

    open_card(
        "3. Procesar y generar Excel",
        kicker="Salida final",
        texto="Convierte las boletas en un Excel con las hojas Boletas, Conceptos, Resumen y Auditoria.",
    )

    if st.button(
        "Procesar boletas",
        type="primary",
        disabled=not uploaded_files,
        key="rrhh_procesar",
    ):
        with st.spinner("Leyendo boletas..."):
            excel_bytes, boletas, conceptos = build_excel(uploaded_files)

        sin_leer = sum(1 for boleta in boletas if boleta.get("Observaciones"))
        descuadradas = sum(1 for boleta in boletas if boleta.get("Cuadra Neto") == "No")

        if boletas:
            render_banner("Excel generado correctamente. Ya puedes descargar la salida consolidada.")
        else:
            render_banner(
                "No se reconocio ninguna boleta. Revisa la hoja Auditoria del Excel.",
                tipo="warn",
            )

        render_result_grid([
            ("Boletas leidas", len(boletas)),
            ("Conceptos leidos", len(conceptos)),
        ])

        if sin_leer or descuadradas:
            render_banner(
                f"{sin_leer} boleta(s) con campos sin leer y {descuadradas} donde "
                "ingresos menos descuentos no coincide con el neto. Estan marcadas en el Excel.",
                tipo="warn",
            )

        if boletas:
            st.dataframe(
                pd.DataFrame(fechas_a_formato_local(boletas)).reindex(columns=[
                    "Periodo", "Trabajador", "DNI", "Cargo", "Area",
                    "Total Ingresos", "Total Descuentos", "Neto a Pagar",
                    "Cuadra Neto", "Observaciones",
                ]).head(50),
                use_container_width=True,
                hide_index=True,
            )

        st.download_button(
            "Descargar Excel",
            data=excel_bytes,
            file_name="salida_rrhh_boletas.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="rrhh_descargar",
        )

    close_card()

    render_benefits([
        ("Conceptos completos", "Todos los conceptos quedan listados, tambien los que no encajan en una columna fija."),
        ("Control de cuadre", "Compara ingresos menos descuentos contra el neto de cada boleta."),
        ("Sin guardar datos", "El procesamiento es en memoria y el resultado se descarga a tu equipo."),
    ])

    st.markdown("</div>", unsafe_allow_html=True)


ICONO_ALQUILERES = """
<svg viewBox="0 0 96 96" width="72" height="72" aria-label="Alquileres">
    <rect x="14" y="30" width="30" height="46" rx="4" fill="#ffffff" stroke="#d3c0f7"/>
    <rect x="50" y="16" width="32" height="60" rx="4" fill="#6d3fd1"/>
    <rect x="21" y="38" width="7" height="7" rx="2" fill="#d3c0f7"/>
    <rect x="32" y="38" width="7" height="7" rx="2" fill="#d3c0f7"/>
    <rect x="21" y="52" width="7" height="7" rx="2" fill="#d3c0f7"/>
    <rect x="32" y="52" width="7" height="7" rx="2" fill="#d3c0f7"/>
    <rect x="57" y="26" width="8" height="8" rx="2" fill="#ffffff"/>
    <rect x="69" y="26" width="8" height="8" rx="2" fill="#ffffff"/>
    <rect x="57" y="42" width="8" height="8" rx="2" fill="#ffffff"/>
    <rect x="69" y="42" width="8" height="8" rx="2" fill="#ffffff"/>
    <rect x="61" y="58" width="12" height="18" rx="2" fill="#ffffff"/>
</svg>
"""

# Se muestra la hoja Control tal cual saldra en el Excel, sin columnas extra.
COLUMNAS_VISTA_PREVIA = alquileres.CONTROL_COLUMNS


def render_alquileres():
    """Pantalla de lectura de facturas de arriendo de locales."""
    st.markdown('<div class="app-shell">', unsafe_allow_html=True)

    render_hero(
        "CONTROL DE ARRIENDOS",
        'Facturas de alquiler <span style="color:#d9c2ff">&rsaquo;</span> Excel de control',
        "Sube las facturas de los centros comerciales y genera el control con una fila por concepto: renta minima, fondo de promociones, gastos comunes y los demas.",
        tags=[("Una fila por concepto", "green")],
        variante="hr",
        icono_svg=ICONO_ALQUILERES,
    )

    render_pipeline([
        ("Input", "Facturas PDF", "active", "Pend."),
        ("Lectura", "Contrato y conceptos", "ok", "OK"),
        ("Validacion", "Conceptos vs base imponible", "warn", "Revisar"),
        ("Salida", "Excel de control", "", "Pend."),
    ])

    render_rules(
        "Preparar lectura de facturas",
        "Cada concepto de la factura se convierte en una fila. El importe se toma sin IGV y se verifica que la suma de los conceptos cuadre con la base imponible del documento.",
        [
            ("Contrato", "Numero de contrato y local, segun lo que traiga la factura"),
            ("Concepto", "Descripcion limpia y mes del periodo facturado"),
            ("Moneda", "El importe cae en la columna SOLES o DOLARES"),
        ],
        variante="hr",
    )

    st.markdown('<div class="work-card upload-wrap hr"><h3>1. Cargar facturas</h3>', unsafe_allow_html=True)
    uploaded_files = st.file_uploader(
        "Subir PDFs de facturas de alquiler",
        type=["pdf"],
        accept_multiple_files=True,
        label_visibility="collapsed",
        key="alquileres_uploader",
    )
    st.markdown("</div>", unsafe_allow_html=True)

    open_card("2. Archivos cargados", kicker="Control de entrada")

    if uploaded_files:
        render_stat_grid([
            ("PDFs cargados", len(uploaded_files)),
            ("Tamano total", format_file_size(
                sum(getattr(archivo, "size", 0) or 0 for archivo in uploaded_files)
            )),
            ("Estado", "Listos"),
        ])
        render_file_list(
            [
                (
                    archivo.name,
                    f"{format_file_size(getattr(archivo, 'size', None))} - Factura de alquiler",
                    "PDF",
                    "Listo",
                )
                for archivo in uploaded_files
            ],
            variante="hr",
        )
    else:
        render_empty("Carga las facturas de los centros comerciales para comenzar.")

    close_card()

    open_card(
        "3. Procesar y generar Excel",
        kicker="Salida final",
        texto="Genera el Excel con la hoja Control lista para pegar, mas Control ampliado, Facturas, Resumen y Auditoria.",
    )

    if st.button(
        "Procesar facturas",
        type="primary",
        disabled=not uploaded_files,
        key="alquileres_procesar",
    ):
        with st.spinner("Leyendo facturas..."):
            excel_bytes, control, facturas = alquileres.build_excel(uploaded_files)

        descuadradas = sum(1 for factura in facturas if factura.get("Cuadra Base") == "No")
        sin_conceptos = sum(1 for factura in facturas if not factura.get("Conceptos"))

        if control:
            render_banner("Excel generado correctamente. Ya puedes descargar el control.")
        else:
            render_banner(
                "No se reconocio ninguna factura. Revisa la hoja Auditoria del Excel.",
                tipo="warn",
            )

        render_result_grid([
            ("Facturas leidas", len(facturas)),
            ("Filas de control", len(control)),
        ])

        if descuadradas or sin_conceptos:
            render_banner(
                f"{descuadradas} factura(s) donde los conceptos no cuadran con la base "
                f"imponible y {sin_conceptos} sin conceptos leidos. Estan marcadas en el Excel.",
                tipo="warn",
            )

        if control:
            st.dataframe(
                pd.DataFrame(fechas_a_formato_local(control)).reindex(columns=COLUMNAS_VISTA_PREVIA).head(80),
                use_container_width=True,
                hide_index=True,
            )

        st.download_button(
            "Descargar Excel",
            data=excel_bytes,
            file_name="control_alquileres.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="alquileres_descargar",
        )

    close_card()

    render_benefits([
        ("Listo para pegar", "La hoja Control trae solo tus columnas, en tu orden, sin nada que borrar."),
        ("Importe verificado", "La columna del valor sin IGV se elige comprobando que la suma cuadre con la factura."),
        ("Tienda o contrato", "La columna Tienda toma el local y, cuando la factura no lo trae, el numero de contrato."),
    ])

    st.markdown("</div>", unsafe_allow_html=True)


def proceso_activo():
    """Pantalla elegida dentro del modulo, con un valor valido siempre."""
    if st.session_state.get("rrhh_proceso") not in PROCESOS:
        st.session_state["rrhh_proceso"] = "boletas"
    return st.session_state["rrhh_proceso"]


def render_sidebar():
    """Panel lateral del modulo: la ayuda de la pantalla elegida arriba.

    Que pantalla se muestra lo decide el desplegable "Sitio destino" del panel
    comun, para llegar a cualquiera de las dos en un solo paso.
    """
    if proceso_activo() == "alquileres":
        alquileres.render_sidebar()
    else:
        render_sidebar_boletas()


def render():
    """Pantalla completa del modulo Recursos Humanos."""
    if proceso_activo() == "alquileres":
        render_alquileres()
    else:
        render_boletas()
