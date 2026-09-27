"""openWakeWord's streaming speech features, without importing openWakeWord.

`import openwakeword` brings scipy, scikit-learn, requests and tqdm with it:
+80 MB of the app's ~400 MB, for code the running app never calls. The wake
word needs only AudioFeatures' streaming path - two small ONNX models (mel
spectrogram, Google's speech embedding) and some buffering - so this is that
path, reimplemented on the same model files and checked to give the same
numbers (tests/checks/wake_features.py). It also keeps the last 10 s of audio as a
numpy ring instead of a deque of Python ints copied into a list every 80 ms.

Training a wake word (rare, in the background) still uses openWakeWord itself.
"""

from __future__ import annotations

import importlib.util
import os

import numpy as np

FRAME = 1280                    # 80 ms at 16 kHz
_MEL_FRAMES, _MEL_BINS = 76, 32
_KEEP_AUDIO = 16000 * 10
_KEEP_MEL = 10 * 97
_KEEP_FEATURES = 120


def _models_dir() -> str:
    spec = importlib.util.find_spec("openwakeword")     # locates the package without running it
    return os.path.join(list(spec.submodule_search_locations)[0], "resources", "models")


class Features:
    """Drop-in for openwakeword.utils.AudioFeatures' streaming use:
    __call__(int16 samples), get_features(n), reset()."""

    def __init__(self) -> None:
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.enable_cpu_mem_arena = False
        folder = _models_dir()
        self._mel = ort.InferenceSession(os.path.join(folder, "melspectrogram.onnx"), options,
                                         providers=["CPUExecutionProvider"])
        self._embed = ort.InferenceSession(os.path.join(folder, "embedding_model.onnx"), options,
                                           providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self._audio = np.zeros(0, dtype=np.int16)
        self._pending = np.zeros(0, dtype=np.int16)
        self._accumulated = 0
        self.melspectrogram_buffer = np.ones((_MEL_FRAMES, _MEL_BINS))
        # As openWakeWord does: start from the features of random noise.
        self.feature_buffer = self._embeddings(np.random.randint(-1000, 1000, 16000 * 4).astype(np.int16))

    # --- the two models ------------------------------------------------------------

    def _melspectrogram(self, samples: np.ndarray) -> np.ndarray:
        out = self._mel.run(None, {"input": samples.astype(np.float32)[None, :]})[0]
        return np.squeeze(out) / 10 + 2

    def _embed_windows(self, windows: np.ndarray) -> np.ndarray:
        return self._embed.run(None, {"input_1": windows.astype(np.float32)})[0].squeeze()

    def _embeddings(self, samples: np.ndarray) -> np.ndarray:
        spec = self._melspectrogram(samples)
        windows = [spec[i:i + _MEL_FRAMES] for i in range(0, spec.shape[0], 8)
                   if spec[i:i + _MEL_FRAMES].shape[0] == _MEL_FRAMES]
        return self._embed_windows(np.array(windows)[..., None])

    # --- streaming -----------------------------------------------------------------

    def __call__(self, samples: np.ndarray) -> int:
        samples = np.asarray(samples, dtype=np.int16)
        if self._pending.size:
            samples = np.concatenate([self._pending, samples])
            self._pending = np.zeros(0, dtype=np.int16)
        total = self._accumulated + samples.size
        if total >= FRAME and total % FRAME:
            extra = total % FRAME
            samples, self._pending = samples[:-extra], samples[-extra:]
        self._audio = np.concatenate([self._audio, samples])[-_KEEP_AUDIO:]
        self._accumulated += samples.size

        processed = 0
        if self._accumulated >= FRAME and self._accumulated % FRAME == 0:
            spec = self._melspectrogram(self._audio[-self._accumulated - 160 * 3:])
            self.melspectrogram_buffer = np.vstack([self.melspectrogram_buffer, spec])[-_KEEP_MEL:]
            for i in range(self._accumulated // FRAME - 1, -1, -1):
                end = -8 * i or len(self.melspectrogram_buffer)
                window = self.melspectrogram_buffer[-_MEL_FRAMES + end:end].astype(np.float32)[None, :, :, None]
                if window.shape[1] == _MEL_FRAMES:
                    self.feature_buffer = np.vstack([self.feature_buffer, self._embed_windows(window)])
            processed, self._accumulated = self._accumulated, 0
        if self.feature_buffer.shape[0] > _KEEP_FEATURES:
            self.feature_buffer = self.feature_buffer[-_KEEP_FEATURES:]
        return processed or self._accumulated

    def get_features(self, n_feature_frames: int = 16) -> np.ndarray:
        return self.feature_buffer[-int(n_feature_frames):][None].astype(np.float32)
