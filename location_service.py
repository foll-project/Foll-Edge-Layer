import os
import subprocess
import platform
import requests
import re

# Ubicación fija solo si FOLL_DEV_MODE=true (pruebas sin Google).
DEV_LOCATION = {
    "latitude": -12.0464,
    "longitude": -77.0428,
    "address": "Lima, Perú (ubicación fija — modo desarrollo)",
}

DEFAULT_GOOGLE_MAPS_API_KEY = "AIzaSyBDq3aQg2_PNMX-GiC1Y-d1oeZW4V2wXhs"


class LocationService:
    def __init__(self, api_key: str | None = None, dev_mode: bool = False):
        """
        Geolocalización vía Google Maps (WiFi + reverse geocoding).
        Con dev_mode=True retorna DEV_LOCATION sin llamadas externas.
        """
        self.api_key = api_key or os.getenv("GOOGLE_MAPS_API_KEY", DEFAULT_GOOGLE_MAPS_API_KEY)
        self.dev_mode = dev_mode


    def get_wifi_networks(self):
        """
        Escanea las redes WiFi cercanas. 
        VERSIÓN CON DEBUGGING ACTIVADO PARA RASTREAR EL ERROR.
        """
        wifi_list = []
        os_name = platform.system()
        
        try:
            if os_name == "Windows":
                print("🔍 [DEBUG] Ejecutando escaneo WiFi en Windows con netsh...")
                result = subprocess.run(["netsh", "wlan", "show", "networks", "mode=bssid"], capture_output=True, text=True, errors="ignore")
                output = result.stdout
                
                print("\n=== 🖥️ RAW OUTPUT DE WINDOWS (Lo que ve Python) ===")
                # Imprimimos lo que Windows respondió (si está vacío, el problema es el adaptador WiFi)
                if not output.strip():
                    print("[VÍCIO / NADA]")
                else:
                    # Imprimimos los primeros 800 caracteres para no saturar la consola
                    print(output[:800] + "\n... [Output truncado]")
                print("====================================================\n")
                
                # Expresión regular para capturar los BSSID (MAC addresses)
                bssids = re.findall(r"BSSID\s+\d+\s*:\s*([0-9a-fA-F:]{17})", output)
                
                # Capturar las potencias de señal
                signals = re.findall(r"(?:Se.*al|Signal)\s*:\s*(\d+)%", output)
                
                print(f"📊 [DEBUG REGEX] BSSIDs detectados: {len(bssids)} | Señales detectadas: {len(signals)}\n")
                
                # Empaquetar en el formato estricto que exige la API de Google Geolocation
                for i in range(min(len(bssids), len(signals))):
                    mac = bssids[i]
                    percentage = int(signals[i])
                    dbm = int((percentage / 2) - 100)
                    
                    wifi_list.append({
                        "macAddress": mac,
                        "signalStrength": dbm
                    })
            
            elif os_name == "Linux":
                print("📡 [Location] Escaneo WiFi en Linux (Raspberry Pi) detectado.")
                pass
                
        except Exception as e:
            print(f"⚠️ Error crítico escaneando redes WiFi: {e}")
            
        return wifi_list

    def get_location_google(self):
        """
        Envía las MACs recolectadas a Google Geolocation API para obtener Lat/Lng exactos.
        """
        wifi_points = self.get_wifi_networks()
        
        if not wifi_points:
            print("⚠️ [Location] No se detectaron redes WiFi cercanas mediante hardware.")
            return None, None
            
        # Payload estructurado para Google Maps
        payload = {
            "considerIp": "true", # Si los puntos WiFi son insuficientes, Google usa su base de datos de IPs
            "wifiAccessPoints": wifi_points
        }
        
        try:
            url = f"https://www.googleapis.com/geolocation/v1/geolocate?key={self.api_key}"
            response = requests.post(url, json=payload, timeout=6)
            if response.status_code == 200:
                data = response.json()
                return data["location"]["lat"], data["location"]["lng"]
            else:
                print(f"⚠️ Error en Google Geolocation API (Status: {response.status_code}): {response.text}")
        except Exception as e:
            print(f"⚠️ Fallo de conexión con Google Geolocation: {e}")
        return None, None

    def get_address_google(self, lat, lon):
        """
        Reverse Geocoding: Convierte las coordenadas Lat/Lng en una dirección postal exacta.
        """
        try:
            # Agregamos &language=es para que Google responda con el formato oficial de Perú
            url = f"https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lon}&language=es&key={self.api_key}"
            response = requests.get(url, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                if data["status"] == "OK" and len(data["results"]) > 0:
                    # El primer resultado [0] siempre contiene la dirección más específica (calle + número)
                    return data["results"][0]["formatted_address"]
                else:
                    print(f"⚠️ Google Geocoding retornó un estado anómalo: {data['status']}")
            else:
                print(f"⚠️ Error de red en Google Geocoding (Status: {response.status_code})")
        except Exception as e:
            print(f"⚠️ Error crítico en Google Geocoding API: {e}")
        return "Dirección exacta no disponible (Error de Geocoding)"

    def get_location_fallback_ip(self):
        """
        Plan C: IP-API gratuito de respaldo por si Google llega a fallar o no hay API Key válida.
        """
        try:
            response = requests.get("http://ip-api.com/json/", timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data["status"] == "success":
                    return data["lat"], data["lon"]
        except Exception:
            pass
        return -12.0464, -77.0428 # Coordenadas de Lima Centro por defecto si todo el internet muere

    def update_edge_location(self):
        """
        Orquestador principal: Obtiene coordenadas precisas y dirección legible por humanos.
        """
        if self.dev_mode:
            print("📍 [DEV] Usando ubicación hardcodeada (sin llamada a Google).")
            return dict(DEV_LOCATION)

        print("🚀 [Google Maps] Solicitando geolocalización de alta precisión...")
        lat, lon = self.get_location_google()
        
        if lat is None or lon is None:
            print("⚠️ [Location] Google Geolocation falló o no está configurado. Usando IP de emergencia...")
            lat, lon = self.get_location_fallback_ip()
            address = "Ubicación aproximada (Respaldo por IP asignada)"
        else:
            print(f"✅ [Google Maps] Coordenadas obtenidas con éxito: Lat {lat}, Lng {lon}")
            address = self.get_address_google(lat, lon)
            print(f"✅ [Google Maps] Dirección estructurada correctamente.")
            
        return {
            "latitude": lat,
            "longitude": lon,
            "address": address
        }