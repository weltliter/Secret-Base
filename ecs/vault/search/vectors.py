"""Local Chinese embeddings and an in-memory index; no business vectors on disk."""
import hashlib
import json
import os
import threading
from pathlib import Path


MODEL_NAME = "BAAI/bge-small-zh-v1.5"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class VectorIndex:
    def __init__(self, model_dir=None, *, embedder=None):
        configured = model_dir or os.environ.get("VAULT_EMBEDDING_DIR")
        self.model_dir = Path(configured) if configured else None
        self.embedder = embedder
        self.session = None
        self.tokenizer = None
        self.cache = {}
        self.mutex = threading.RLock()

    @property
    def configured(self):
        return self.embedder is not None or self.model_dir is not None

    def clear(self):
        with self.mutex:
            self.cache.clear()

    def _load(self):
        if self.session is not None:
            return
        import onnxruntime as ort
        from tokenizers import Tokenizer
        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        session = ort.InferenceSession(str(self.model_dir / "model_quantized.onnx"), sess_options=options, providers=["CPUExecutionProvider"])
        tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=512)
        self.session, self.tokenizer = session, tokenizer

    def _encode(self, texts, *, query=False):
        import numpy as np
        if self.embedder is not None:
            vectors = np.asarray(self.embedder(texts, query=query), dtype=np.float32)
        else:
            self._load()
            vectors = []
            input_names = {value.name for value in self.session.get_inputs()}
            for text in texts:
                encoded = self.tokenizer.encode((QUERY_PREFIX if query else "") + text)
                inputs = {"input_ids": [encoded.ids], "attention_mask": [encoded.attention_mask], "token_type_ids": [encoded.type_ids]}
                inputs = {name: np.asarray(value, dtype=np.int64) for name, value in inputs.items() if name in input_names}
                # BGE uses CLS pooling, not the BERT pooler or token averaging.
                output = self.session.run(["last_hidden_state"], inputs)[0]
                vectors.append(output[0, 0])
            vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim != 2 or not np.isfinite(vectors).all():
            raise ValueError("Invalid embedding output")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if (norms <= 0).any():
            raise ValueError("Empty embedding")
        return vectors / norms

    @staticmethod
    def chunks(row):
        text = "\n".join((row["title"], row["service"], " ".join(row["tags"]), row["description"]))
        return [text[start:start + 360] for start in range(0, max(1, len(text)), 300)]

    def rank(self, query, descriptors, limit=12):
        if not descriptors:
            self.clear()
            return []
        if not self.configured:
            return []
        with self.mutex:
            live = {row["id"] for row in descriptors}
            self.cache = {key: value for key, value in self.cache.items() if key in live}
            for row in descriptors:
                fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).digest()
                cached = self.cache.get(row["id"])
                if cached is None or cached[0] != fingerprint:
                    self.cache[row["id"]] = (fingerprint, self._encode(self.chunks(row)))
            query_vector = self._encode([query], query=True)[0]
            ranked = [(row, float((self.cache[row["id"]][1] @ query_vector).max())) for row in descriptors]
            ranked.sort(key=lambda pair: -pair[1])
            return [{"id": row["id"], "score": round(score, 5)} for row, score in ranked[:limit]]
