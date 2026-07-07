import os
from fastapi import FastAPI, HTTPException, BackgroundTasks
from datetime import datetime
import random
import asyncio
import json
import paho.mqtt.client as mqtt
from location_service import LocationService

# 1. IMPORTAR TU IA
from logic import FallDetector

app = FastAPI(title="Foll Device Simulator + MQTT", version="2.0.0")

# 2. INICIALIZAR EL DETECTOR GLOBALMENTE
# Al arrancar la app, el TFLite se carga en memoria y se queda listo
detector = FallDetector(model_path="foll_cnn_v1.tflite", meta_path="foll_preprocessing.json")

FALL_TYPES = [
    {"id": 1, "name": "FRONTAL", "description": "Impacto frontal, probable lesión de cabeza/torax.", "severity_level": 1},
    {"id": 2, "name": "LATERAL", "description": "Caída lateral, posible fractura de cadera o costillas.", "severity_level": 2},
    {"id": 3, "name": "UNKNOWN", "description": "Tipo de caída no determinado por el modelo IA.", "severity_level": 2},
    {"id": 4, "name": "BACKWARD", "description": "Caída hacia atrás, riesgo de lesión cervical o cabeza.", "severity_level": 1},
]

def get_random_fall_type():
    return random.choice(FALL_TYPES)

DEV_MODE = os.getenv("FOLL_DEV_MODE", "true").lower() in ("1", "true", "yes")

BROKER_IP = os.getenv("MQTT_BROKER_HOST", "127.0.0.1")
MQTT_PORT_BACK = int(os.getenv("MQTT_BACK_PORT", "1884"))
mqtt_back = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "Foll_Edge_To_Back")

MQTT_PORT_IOT = int(os.getenv("MQTT_IOT_PORT", "1884"))
mqtt_iot = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "Foll_Edge_Listener")

IOT_TOPIC_PREFIX = "iot/dispositivo_foll/"

devices_state = {}
edge_location = {"lat": 0.0, "lng": 0.0, "address": ""}
state_lock = asyncio.Lock()


def parse_iot_device_id(topic: str) -> int | None:
    """Extrae device_id de iot/dispositivo_foll/{id}/..."""
    if not topic.startswith(IOT_TOPIC_PREFIX):
        return None
    parts = topic.split("/")
    if len(parts) < 4:
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def parse_foll_device_id(topic: str) -> int | None:
    """Extrae device_id de foll/devices/{id}/..."""
    parts = topic.split("/")
    if len(parts) < 3 or parts[0] != "foll" or parts[1] != "devices":
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def ensure_device(device_id: int) -> dict:
    if device_id not in devices_state:
        devices_state[device_id] = {
            "battery_level": 100,
            "is_charging": False,
            "location": {"lat": edge_location["lat"], "lng": edge_location["lng"]},
            "address": edge_location.get("address", ""),
            "is_active": True,
            "is_falling": False,
            "last_seen": None,
        }
        print(f"📱 Dispositivo registrado en Edge: {device_id}")
    return devices_state[device_id]

def publish_mqtt_iot(topic: str, data: dict):
    payload = json.dumps(data)
    mqtt_iot.publish(topic, payload, qos=1)
    print(f"🔊 MQTT PUBLISH (Hacia IoT) -> {topic} | Data: {payload}")

def publish_mqtt(topic: str, data: dict):
    payload = json.dumps(data)
    mqtt_back.publish(topic, payload, qos=1)
    print(f"📡 MQTT PUBLISH (Back) -> {topic} | Data: {payload}")


def handle_user_cancel(device_id: int, timestamp: str | None = None) -> bool:
    """Cierra la alerta activa: backend + comando al ESP32 para apagar buzzer."""
    state = devices_state.get(device_id)
    if not state or not state.get("is_falling"):
        print(f"⚠️ [Edge] Cancel ignorada: no hay caída activa (dispositivo {device_id})")
        return False

    state["is_falling"] = False
    print(f"🟢 [Edge] Caída cancelada (dispositivo {device_id}). Reseteando estado IA.")

    ts = timestamp or datetime.utcnow().isoformat() + "Z"
    back_payload = {
        "device_id": device_id,
        "timestamp": ts,
        "reason": "USER_BUTTON_PRESSED",
    }
    publish_mqtt(f"foll/devices/{device_id}/fall-cancelled", back_payload)
    publish_mqtt_iot(f"iot/dispositivo_foll/{device_id}/comandos", {"accion": "cancelar_caida"})
    return True


# --- CALLBACK PRINCIPAL MQTT ---
def on_message_iot(client, userdata, msg):
    topic = msg.topic

    # CASO 1: ventana binaria de acelerómetro (ID en el tópico)
    if topic.endswith("/ventana"):
        device_id = parse_iot_device_id(topic)
        if device_id is None:
            print(f"⚠️ Tópico ventana sin device_id válido: {topic}")
            return

        state = ensure_device(device_id)
        print(f"\n📥 [MQTT] Dispositivo {device_id} — {len(msg.payload)} bytes de acelerómetro.")

        resultado = detector.process(msg.payload)

        if not resultado["ok"]:
            return

        is_fall = resultado["is_fall"]
        proba = resultado["proba"]
        print(f"🧠 [IA] Dispositivo {device_id} -> Caída: {is_fall} | Probabilidad: {proba:.4f}")

        if is_fall:
            if state.get("is_falling"):
                print(f"⏸️ [Edge] Caída ya activa (dispositivo {device_id}), ignorando duplicado.")
                return

            state["is_falling"] = True

            publish_mqtt_iot(f"iot/dispositivo_foll/{device_id}/comandos", {"accion": "caida"})

            fall_type = get_random_fall_type()
            fall_payload = {
                "device_id": device_id,
                "is_fall": True,
                "ai_confidence_score": round(proba, 4),
                "latitude": state["location"]["lat"],
                "longitude": state["location"]["lng"],
                "address": state.get("address", ""),
                "is_cancelled_by_user": False,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "fall_type_id": fall_type["id"],
            }
            publish_mqtt(f"foll/devices/{device_id}/fall-detected", fall_payload)
            print(f"🚨 ¡ALERTA DISPARADA (dispositivo {device_id}) HACIA EL BACKEND! 🚨")

        return

    # CASO 2: mensajes JSON (telemetría, power, cancel)
    try:
        payload = json.loads(msg.payload.decode())
        device_id = parse_iot_device_id(topic) or parse_foll_device_id(topic)
        payload_device_id = payload.get("device_id")

        if payload_device_id is not None and device_id is not None:
            if int(payload_device_id) != device_id:
                print(f"⚠️ device_id inconsistente: tópico={device_id}, payload={payload_device_id}")
                return
            device_id = int(payload_device_id)
        elif device_id is None and payload_device_id is not None:
            device_id = int(payload_device_id)
        elif device_id is None:
            print(f"⚠️ Mensaje JSON sin device_id identificable: {topic}")
            return

        ensure_device(device_id)

        if topic.endswith("/telemetria"):
            devices_state[device_id]["battery_level"] = payload.get("bateria", devices_state[device_id]["battery_level"])
            devices_state[device_id]["is_charging"] = payload.get("is_charging", False)
            devices_state[device_id]["last_seen"] = datetime.utcnow()

        elif topic.endswith("/power"):
            devices_state[device_id]["is_active"] = payload.get("is_active", False)
            publish_mqtt(f"foll/devices/{device_id}/power", payload)

        elif topic.endswith("/cancelar"):
            if payload.get("is_cancelled_by_user") is True:
                handle_user_cancel(
                    device_id,
                    payload.get("timestamp"),
                )

    except Exception as e:
        print(f"⚠️ Error procesando mensaje IoT JSON: {e}")

mqtt_iot.on_message = on_message_iot


# --- HEARTBEAT LOOP (LIMPIO, SIN LÓGICA DE DETECCIÓN MOCK) ---
async def heartbeat_loop():
    while True:
        for device_id, state in devices_state.items():
            if not state.get("is_active", False):
                continue

            # Ahora el heartbeat SOLO reporta estado. La detección ya ocurrió arriba en el evento MQTT.
            heartbeat_payload = {
                "device_id": device_id,
                "battery_level": state["battery_level"],
                "is_charging": state["is_charging"],
                "latitude": state["location"]["lat"],
                "longitude": state["location"]["lng"],
                "is_falling": state["is_falling"], # Le avisamos al back si estamos en emergencia
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
            publish_mqtt(f"foll/devices/{device_id}/heartbeat", heartbeat_payload)
            # print(f"📡 Heartbeat enviado -> Batería: {state['battery_level']}% | Caída: {state['is_falling']}")
            
        await asyncio.sleep(5)


@app.post("/simulate/cancel-fall/{device_id}")
async def cancel_fall(device_id: int):
    if device_id not in devices_state:
        raise HTTPException(status_code=404, detail="Device not found")

    async with state_lock:
        if not devices_state[device_id].get("is_active", True):
            raise HTTPException(status_code=409, detail="Device is powered off")

    if not handle_user_cancel(device_id):
        raise HTTPException(status_code=409, detail="No active fall alert to cancel")

    return {"status": "Alerta cancelada. IA lista para nueva detección."}


@app.on_event("startup")
async def startup_event():
    global edge_location

    if DEV_MODE:
        print("🔧 Modo desarrollo activo (FOLL_DEV_MODE=true). MQTT local + ubicación hardcodeada.")
    loc_service = LocationService(dev_mode=DEV_MODE)
    ubicacion = loc_service.update_edge_location()

    edge_location = {
        "lat": ubicacion["latitude"],
        "lng": ubicacion["longitude"],
        "address": ubicacion["address"],
    }
    print(f"📍 [Capa Edge] Ubicación de la estación: {ubicacion['address']}")

    mqtt_iot.connect(BROKER_IP, MQTT_PORT_IOT)

    mqtt_iot.subscribe(f"{IOT_TOPIC_PREFIX}+/ventana")
    mqtt_iot.subscribe(f"{IOT_TOPIC_PREFIX}+/telemetria")
    mqtt_iot.subscribe("foll/devices/+/power")
    mqtt_iot.subscribe(f"{IOT_TOPIC_PREFIX}+/cancelar")
    
    mqtt_iot.loop_start()
    
    mqtt_back.connect(BROKER_IP, MQTT_PORT_BACK)
    mqtt_back.loop_start()
    
    asyncio.create_task(heartbeat_loop()) 
    print("✅ Capa Edge Real Operativa con TensorFlow Lite activado.")

@app.on_event("shutdown")
def shutdown_event():
    mqtt_iot.loop_stop()
    mqtt_iot.disconnect()
    mqtt_back.loop_stop()
    mqtt_back.disconnect()