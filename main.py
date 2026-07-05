import threading
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
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

BROKER_IP = "127.0.0.1"

MQTT_PORT_BACK = 1884
mqtt_back = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "Foll_Edge_To_Back")

MQTT_PORT_IOT = 1884
mqtt_iot = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "Foll_Edge_Listener")

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
    payload = json.dumps(data)
    mqtt_iot.publish(topic, payload, qos=1)
    print(f"🔊 MQTT PUBLISH (Hacia IoT) -> {topic} | Data: {payload}")

def publish_mqtt(topic: str, data: dict):
    payload = json.dumps(data)
    mqtt_back.publish(topic, payload, qos=1)
    print(f"📡 MQTT PUBLISH (Back) -> {topic} | Data: {payload}")


# --- CALLBACK PRINCIPAL MQTT ---
def on_message_iot(client, userdata, msg):
    topic = msg.topic
    device_id = 1001 # Hardcodeado por ahora según tu ESP32
    
    # 🚨 CASO 1: LLEGA LA VENTANA BINARIA DE LA IA (Cuidado: NO es JSON)
    if topic == "iot/dispositivo_foll/ventana":
        # Ignorar si ya estamos en estado de caída (para no saturar)
        #if devices_state[device_id]["is_falling"]:
            #return 
            
        print(f"\n📥 [MQTT] Recibidos {len(msg.payload)} bytes de acelerómetro.")
        
        # Le pasamos la memoria cruda a la IA
        resultado = detector.process(msg.payload)
        
        if not resultado["ok"]:
            return # Fallo al decodificar o tamaño incorrecto
            
        # Logging de la predicción en tiempo real
        is_fall = resultado["is_fall"]
        proba = resultado["proba"]
        print(f"🧠 [IA] Predicción -> Caída: {is_fall} | Probabilidad: {proba:.4f}")
        
        if is_fall:
            # ¡LA IA DETECTÓ CAÍDA! Cambiamos el estado (sin lock porque MQTT corre en su thread)
            devices_state[device_id]["is_falling"] = True
            
            # 1. Hacer sonar el Buzzer en el ESP32
            publish_mqtt_iot(f"iot/dispositivo_foll/{device_id}/comandos", {"accion": "caida"})
            
            # 2. Generar el log / payload que irá hacia Azure después
            fall_type = get_random_fall_type()
            fall_payload = {
                "device_id": device_id,
                "is_fall": True,
                "ai_confidence_score": round(proba, 4), # Usamos la confianza REAL del modelo
                "latitude": devices_state[device_id]["location"]["lat"],
                "longitude": devices_state[device_id]["location"]["lng"],
                "is_cancelled_by_user": False,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "fall_type_id": fall_type["id"],
            }
            # Por ahora lo publicamos en el local back (1883), luego será en Azure
            publish_mqtt(f"foll/devices/{device_id}/fall-detected", fall_payload)
            print(f"🚨 ¡ALERTA DISPARADA HACIA EL BACKEND! 🚨")
            
        return # Importante: Salimos aquí para que NO intente decodificar JSON abajo

    # 🟢 CASO 2: LLEGA TELEMETRÍA JSON NORMAL
    try:
        payload = json.loads(msg.payload.decode())

        if topic == "iot/dispositivo_foll/telerimetria":
            if device_id in devices_state:
                devices_state[device_id]["battery_level"] = payload.get("bateria", 0)
                devices_state[device_id]["is_charging"] = payload.get("is_charging", False)
                devices_state[device_id]["last_seen"] = datetime.utcnow()
                
                if not devices_state[device_id]["is_falling"]:
                    gps = payload.get("gps", {})
                    devices_state[device_id]["location"]["lat"] = gps.get("lat", 0.0)
                    devices_state[device_id]["location"]["lng"] = gps.get("lon", 0.0)

        elif topic == "foll/devices/1001/power":
            if device_id in devices_state:
                devices_state[device_id]["is_active"] = payload.get("is_active", False)
                publish_mqtt(f"foll/devices/{device_id}/power", payload)

        elif topic == "foll/devices/1001/cancel_events":
            if device_id in devices_state and payload.get("is_cancelled_by_user") == True:
                devices_state[device_id]["is_falling"] = False
                print(f"🟢 [Edge] Caída cancelada. Reseteando estado IA.")
                back_payload = {
                    "device_id": device_id,
                    "timestamp": payload.get("timestamp", datetime.utcnow().isoformat() + "Z"),
                    "reason": "USER_BUTTON_PRESSED"
                }
                publish_mqtt(f"foll/devices/{device_id}/fall-cancelled", back_payload)

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
        devices_state[device_id]["is_falling"] = False
    
    payload = {
        "device_id": device_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "reason": "USER_BUTTON_PRESSED"
    }
    publish_mqtt(f"foll/devices/{device_id}/fall-cancelled", payload)
    publish_mqtt_iot(f"iot/dispositivo_foll/{device_id}/comandos", {"accion": "cancelar_caida"})
    return {"status": "Alerta cancelada. IA lista para nueva detección."}


@app.on_event("startup")
async def startup_event():
    print("📍 Inicializando servicio de geolocalización de alta precisión...")
    loc_service = LocationService()
    ubicacion = loc_service.update_edge_location()
    
    devices_state[1001]["location"]["lat"] = ubicacion["latitude"]
    devices_state[1001]["location"]["lng"] = ubicacion["longitude"]
    devices_state[1001]["address"] = ubicacion["address"]
    print(f"📍 [Capa Edge] Ubicación fija de la estación: {ubicacion['address']}")


    mqtt_iot.connect(BROKER_IP, MQTT_PORT_IOT)
    
    # 🚨 LA SUSCRIPCIÓN MÁS IMPORTANTE AHORA:
    mqtt_iot.subscribe("iot/dispositivo_foll/ventana") 
    
    mqtt_iot.subscribe("iot/dispositivo_foll/telerimetria")
    mqtt_iot.subscribe("foll/devices/1001/power")
    mqtt_iot.subscribe("foll/devices/1001/cancel_events") 
    
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