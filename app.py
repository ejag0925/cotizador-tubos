import os
from flask import Flask, render_template, request, redirect, url_for, flash
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
import pandas as pd
import math

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key')

# Configuración Google Sheets
def get_google_sheet():
    scope = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive.file'
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        'credentials.json', scope)
    client = gspread.authorize(creds)
    return client.open_by_key(os.getenv('GOOGLE_SHEET_KEY'))

def convertir_usd_a_cop(valor_usd, trm):
    """Convierte dólares a pesos colombianos"""
    return valor_usd * trm

def calcular_rollos_necesarios(largo_mm, unidades, metros_por_rollo):
    """
    Calcula el número de rollos necesarios
    """
    largo_metro = largo_mm / 1000
    metros_totales = largo_metro * unidades
    return math.ceil(metros_totales / metros_por_rollo)

def encontrar_material(diametro):
    """Busca el material adecuado para el diámetro especificado"""
    sheet = get_google_sheet()
    materiales = pd.DataFrame(sheet.worksheet('Materiales').get_all_records())
    
    for _, row in materiales.iterrows():
        diametros_asociados = [int(d.strip()) for d in str(row['Diámetros Asociados']).split(',')]
        if diametro in diametros_asociados:
            return row
    return None

def obtener_costos_indirectos(escala):
    """Obtiene MOD y CIF unitarios según la escala de producción"""
    sheet = get_google_sheet()
    costos = pd.DataFrame(sheet.worksheet('CostosIndirectos').get_all_records())
    
    for _, row in costos.iterrows():
        if row['Escala Mínima'] <= escala <= row['Escala Máxima']:
            return row['MOD Unitario (COP)'], row['CIF Unitario (COP)']
    return costos.iloc[0]['MOD Unitario (COP)'], costos.iloc[0]['CIF Unitario (COP)']

@app.route('/')
def index():
    sheet = get_google_sheet()
    materiales = sheet.worksheet('Materiales').get_all_records()
    return render_template('index.html', materiales=materiales)

@app.route('/cotizar', methods=['POST'])
def cotizar():
    try:
        # Obtener datos del formulario
        data = {
            'diametro': float(request.form['diametro']),
            'largo': float(request.form['largo']),
            'tipo_tapa': request.form['tipo_tapa'],
            'escala': int(request.form['escala']),
            'margen': float(request.form['margen']) / 100
        }

        sheet = get_google_sheet()
        parametros = sheet.worksheet('Parametros').get_all_records()[0]
        trm = float(parametros['TRM'])
        desperdicio_primer_rollo = parametros['Desperdicio Primer Rollo (%)'] / 100
        desperdicio_rollos_siguientes = parametros['Desperdicio Rollos Siguientes (%)'] / 100
        metros_por_rollo = parametros['Metros por Rollo']

        # Buscar material adecuado
        material = encontrar_material(data['diametro'])
        if not material:
            flash(f'No existe rollo configurado para diámetro {data["diametro"]}mm', 'error')
            return redirect(url_for('index'))

        # Calcular área por tubo (m²)
        area_por_tubo = (data['diametro'] * 3.1416 * data['largo']) / 1000000
        area_por_tubo_efectiva = area_por_tubo / material['Repeticiones']
        
        # Calcular rollos necesarios (para mostrar, no afecta costo unitario)
        rollos_necesarios = calcular_rollos_necesarios(
            data['largo'], 
            data['escala'], 
            metros_por_rollo
        )

        # Calcular consumo de material con desperdicio (unitario)
        # Primer rollo: 20% desperdicio
        costo_mp_primer_rollo_usd = (material['Costo FOB ($/m²)'] * (1 + material['Factor Importación (%)']/100)) * area_por_tubo_efectiva * (1 + desperdicio_primer_rollo)
        
        # Rollos siguientes: 5% desperdicio
        costo_mp_rollos_siguientes_usd = (material['Costo FOB ($/m²)'] * (1 + material['Factor Importación (%)']/100)) * area_por_tubo_efectiva * (1 + desperdicio_rollos_siguientes)
        
        # Ponderar costos según proporción de rollos
        if rollos_necesarios == 1:
            costo_mp_unitario_usd = costo_mp_primer_rollo_usd
        else:
            # Calcular proporción de material que viene del primer rollo
            proporcion_primer_rollo = 1 / rollos_necesarios
            costo_mp_unitario_usd = (costo_mp_primer_rollo_usd * proporcion_primer_rollo) + \
                                   (costo_mp_rollos_siguientes_usd * (1 - proporcion_primer_rollo))
        
        costo_mp_unitario_cop = convertir_usd_a_cop(costo_mp_unitario_usd, trm)
        
        # Obtener costo de estampación unitario (COP)
        estampacion = pd.DataFrame(sheet.worksheet('Estampacion').get_all_records())
        estampacion_filtrada = estampacion[
            (estampacion['Diámetro (mm)'] == data['diametro']) & 
            (estampacion['Escala Mínima'] <= data['escala']) & 
            (estampacion['Escala Máxima'] >= data['escala'])
        ]
        
        if estampacion_filtrada.empty:
            flash('No se encontró costo de estampación para la escala especificada', 'error')
            return redirect(url_for('index'))
            
        costo_estampacion_unitario_cop = estampacion_filtrada.iloc[0]['Costo por m² (COP)'] * area_por_tubo_efectiva
        
        # Obtener componentes unitarios (COP)
        componentes = pd.DataFrame(sheet.worksheet('Componentes').get_all_records())
        hombro = componentes[
            (componentes['Tipo'] == 'hombro') & 
            (componentes['Diámetro (mm)'] == data['diametro'])
        ]
        
        if hombro.empty:
            flash('No se encontró costo para el hombro del diámetro especificado', 'error')
            return redirect(url_for('index'))
            
        costo_hombro_unitario_cop = hombro.iloc[0]['Costo Unitario (COP)']
        
        tapa = componentes[
            (componentes['Tipo'] == data['tipo_tapa']) & 
            (componentes['Diámetro (mm)'] == data['diametro'])
        ]
        
        if tapa.empty:
            flash('No se encontró costo para la tapa especificada', 'error')
            return redirect(url_for('index'))
            
        costo_tapa_unitario_cop = tapa.iloc[0]['Costo Unitario (COP)']
        
        # Obtener MOD y CIF unitarios según escala
        mod_unitario_cop, cif_unitario_cop = obtener_costos_indirectos(data['escala'])
        
        # Cálculos finales unitarios en COP
        costoTuboUnitarioCOP = (costo_mp_unitario_cop + costo_estampacion_unitario_cop)
        costoDirectoUnitarioCOP = costoTuboUnitarioCOP + costo_hombro_unitario_cop + costo_tapa_unitario_cop
        costoTotalUnitarioCOP = costoDirectoUnitarioCOP + mod_unitario_cop + cif_unitario_cop
        precioVentaUnitarioCOP = costoTotalUnitarioCOP / (1 - data['margen'])
        
        # Preparar resultado
        resultado = {
            'datos': data,
            'material': material,
            'costos': {
                'materia_prima_usd': round(costo_mp_unitario_usd, 4),
                'materia_prima_cop': round(costo_mp_unitario_cop, 2),
                'estampacion_cop': round(costo_estampacion_unitario_cop, 2),
                'hombro_cop': round(costo_hombro_unitario_cop, 2),
                'tapa_cop': round(costo_tapa_unitario_cop, 2),
                'mod_cop': round(mod_unitario_cop, 2),
                'cif_cop': round(cif_unitario_cop, 2),
                'total_cop': round(costoTotalUnitarioCOP, 2),
                'precio_venta_cop': round(precioVentaUnitarioCOP, 2)
            },
            'produccion': {
                'rollos_necesarios': rollos_necesarios,
                'desperdicio_primer_rollo': parametros['Desperdicio Primer Rollo (%)'],
                'desperdicio_rollos_siguientes': parametros['Desperdicio Rollos Siguientes (%)'],
                'metros_por_rollo': metros_por_rollo,
                'area_por_tubo_m2': round(area_por_tubo_efectiva, 6)
            },
            'parametros': parametros,
            'tasa_cambio': trm
        }
        
        return render_template('resultado.html', resultado=resultado)
        
    except Exception as e:
        flash(f'Error al calcular la cotización: {str(e)}', 'error')
        return redirect(url_for('index'))

@app.route('/admin')
def admin():
    sheet = get_google_sheet()
    materiales = sheet.worksheet('Materiales').get_all_records()
    estampacion = sheet.worksheet('Estampacion').get_all_records()
    componentes = sheet.worksheet('Componentes').get_all_records()
    costos_indirectos = sheet.worksheet('CostosIndirectos').get_all_records()
    parametros = sheet.worksheet('Parametros').get_all_records()[0]
    
    return render_template('admin.html', 
                         materiales=materiales,
                         estampacion=estampacion,
                         componentes=componentes,
                         costos_indirectos=costos_indirectos,
                         parametros=parametros)

if __name__ == '__main__':
    app.run(debug=True)
