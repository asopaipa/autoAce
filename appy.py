import csv
import os
import subprocess
import requests
import re
import logging
import json
import time
import ipaddress
import threading
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, flash

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@dataclass
class EventoConfig:
    nombre: str
    titulo: str
    puerto: int
    tracker: str
    sources: list  # lista de diccionarios: [{'source': 'url', 'valid': False}, ...]
    host: str
    bitrate: int
    token: str
    content_id: str = ""
    docker_active: str = "False"

    def _es_ip_valida(self, ip: str) -> bool:
        try:
            ipaddress.ip_address(ip)
            return True
        except ValueError:
            return False

    def _es_dominio_valido(self, dominio: str) -> bool:
        patron = r'^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$'
        return bool(re.match(patron, dominio))

    def validar(self) -> list:
        errores = []
        if not self.nombre or not re.match(r'^[a-zA-Z0-9_-]+$', self.nombre):
            errores.append("Nombre inválido: use solo letras, números, guiones y guiones bajos")
        if not (1024 <= self.puerto <= 65535):
            errores.append("Puerto inválido: debe estar entre 1024 y 65535")
        if not self._es_ip_valida(self.host) and not self._es_dominio_valido(self.host):
            errores.append("Host inválido: debe ser una IP válida o un nombre de dominio")
        if not (0 <= self.bitrate <= 10000000):
            errores.append("Bitrate inválido")
        if not self.titulo.strip():
            errores.append("El título no puede estar vacío")
        # Se requiere al menos una fuente no vacía
        if not self.sources or all(not s.get("source", "").strip() for s in self.sources):
            errores.append("Debe ingresar al menos una fuente")
        return errores

class EventoManager:
    def __init__(self, csv_path: str):
        self.csv_path = Path(csv_path)
        # Nuevas cabeceras con múltiples fuentes y flag de docker activo
        self.cabeceras = ["name", "title", "port", "service_access_token", "tracker", "sources", "host",
                           "bitrate", "content_id", "docker_active"]
        self._inicializar_csv()

    def _inicializar_csv(self):
        if not self.csv_path.exists():
            with self._abrir_csv('w') as file:
                writer = csv.writer(file)
                writer.writerow(self.cabeceras)

    @contextmanager
    def _abrir_csv(self, modo='r'):
        file = None
        try:
            file = open(self.csv_path, mode=modo, newline='', encoding='utf-8')
            yield file
        finally:
            if file:
                file.close()

    def listar_eventos(self) -> list:
        eventos = []
        with self._abrir_csv('r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                # Convertir el campo sources de JSON a lista
                if row.get("sources"):
                    try:
                        row["sources"] = json.loads(row["sources"])
                    except json.JSONDecodeError:
                        row["sources"] = []
                else:
                    row["sources"] = []
                eventos.append(row)
        return eventos

    def agregar_evento(self, config: EventoConfig):
        with self._abrir_csv('a') as file:
            writer = csv.writer(file)
            writer.writerow([
                config.nombre,
                config.titulo,
                str(config.puerto),
                config.token,
                config.tracker,
                json.dumps(config.sources),
                config.host,
                str(config.bitrate),
                config.content_id,
                config.docker_active
            ])

    def actualizar_evento(self, config: EventoConfig):
        eventos = self.listar_eventos()
        updated = False
        for evento in eventos:
            if evento["name"] == config.nombre:
                evento["title"] = config.titulo
                evento["port"] = str(config.puerto)
                evento["service_access_token"] = config.token
                evento["tracker"] = config.tracker
                evento["sources"] = json.dumps(config.sources)
                evento["host"] = config.host
                evento["bitrate"] = str(config.bitrate)
                evento["content_id"] = config.content_id
                evento["docker_active"] = config.docker_active
                updated = True
                break
        if updated:
            self._escribir_eventos(eventos)
        else:
            raise ValueError("Evento no encontrado para actualizar")

    def eliminar_evento(self, nombre: str):
        eventos = self.listar_eventos()
        eventos = [evento for evento in eventos if evento["name"] != nombre]
        self._escribir_eventos(eventos)

    def _escribir_eventos(self, eventos: list):
        with self._abrir_csv('w') as file:
            writer = csv.writer(file)
            writer.writerow(self.cabeceras)
            for evento in eventos:
                sources = evento.get("sources")
                if isinstance(sources, list):
                    sources = json.dumps(sources)
                writer.writerow([
                    evento.get("name", ""),
                    evento.get("title", ""),
                    evento.get("port", ""),
                    evento.get("service_access_token", ""),
                    evento.get("tracker", ""),
                    sources,
                    evento.get("host", ""),
                    evento.get("bitrate", ""),
                    evento.get("content_id", ""),
                    evento.get("docker_active", "False")
                ])

    def _actualizar_content_id(self, nombre: str, content_id: str):
        eventos = self.listar_eventos()
        for evento in eventos:
            if evento["name"] == nombre:
                evento["content_id"] = content_id
                break
        self._escribir_eventos(eventos)

    def _actualizar_docker_active(self, nombre: str, active: bool):
        eventos = self.listar_eventos()
        for evento in eventos:
            if evento["name"] == nombre:
                evento["docker_active"] = "True" if active else "False"
                break
        self._escribir_eventos(eventos)

    def verificar_y_limpiar_contenedor(self, nombre: str):
        try:
            resultado = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name=^{nombre}$", "--format", "{{.ID}}"],
                capture_output=True,
                text=True,
                check=True
            )
            if resultado.stdout.strip():
                contenedor_id = resultado.stdout.strip()
                logger.info(f"Contenedor existente encontrado: {nombre} ({contenedor_id})")
                self.parar_contenedor(contenedor_id)
                self.borrar_contenedor(contenedor_id)
                logger.info(f"Contenedor {nombre} eliminado exitosamente")
        except subprocess.CalledProcessError as e:
            logger.error(f"Error al ejecutar comando Docker: {e.stderr}")
            raise
        except Exception as e:
            logger.error(f"Error al limpiar contenedor {nombre}: {e}")
            raise

    def parar_contenedor(self, contenedor_id: str):
        try:
            resultado = subprocess.run(
                ["docker", "stop", contenedor_id],
                check=True,
                capture_output=True,
                text=True
            )
            logger.debug(f"Contenedor detenido: {resultado.stdout}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Error al detener contenedor: {e.stderr}")
            raise RuntimeError(f"Error al detener contenedor: {e.stderr}")

    def borrar_contenedor(self, contenedor_id: str):
        try:
            resultado = subprocess.run(
                ["docker", "rm", contenedor_id],
                check=True,
                capture_output=True,
                text=True
            )
            logger.debug(f"Contenedor eliminado: {resultado.stdout}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Error al eliminar contenedor: {e.stderr}")
            raise RuntimeError(f"Error al eliminar contenedor: {e.stderr}")

    def limpiar_archivos_temporales(self, nombre_volumen: str) -> int:
        try:
            import platform
            if platform.system() == "Windows":
                ruta_base = Path(f"\\\\wsl.localhost\\docker-desktop\\mnt\\docker-desktop-disk\\data\\docker\\volumes\\{nombre_volumen}\\_data")
            elif platform.system() == "Linux":
                ruta_base = Path(f"/var/lib/docker/volumes/{nombre_volumen}/_data")
            else:
                logger.warning(f"Sistema operativo no soportado: {platform.system()}")
                return 0

            if not ruta_base.exists():
                logger.info(f"El directorio {ruta_base} no existe")
                return 0

            extensiones_permitidas = {".acelive", ".sauth"}
            archivos_eliminados = 0
            for archivo in ruta_base.iterdir():
                if archivo.is_file() and archivo.suffix not in extensiones_permitidas:
                    try:
                        archivo.unlink()
                        archivos_eliminados += 1
                        logger.debug(f"Archivo eliminado: {archivo}")
                    except Exception as e:
                        logger.error(f"Error al eliminar {archivo}: {e}")
            
            logger.info(f"Se eliminaron {archivos_eliminados} archivos temporales")
            return archivos_eliminados
        except Exception as e:
            logger.error(f"Error en limpieza de archivos temporales: {e}")
            return 0

    def _construir_comando_docker(self, config: EventoConfig) -> list:
        # Seleccionar la primera fuente válida
        valid_source = None
        for fuente in config.sources:
            if fuente.get("valid") == True:
                valid_source = fuente.get("source")
                break
        if not valid_source:
            raise ValueError("No se encontró una fuente válida para el evento")
        return [
            "docker", "run", "-d", "--restart", "unless-stopped",
            "--name", config.nombre,
            "-p", f"{config.puerto}:{config.puerto}/tcp",
            "-p", f"{config.puerto}:{config.puerto}/udp",
            "-v", f"acestreamengine_{config.nombre}:/data",
            "lob666/acestreamengine",
            "--port", str(config.puerto),
            "--tracker", config.tracker,
            "--stream-source",
            "--name", config.nombre,
            "--title", config.titulo,
            "--publish-dir", "/data",
            "--cache-dir", "/data",
            "--skip-internal-tracker",
            "--quality", "HD",
            "--category", "amateur",
            "--service-access-token", config.token,
            "--service-remote-access",
            "--log-debug", "1",
            "--max-peers", "6",
            "--max-upload-slots", "6",
            "--source-read-timeout", "15",
            "--source-reconnect-interval", "1",
            "--host", config.host,
            "--source", valid_source,
            "--bitrate", str(config.bitrate)
        ]

    def obtener_monitor(self, contenedor_id: str, puerto: int, intentos: int = 10) -> dict:
        tiempo_espera_inicial = 15  # segundos
        tiempo_entre_intentos = 5   # segundos
        
        logger.info(f"Esperando {tiempo_espera_inicial} segundos para el inicio del servicio...")
        time.sleep(tiempo_espera_inicial)
        
        for intento in range(intentos):
            try:
                response = requests.get(
                    f"http://localhost:{puerto}/app/{puerto}/monitor",
                    timeout=10
                )
                
                if response.status_code == 200:
                    try:
                        monitor_data = response.json()
                        content_id = monitor_data.get('content_id')
                        download_hash = monitor_data.get('download_hash')
                        
                        if content_id and content_id != 'No encontrado':
                            return {
                                'content_id': content_id,
                                'download_hash': download_hash
                            }
                        else:
                            logger.warning(f"Content ID vacío o no encontrado en el intento {intento + 1}")
                    except ValueError as e:
                        logger.warning(f"Error al decodificar JSON en el intento {intento + 1}: {e}")
                else:
                    logger.warning(f"Estado de respuesta inesperado ({response.status_code}) en el intento {intento + 1}")
                    
            except requests.RequestException as e:
                logger.warning(f"Reintentando obtener monitor ({intento + 1}/{intentos}): {e}")
            
            logger.info(f"Esperando {tiempo_entre_intentos} segundos antes del siguiente intento...")
            time.sleep(tiempo_entre_intentos)
        
        raise RuntimeError("No se pudo obtener el monitor después de varios intentos")

    def iniciar_docker_evento(self, config: EventoConfig):
        try:
            errores = config.validar()
            if errores:
                logger.error(f"Errores de validación para {config.nombre}: {errores}")
                return f"Errores de validación: {errores}"

            logger.info(f"Iniciando contenedor para evento: {config.nombre}")
            self.verificar_y_limpiar_contenedor(config.nombre)
            nombre_volumen = f"acestreamengine_{config.nombre}"
            archivos_eliminados = self.limpiar_archivos_temporales(nombre_volumen)
            logger.info(f"Archivos temporales eliminados: {archivos_eliminados}")
            
            comando = self._construir_comando_docker(config)
            comando_str = " ".join(comando)
            logger.info(f"Ejecutando comando: {comando_str}")
            subprocess.run(comando, check=True, capture_output=True, text=True)
            logger.info(f"Contenedor creado exitosamente para {config.nombre}")
            
            # Verificar el estado del contenedor y obtener monitor
            time.sleep(10)
            resultado = subprocess.run(
                ["docker", "ps", "--filter", f"name=^{config.nombre}$", "--format", "{{.ID}}"],
                capture_output=True,
                text=True,
                check=True
            )
            contenedor_id = resultado.stdout.strip()
            info = self.obtener_monitor(contenedor_id, config.puerto)
            content_id = info.get('content_id', 'No encontrado')
            logger.info(f"Content ID para {config.nombre}: {content_id}")
            self._actualizar_content_id(config.nombre, content_id)
            # Actualizar flag de docker activo a True
            self._actualizar_docker_active(config.nombre, True)
            return f"Evento '{config.nombre}' iniciado correctamente con Content ID: {content_id}"
        except Exception as e:
            logger.error(f"Error al iniciar evento {config.nombre}: {e}")
            return f"Error al iniciar evento {config.nombre}: {e}"

# Configuración de la aplicación Flask
app = Flask(__name__)
app.secret_key = 'supersecretkey'
manager = EventoManager("eventos.csv")

@app.route("/")
def index():
    eventos = manager.listar_eventos()
    return render_template("index.html", eventos=eventos)

@app.route("/event/new", methods=["GET", "POST"])
def new_event():
    if request.method == "POST":
        nombre = request.form.get("nombre").strip()
        titulo = request.form.get("titulo").strip()
        puerto = int(request.form.get("puerto").strip() or 8642)
        tracker = request.form.get("tracker").strip() or "udp://tracker.opentrackr.org:1337/announce"
        token = request.form.get("token").strip() or "12345"
        host = request.form.get("host").strip()
        bitrate = int(request.form.get("bitrate").strip() or 697587)
        # Obtener múltiples fuentes
        fuentes = request.form.getlist("sources")
        sources = [{"source": s.strip(), "valid": False} for s in fuentes if s.strip() != ""]
        
        config = EventoConfig(
            nombre=nombre,
            titulo=titulo,
            puerto=puerto,
            tracker=tracker,
            sources=sources,
            host=host,
            bitrate=bitrate,
            token=token
        )
        errores = config.validar()
        if errores:
            for error in errores:
                flash(error, "danger")
            return redirect(url_for("new_event"))
        try:
            manager.agregar_evento(config)
            flash(f"Evento '{nombre}' creado exitosamente", "success")
            return redirect(url_for("index"))
        except Exception as e:
            flash(f"Error al crear evento: {e}", "danger")
            return redirect(url_for("new_event"))
    return render_template("new_event.html")

@app.route("/event/<nombre>/edit", methods=["GET", "POST"])
def edit_event(nombre):
    eventos = manager.listar_eventos()
    evento = next((e for e in eventos if e["name"] == nombre), None)
    if not evento:
        flash("Evento no encontrado", "danger")
        return redirect(url_for("index"))
    if request.method == "POST":
        titulo = request.form.get("titulo").strip()
        puerto = int(request.form.get("puerto").strip() or 8642)
        tracker = request.form.get("tracker").strip() or "udp://tracker.opentrackr.org:1337/announce"
        token = request.form.get("token").strip() or "12345"
        host = request.form.get("host").strip()
        bitrate = int(request.form.get("bitrate").strip() or 697587)
        fuentes = request.form.getlist("sources")
        sources = [{"source": s.strip(), "valid": False} for s in fuentes if s.strip() != ""]
        config = EventoConfig(
            nombre=nombre,
            titulo=titulo,
            puerto=puerto,
            tracker=tracker,
            sources=sources,
            host=host,
            bitrate=bitrate,
            token=token,
            content_id=evento.get("content_id", ""),
            docker_active=evento.get("docker_active", "False")
        )
        errores = config.validar()
        if errores:
            for error in errores:
                flash(error, "danger")
            return redirect(url_for("edit_event", nombre=nombre))
        try:
            manager.actualizar_evento(config)
            flash(f"Evento '{nombre}' actualizado exitosamente", "success")
            return redirect(url_for("index"))
        except Exception as e:
            flash(f"Error al actualizar evento: {e}", "danger")
            return redirect(url_for("edit_event", nombre=nombre))
    return render_template("edit_event.html", evento=evento)

@app.route("/event/<nombre>/delete", methods=["POST"])
def delete_event(nombre):
    try:
        manager.eliminar_evento(nombre)
        flash(f"Evento '{nombre}' eliminado exitosamente", "success")
    except Exception as e:
        flash(f"Error al eliminar evento: {e}", "danger")
    return redirect(url_for("index"))

@app.route("/event/<nombre>/start", methods=["POST"])
def start_event(nombre):
    eventos = manager.listar_eventos()
    evento = next((e for e in eventos if e["name"] == nombre), None)
    if not evento:
        flash("Evento no encontrado", "danger")
        return redirect(url_for("index"))
    try:
        config = EventoConfig(
            nombre=evento["name"],
            titulo=evento["title"],
            puerto=int(evento["port"]),
            tracker=evento["tracker"],
            sources=evento["sources"],
            host=evento["host"],
            bitrate=int(evento["bitrate"]),
            token=evento["service_access_token"],
            content_id=evento.get("content_id", ""),
            docker_active=evento.get("docker_active", "False")
        )
    except Exception as e:
        flash(f"Error al parsear evento: {e}", "danger")
        return redirect(url_for("index"))
    
    def run_docker():
        result = manager.iniciar_docker_evento(config)
        logger.info("Resultado de inicio en background: " + result)
    
    # Ejecutar en segundo plano
    thread = threading.Thread(target=run_docker)
    thread.start()
    flash(f"Proceso de inicio del contenedor para '{nombre}' iniciado en segundo plano", "info")
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
