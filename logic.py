import json
import numpy as np
from scipy.signal import butter, sosfilt
import tensorflow as tf


class FallDetector:
    def __init__(self, model_path="foll_cnn_v1.tflite", meta_path="foll_preprocessing.json"):
        # --- Cargar metadatos del preprocesamiento (mean/std/umbral/etc.) ---
        with open(meta_path, "r") as f:
            meta = json.load(f)

        self.fs        = meta["fs"]                       # 200 Hz
        self.window    = meta["window"]                   # 400 muestras
        self.n_ch      = len(meta["channels"])            # 3 (adxl x,y,z)
        self.mean      = np.array(meta["mean"], dtype=np.float32)   # (3,)
        self.std       = np.array(meta["std"],  dtype=np.float32)   # (3,)
        self.threshold = meta["threshold"]                # 0.5
        cutoff         = meta["butterworth"]["cutoff_hz"] # 5.0
        order          = meta["butterworth"]["order"]     # 4

        # --- Filtro Butterworth 5Hz en formato SOS (estable) ---
        # OJO: en vivo usamos sosfilt (CAUSAL), no sosfiltfilt (que mira el futuro)
        self.sos = butter(order, cutoff, btype="low", fs=self.fs, output="sos")

        # --- Cargar el modelo TFLite ---
        self.interpreter = tf.lite.Interpreter(model_path=model_path)
        self.interpreter.allocate_tensors()
        self.inp = self.interpreter.get_input_details()[0]
        self.out = self.interpreter.get_output_details()[0]

        print(f"[FallDetector] Listo. Ventana={self.window}, canales={self.n_ch}, "
              f"umbral={self.threshold}")

    # ---------- 1. Decodificar los 4800 bytes ----------
    def decode(self, raw_bytes):
        """
        El ESP32 manda: [400 floats X][400 floats Y][400 floats Z] en little-endian.
        Devuelve un array (400, 3) o None si el tamaño no cuadra.
        """
        expected = self.window * self.n_ch * 4  # 400*3*4 = 4800 bytes
        if len(raw_bytes) != expected:
            print(f"[FallDetector] Tamaño inesperado: {len(raw_bytes)} (esperado {expected})")
            return None

        flat = np.frombuffer(raw_bytes, dtype="<f4")   # little-endian float32
        # flat = [x0..x399, y0..y399, z0..z399] -> reshape a (3, 400) -> transponer a (400, 3)
        window = flat.reshape(self.n_ch, self.window).T.astype(np.float32)
        return window

    # ---------- 2. Filtrar + normalizar ----------
    def preprocess(self, window):
        """window (400,3) -> filtrado + normalizado (400,3)."""
        filtered = np.empty_like(window)
        for c in range(self.n_ch):
            filtered[:, c] = sosfilt(self.sos, window[:, c])   # causal
        normalized = (filtered - self.mean) / self.std
        return normalized.astype(np.float32)

    # ---------- 3. Inferencia ----------
    def predict_proba(self, window_norm):
        """window_norm (400,3) -> probabilidad de caída (float 0..1)."""
        x = window_norm[np.newaxis, :, :]   # (1, 400, 3)
        self.interpreter.set_tensor(self.inp["index"], x)
        self.interpreter.invoke()
        return float(self.interpreter.get_tensor(self.out["index"])[0, 0])

    # ---------- API pública: de bytes a veredicto ----------
    def process(self, raw_bytes):
        """
        Recibe los bytes crudos del MQTT y devuelve un dict con el resultado.
        {'ok': bool, 'is_fall': bool, 'proba': float}
        """
        window = self.decode(raw_bytes)
        m = window.mean(axis=0)
        print(f"[RAW] medias (g): x={m[0]:+.2f}  y={m[1]:+.2f}  z={m[2]:+.2f}")
        if window is None:
            return {"ok": False, "is_fall": False, "proba": 0.0}

        window_norm = self.preprocess(window)
        proba = self.predict_proba(window_norm)
        is_fall = proba >= self.threshold

        return {"ok": True, "is_fall": is_fall, "proba": proba}