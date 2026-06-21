import threading
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from datetime import datetime
import random
import asyncio
import json
import paho.mqtt.client as mqtt

app = FastAPI(title="Foll Device Simulator + MQTT", version="2.0.0")

FALL_TYPES = [
    {"id": 1, "name": "FRONTAL", "description": "Impacto frontal, probable lesión de cabeza/torax.", "severity_level": 1},
    {"id": 2, "name": "LATERAL", "description": "Caída lateral, posible fractura de cadera o costillas.", "severity_level": 2},
    {"id": 3, "name": "UNKNOWN", "description": "Tipo de caída no determinado por el modelo IA.", "severity_level": 2},
    {"id": 4, "name": "BACKWARD", "description": "Caída hacia atrás, riesgo de lesión cervical o cabeza.", "severity_level": 1},
]

def get_random_fall_type():
    """Devuelve un entry aleatorio del catálogo `FALL_TYPES`."""
    return random.choice(FALL_TYPES)

# -------------------------------------------------------------------------->>

# --- CONFIGURACIÓN MQTT DUAL ---
BROKER_IP = "192.168.0.10" # O localhost, dependiendo de dónde corra este script

# Cliente para el Backend (Publicador)
MQTT_PORT_BACK = 1883
mqtt_back = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "Foll_Edge_To_Back")

# Cliente para el IoT (Oyente)
MQTT_PORT_IOT = 1884
mqtt_iot = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "Foll_Edge_Listener")

# Estado en memoria (La única fuente de la verdad para el heartbeat)
devices_state = {
    1001: {
        "battery_level": 100,
        "is_charging": False,
        "location": {"lat": 0.0, "lng": 0.0},
        "is_active": True,
        "is_falling": False,
        "last_seen": None
    }
}
state_lock = asyncio.Lock()


def publish_mqtt_iot(topic: str, data: dict):
    """Auxiliar para mandar comandos de vuelta al hardware."""
    payload = json.dumps(data)
    mqtt_iot.publish(topic, payload, qos=1)
    print(f"🔊 MQTT PUBLISH (Hacia IoT) -> {topic} | Data: {payload}")

def publish_mqtt(topic: str, data: dict):
    """Auxiliar para mandar el JSON al broker del backend."""
    payload = json.dumps(data)
    mqtt_back.publish(topic, payload, qos=1) # ✅ Usamos el cliente del backend
    print(f"📡 MQTT PUBLISH (Back) -> {topic} | Data: {payload}")







# -------------------------------------------------------------------------->>

def evaluar_caida(state: dict) -> bool:
    """Evalúa si las lecturas de telemetría corresponden al patrón de caída."""
    acel = state.get("acel", {})
    giro = state.get("giro", {})
    
    try:
        # Validación estricta redondeada a 4 decimales
        return (
            round(acel.get("x", 0), 4) == -19.6133 and
            round(acel.get("y", 0), 4) == -19.6133 and
            round(acel.get("z", 0), 4) == 19.6133 and
            round(giro.get("x", 0), 4) == 4.3633 and
            round(giro.get("y", 0), 4) == -4.3633 and
            round(giro.get("z", 0), 4) == 4.3633
        )
    except Exception:
        return False
    


def on_message_iot(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        topic = msg.topic

        if topic == "iot/dispositivo_foll/telerimetria":
            device_id = int(payload.get("deviceId", 1001))
            if device_id in devices_state:
                # Datos básicos de conexión que SIEMPRE se actualizan
                devices_state[device_id]["battery_level"] = payload.get("bateria", 0)
                devices_state[device_id]["is_charging"] = payload.get("is_charging", False)
                devices_state[device_id]["last_seen"] = datetime.utcnow()
                
                # 🚨 SI ESTÁ EN MODO CAÍDA, SE IGNORA EL ANÁLISIS POSTERIOR Y NO SE ACTUALIZAN COORDENADAS
                if not devices_state[device_id]["is_falling"]:
                    gps = payload.get("gps", {})
                    devices_state[device_id]["location"]["lat"] = gps.get("lat", 0.0)
                    devices_state[device_id]["location"]["lng"] = gps.get("lon", 0.0)
                    
                    # Guardamos las fuerzas actuales para que las analice el heartbeat
                    devices_state[device_id]["acel"] = payload.get("acel", {})
                    devices_state[device_id]["giro"] = payload.get("giro", {})

        elif topic == "foll/devices/1001/power":
            device_id = payload.get("device_id")
            if device_id in devices_state:
                devices_state[device_id]["is_active"] = payload.get("is_active", False)
                # Espejo inmediato al backend
                publish_mqtt(f"foll/devices/{device_id}/power", payload)
                print(f"🔌 Power retransmitido -> {payload}")

        elif topic == "foll/devices/1001/cancel_events":
            device_id = int(payload.get("device_id", 1001))
            
            if device_id in devices_state:
                # Verificamos que venga explícitamente cancelado por el usuario
                if payload.get("is_cancelled_by_user") == True:
                    devices_state[device_id]["is_falling"] = False
                    print(f"🟢 [Edge] Caída cancelada por botón físico en hardware {device_id}. Reseteando estado.")
                    
                    # Espejo inmediato al backend para que limpie la alerta en web/móvil
                    back_payload = {
                        "device_id": device_id,
                        "timestamp": payload.get("timestamp", datetime.utcnow().isoformat() + "Z"),
                        "reason": "USER_BUTTON_PRESSED"
                    }
                    publish_mqtt(f"foll/devices/{device_id}/fall-cancelled", back_payload)

    except Exception as e:
        print(f"⚠️ Error procesando mensaje IoT: {e}")

mqtt_iot.on_message = on_message_iot

# --- 3. MODELOS DE RESPUESTA ---
class HeartbeatResponse(BaseModel):
    device_id: int
    battery_level: int
    is_charging: bool
    latitude: float
    longitude: float
    timestamp: str

# --- 4. LÓGICA DE ACTUALIZACIÓN DE ESTADO ---


async def heartbeat_loop():
    while True:
        for device_id, state in devices_state.items():
            if not state.get("is_active", False):
                continue

            # 🚨 ANALIZAR TELEMETRÍA (Solo si no está ya en caída)
            if not state["is_falling"]:
                if evaluar_caida(state):
                    async with state_lock:
                        state["is_falling"] = True
                    
                    # Notificar Caída al Backend (1883)
                    fall_type = get_random_fall_type()
                    fall_payload = {
                        "device_id": device_id,
                        "is_fall": True,
                        "ai_confidence_score": 1.0,
                        "latitude": state["location"]["lat"],
                        "longitude": state["location"]["lng"],
                        "is_cancelled_by_user": False,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "fall_type_id": fall_type["id"],
                    }
                    publish_mqtt(f"foll/devices/{device_id}/fall-detected", fall_payload)
                    
                    # Notificar Caída al Hardware (1884)
                    publish_mqtt_iot(f"iot/dispositivo_foll/{device_id}/comandos", {"accion": "caida"})
                    print(f"🚨 ¡CAÍDA DETECTADA AUTOMÁTICAMENTE EN DISPOSITIVO {device_id}!")

            # 📡 EL PAYLOAD SIGUE SIEMPRE (Reporta constantemente al Back con el estado actual)
            heartbeat_payload = {
                "device_id": device_id,
                "battery_level": state["battery_level"],
                "is_charging": state["is_charging"],
                "latitude": state["location"]["lat"],
                "longitude": state["location"]["lng"],
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
            publish_mqtt(f"foll/devices/{device_id}/heartbeat", heartbeat_payload)
            print(f"📡 Heartbeat enviado (Modo Caída: {state['is_falling']}) -> {heartbeat_payload}")
            
        await asyncio.sleep(5)


@app.post("/simulate/cancel-fall/{device_id}")
async def cancel_fall(device_id: int):
    """Simula o procesa la cancelación física/remota de la alerta."""
    if device_id not in devices_state:
        raise HTTPException(status_code=404, detail="Device not found")

    async with state_lock:
        if not devices_state[device_id].get("is_active", True):
            raise HTTPException(status_code=409, detail="Device is powered off")
        
        # Restablecemos estado para reactivar el análisis de coordenadas posteriores
        devices_state[device_id]["is_falling"] = False
    
    # Payload para desarmar backend
    payload = {
        "device_id": device_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "reason": "USER_BUTTON_PRESSED"
    }
    publish_mqtt(f"foll/devices/{device_id}/fall-cancelled", payload)
    
    # Payload para desarmar hardware
    publish_mqtt_iot(f"iot/dispositivo_foll/{device_id}/comandos", {"accion": "cancelar_caida"})
    
    return {"status": "Alerta cancelada. Modos reestablecidos."}


# --- 7. CICLO DE VIDA DE FASTAPI ---
@app.on_event("startup")
async def startup_event():
    mqtt_iot.connect(BROKER_IP, MQTT_PORT_IOT)
    mqtt_iot.subscribe("iot/dispositivo_foll/telerimetria")
    mqtt_iot.subscribe("foll/devices/1001/power")
    mqtt_iot.subscribe("foll/devices/1001/cancel_events") 
    
    mqtt_iot.loop_start()
    
    mqtt_back.connect(BROKER_IP, MQTT_PORT_BACK)
    mqtt_back.loop_start()
    
    async with state_lock: 
        pass
    asyncio.create_task(heartbeat_loop()) 
    print("✅ Capa Edge Real Operativa - Detección por Software integrada en el Heartbeat")
@app.on_event("shutdown")
def shutdown_event():
    mqtt_iot.loop_stop()
    mqtt_iot.disconnect()
    mqtt_back.loop_stop()
    mqtt_back.disconnect()