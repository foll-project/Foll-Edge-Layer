# Capa Edge — simulador de dispositivos Foll

Publica telemetría y caídas por MQTT hacia el broker (local o Azure).

## Arrancar simulador

```powershell
pip install -r requirements.txt   # si aplica
copy .env.example .env            # opcional
fastapi dev main.py
```

Swagger: http://127.0.0.1:8000/docs

## MQTT — dos modos

### A) Desarrollo local (broker en Docker, sin auth)

```powershell
cd ..\foll-mqtt-broker
docker compose up mosquitto-dev -d
```

`.env`:

```env
MQTT_BROKER=localhost
MQTT_PORT=1883
```

### B) Demo / producción (broker en Azure)

1. Despliega el broker: ver `foll-mqtt-broker/README.md` y `foll-backend/docs/azure-mqtt-setup.md`
2. Configura `.env`:

```env
MQTT_BROKER=foll-mqtt.eastus.azurecontainer.io
MQTT_PORT=1883
MQTT_USERNAME=foll_mqtt
MQTT_PASSWORD=tu-password
```

El backend en Azure (`foll-backend-iot`) debe usar las mismas credenciales vía App Settings (`Mqtt__*`, `EmergencyAnalyticsMqtt__*`).

## Variables de entorno

| Variable | Default | Descripción |
|----------|---------|-------------|
| `MQTT_BROKER` | `localhost` | Host del broker |
| `MQTT_PORT` | `1883` | Puerto TCP |
| `MQTT_USERNAME` | *(vacío)* | Usuario MQTT (Azure) |
| `MQTT_PASSWORD` | *(vacío)* | Contraseña MQTT |

## Tópicos publicados

- `foll/devices/{id}/heartbeat`
- `foll/devices/{id}/power`
- `foll/devices/{id}/fall-detected`
- `foll/devices/{id}/fall-cancelled`

## MQTT Explorer (opcional)

Conecta al mismo host/puerto/credenciales para ver mensajes en vivo.
