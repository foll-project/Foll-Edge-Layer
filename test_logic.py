import numpy as np
from logic import FallDetector

det = FallDetector()

# Simular una ventana de "reposo": y≈-1g (gravedad), x,z≈0, en g
reposo = np.zeros((400, 3), dtype=np.float32)
reposo[:, 1] = -1.0
# Empaquetar igual que el ESP32: [400 X][400 Y][400 Z] little-endian
fake_bytes = np.concatenate([reposo[:,0], reposo[:,1], reposo[:,2]]).astype("<f4").tobytes()

print("Bytes simulados:", len(fake_bytes), "(esperado 4800)")
resultado = det.process(fake_bytes)
print("Resultado reposo:", resultado)
print("-> Esperado: is_fall=False, proba baja (cerca de 0)")