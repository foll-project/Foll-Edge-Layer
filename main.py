from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from datetime import datetime
import random
import asyncio
import json
import paho.mqtt.client as mqtt

app = FastAPI(title="Foll Device Simulator + MQTT", version="2.0.0")

# --- 1. POOL DE UBICACIONES REALES (Lima, Perú) ---
REAL_LOCATIONS = [
    {"name": "Surco (Cerca a UPC Monterrico)", "lat": -12.1041, "lng": -76.9631},
    {"name": "Miraflores (Parque Kennedy)", "lat": -12.1213, "lng": -77.0296},
    {"name": "San Borja (Cerca a La Rambla)", "lat": -12.0833, "lng": -76.9961},
    {"name": "Jesús María (Hospital Rebagliati)", "lat": -12.0728, "lng": -77.0411},
    {"name": "La Molina (Molicentro)", "lat": -12.0714, "lng": -76.9175}
]

# --- 2. ESTADO EN MEMORIA (Simulando la BD) ---
devices_state = {
    1001: {
        "device_id": 1001,
        "battery_level": 90,
        "is_charging": False,
        "location": REAL_LOCATIONS[0],
        "is_active": True
    },
    1002: {
        "device_id": 1002,
        "battery_level": 50,
        "is_charging": True, # Este está cargando
        "location": REAL_LOCATIONS[1],
        "is_active": True
    },
 
}

# -------------------------------------------------------------------------->>

# --- CONFIGURACIÓN MQTT ---
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "Foll_Sim_Cinturones")
state_lock = asyncio.Lock()

# @app.on_event("startup")
def startup_event():
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT)
    mqtt_client.loop_start()
    print("✅ Conectado al Broker MQTT")

# @app.on_event("shutdown")
def shutdown_event():
    mqtt_client.loop_stop()
    mqtt_client.disconnect()

# -------------------------------------------------------------------------->>

# --- 3. MODELOS DE RESPUESTA ---
class HeartbeatResponse(BaseModel):
    device_id: int
    battery_level: int
    is_charging: bool
    latitude: float
    longitude: float
    timestamp: str

# --- 4. LÓGICA DE ACTUALIZACIÓN DE ESTADO ---

def publish_mqtt(topic: str, data: dict):
    """Auxiliar para mandar el JSON al broker."""
    payload = json.dumps(data)
    mqtt_client.publish(topic, payload, qos=1)
    print(f"📡 MQTT PUBLISH -> {topic} | Data: {payload}")

def update_device_physics(device_id: int):
    device = devices_state[device_id]

    if not device.get("is_active", True):
        return
    
    # Lógica de Batería
    if device["is_charging"]:
        # Sube entre 5% y 10% (Carga rápida simulada)
        device["battery_level"] = min(100, device["battery_level"] + random.randint(5, 10))
    else:
        # Baja entre 1% y 3%
        device["battery_level"] = max(0, device["battery_level"] - random.randint(1, 5))
    
    # Lógica de Ubicación (Pequeño movimiento aleatorio para simular GPS real)
    # Seleccionamos una nueva ubicación del pool a veces o fluctuamos la actual
    if random.random() > 0.7: # 30% de probabilidad de que se mueva a otro sitio real
        device["location"] = random.choice(REAL_LOCATIONS)
    else:
        # Solo fluctúa un poquito la posición actual
        device["location"]["lat"] += random.uniform(-0.0001, 0.0001)
        device["location"]["lng"] += random.uniform(-0.0001, 0.0001)


async def heartbeat_loop():
    """Envía heartbeats de todos los dispositivos cada 10 segundos."""
    while True:
        device_ids = list(devices_state.keys())
        for device_id in device_ids:
            async with state_lock:
                if device_id not in devices_state:
                    continue

                state = devices_state[device_id]
                if not state.get("is_active", True):
                    continue

                update_device_physics(device_id)
                state = devices_state[device_id]

                payload = {
                    "device_id": device_id,
                    "battery_level": state["battery_level"],
                    "is_charging": state["is_charging"],
                    "latitude": state["location"]["lat"],
                    "longitude": state["location"]["lng"],
                    "timestamp": datetime.utcnow().isoformat() + "Z"
                }

            topic = f"foll/devices/{device_id}/heartbeat"
            publish_mqtt(topic, payload)

        await asyncio.sleep(10)

@app.get("/status")
def get_all_devices_status():
    """Mira cómo están los 3 dispositivos en este momento."""
    return devices_state

@app.post("/simulate/heartbeat/{device_id}", response_model=HeartbeatResponse)
async def send_heartbeat(device_id: int):
    if device_id not in devices_state:
        raise HTTPException(status_code=404, detail="Device not found")
    
    # Actualizamos los números antes de enviarlos
    async with state_lock:
        if not devices_state[device_id].get("is_active", True):
            raise HTTPException(status_code=409, detail="Device is powered off")

        update_device_physics(device_id)
        state = devices_state[device_id]
    
    payload = {
        "device_id": device_id,
        "battery_level": state["battery_level"],
        "is_charging": state["is_charging"],
        "latitude": state["location"]["lat"],
        "longitude": state["location"]["lng"],
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    
    # PUBLICAMOS AL TÓPICO DE TELEMETRÍA
    publish_mqtt(f"foll/devices/{device_id}/heartbeat", payload)

    return payload

@app.post("/simulate/fall/{device_id}")
async def trigger_fall(device_id: int, confidence: float = 0.98):
    if device_id not in devices_state:
        raise HTTPException(status_code=404, detail="Device not found")
    
    async with state_lock:
        if not devices_state[device_id].get("is_active", True):
            raise HTTPException(status_code=409, detail="Device is powered off")
        state = devices_state[device_id]
    # Una caída no suele ocurrir cargando, así que forzamos is_charging=False
    payload = {
        "device_id": device_id,
        "is_fall": True,
        "ai_confidence_score": confidence,
        "latitude": state["location"]["lat"],
        "longitude": state["location"]["lng"],
        "is_cancelled_by_user": False,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    
    # PUBLICAMOS AL TÓPICO DE EMERGENCIA
    publish_mqtt(f"foll/devices/{device_id}/fall-detected", payload)
    return payload

@app.post("/simulate/toggle-charging/{device_id}")
async def toggle_charging(device_id: int):
    """Para que puedas probar qué pasa cuando lo conectan o desconectan."""
    if device_id not in devices_state:
        raise HTTPException(status_code=404, detail="Device not found")
    
    async with state_lock:
        if not devices_state[device_id].get("is_active", True):
            raise HTTPException(status_code=409, detail="Device is powered off")

        devices_state[device_id]["is_charging"] = not devices_state[device_id]["is_charging"]
        is_charging = devices_state[device_id]["is_charging"]

    

    status = "CARGANDO" if is_charging else "DESCONECTADO"
    return {"message": f"El dispositivo {device_id} ahora está {status}"}

@app.post("/simulate/power/{device_id}/on")
async def power_on(device_id: int):
    if device_id not in devices_state:
        raise HTTPException(status_code=404, detail="Device not found")

    async with state_lock:
        devices_state[device_id]["is_active"] = True

    publish_mqtt(
        f"foll/devices/{device_id}/power",
        {
            "device_id": device_id,
            "is_active": True,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        },
    )

    return {"device_id": device_id, "is_active": True}

@app.post("/simulate/power/{device_id}/off")
async def power_off(device_id: int):
    if device_id not in devices_state:
        raise HTTPException(status_code=404, detail="Device not found")

    async with state_lock:
        devices_state[device_id]["is_active"] = False

    publish_mqtt(
        f"foll/devices/{device_id}/power",
        {
            "device_id": device_id,
            "is_active": False,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        },
    )

    return {"device_id": device_id, "is_active": False}

@app.post("/simulate/charging/{device_id}/{mode}")
async def set_charging(device_id: int, mode: str):
    if device_id not in devices_state:
        raise HTTPException(status_code=404, detail="Device not found")

    if mode not in ("on", "off"):
        raise HTTPException(status_code=400, detail="mode must be 'on' or 'off'")

    async with state_lock:
        if not devices_state[device_id].get("is_active", True):
            raise HTTPException(status_code=409, detail="Device is powered off")
        devices_state[device_id]["is_charging"] = (mode == "on")

    return {"device_id": device_id, "is_charging": (mode == "on")}

@app.post("/simulate/cancel-fall/{device_id}")
async def cancel_fall(device_id: int):
    """Simula que el usuario presionó el botón físico para cancelar una caída detectada."""
    if device_id not in devices_state:
        raise HTTPException(status_code=404)

    if not devices_state[device_id].get("is_active", True):
        raise HTTPException(status_code=409, detail="Device is powered off")
    
    payload = {
        "device_id": device_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "reason": "USER_BUTTON_PRESSED"
    }
    publish_mqtt(f"foll/devices/{device_id}/fall-cancelled", payload)
    return {"status": "Alerta cancelada enviada"}


@app.on_event("startup")
async def startup_event():
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT)
    mqtt_client.loop_start()
    # ESTA LÍNEA ES VITAL PARA QUE EL BUCLE EMPIECE
    asyncio.create_task(heartbeat_loop()) 
    print("✅ Conectado y automatización iniciada")

@app.on_event("shutdown")
def shutdown_event():
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
