#!/usr/bin/env python3
import csv
import json
import subprocess
import logging
from pathlib import Path

# Configuración de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CSV_FILE = "eventos.csv"
CABECERAS = ["name", "title", "port", "service_access_token", "tracker", "sources", "host", "bitrate", "content_id", "docker_active"]

def is_valid_source(source: str) -> bool:
    """
    Ejecuta ffprobe sobre la fuente y retorna True si la fuente es válida (exit code 0).
    """
    try:
        # Se ejecuta ffprobe; se suprime la salida excepto errores
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", source],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Error al ejecutar ffprobe para {source}: {e}")
        return False

def check_sources():
    csv_path = Path(CSV_FILE)
    if not csv_path.exists():
        logger.error("El archivo CSV no existe")
        return

    # Leer todos los eventos del CSV
    eventos = []
    with csv_path.open('r', newline='', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        for row in reader:
            # Convertir el campo sources de JSON a lista
            if row.get("sources"):
                try:
                    row["sources"] = json.loads(row["sources"])
                except Exception as e:
                    logger.error(f"Error al parsear sources para el evento {row.get('name')}: {e}")
                    row["sources"] = []
            else:
                row["sources"] = []
            eventos.append(row)

    # Procesar cada evento
    for evento in eventos:
        sources = evento.get("sources", [])
        if not sources:
            logger.info(f"Evento {evento.get('name')} no tiene fuentes definidas")
            continue
        valid_found = False
        for fuente in sources:
            # Si ya encontramos una fuente válida, marcamos la actual como inválida
            if valid_found:
                fuente["valid"] = False
                continue
            source_url = fuente.get("source", "").strip()
            if not source_url:
                fuente["valid"] = False
                continue
            logger.info(f"Verificando fuente '{source_url}' para el evento {evento.get('name')}")
            if is_valid_source(source_url):
                logger.info(f"Fuente válida encontrada: '{source_url}' para el evento {evento.get('name')}")
                fuente["valid"] = True
                valid_found = True
            else:
                logger.info(f"Fuente inválida: '{source_url}' para el evento {evento.get('name')}")
                fuente["valid"] = False
        if not valid_found:
            logger.warning(f"Ninguna fuente válida para el evento {evento.get('name')}")
        evento["sources"] = sources

    # Actualizar el CSV con los nuevos flags
    with csv_path.open('w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=CABECERAS)
        writer.writeheader()
        for evento in eventos:
            # Convertir de nuevo la lista de fuentes a JSON
            evento["sources"] = json.dumps(evento.get("sources", []))
            writer.writerow(evento)
    logger.info("CSV actualizado con la validez de las fuentes.")

if __name__ == "__main__":
    check_sources()
