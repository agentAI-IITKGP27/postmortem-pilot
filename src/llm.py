import hashlib
import math
import os
import re
import time
import threading
from typing import List
import httpx
import instructor
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# Chat LLM Configuration (Port 1033)
API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("UNI_API_KEY", "")
BASE_URL = os.getenv("OPENAI_BASE_URL") or os.getenv("UNI_BASE_URL", "https://103.42.50.41:1033/v1")
MODEL_NAME = os.getenv("MODEL_NAME") or os.getenv("UNI_MODEL_NAME", "Qwen/Qwen3-8B")
CERT_PATH = os.getenv("SSL_CERT_FILE", "certs/vm05-ca.crt")

# Embedding API Configuration (Port 1035)
EMBED_API_KEY = os.getenv("EMBEDDING_API_KEY", API_KEY)
EMBED_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://103.42.50.41:1035/v1")
EMBED_MODEL = os.getenv("EMBEDDING_MODEL", "bge-small-en-v1.5")
EMBED_CERT_PATH = os.getenv("EMBEDDING_CERT_PATH", "certs/vm02-ca.crt")

_lock = threading.Lock()

_http_client = httpx.Client(verify=CERT_PATH if os.path.exists(CERT_PATH) else True, timeout=60.0)
raw_client = OpenAI(api_key=API_KEY, base_url=BASE_URL, http_client=_http_client)
structured_client = instructor.from_openai(raw_client, mode=instructor.Mode.JSON)

_embed_verify = EMBED_CERT_PATH if os.path.exists(EMBED_CERT_PATH) else True
_embed_http_client = httpx.Client(verify=_embed_verify, timeout=60.0)
embed_client = OpenAI(api_key=EMBED_API_KEY, base_url=EMBED_BASE_URL, http_client=_embed_http_client)

def call_chat(messages, max_tokens=512, temperature=0.1, response_format=None):
    max_tokens = min(max_tokens, 1024)
    for delay in [2, 4, 8, 0]:
        try:
            with _lock:
                kwargs = {
                    "model": MODEL_NAME,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
                if response_format:
                    kwargs["response_format"] = response_format
                return raw_client.chat.completions.create(**kwargs)
        except Exception as e:
            if delay == 0:
                raise e
            time.sleep(delay)

def call_structured(messages, response_model, max_tokens=512, temperature=0.1, max_retries=2):
    max_tokens = min(max_tokens, 1024)
    for delay in [2, 4, 8, 0]:
        try:
            with _lock:
                return structured_client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=messages,
                    response_model=response_model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    max_retries=max_retries,
                )
        except Exception as e:
            if delay == 0:
                raise e
            time.sleep(delay)

_EMBED_API_DISABLED = False
_EMBED_FALLBACK_ENABLED = os.getenv("EMBEDDINGS_LOCAL_FALLBACK", "1") != "0"
_EMBED_DIM = 384


def _hash_embed(text: str, dim: int = _EMBED_DIM) -> List[float]:
    """Deterministic offline embedding (hashed bag-of-words + prefixes).

    Used when the university embedding API is unreachable or rejects the key so
    that dense retrieval still has a stable vector space to work with.
    """
    vec = [0.0] * dim
    tokens = re.findall(r"[a-z0-9_:-]+", text.lower())
    for tok in tokens:
        for feat in (tok, tok[:4]):
            idx = int(hashlib.md5(feat.encode("utf-8")).hexdigest(), 16) % dim
            vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def call_embeddings(texts: List[str]) -> List[List[float]]:
    global _EMBED_API_DISABLED

    if _EMBED_API_DISABLED:
        return [_hash_embed(t) for t in texts]

    all_vectors: List[List[float]] = []
    batch_size = 16

    for start in range(0, len(texts), batch_size):
        batch = [t[:1800] for t in texts[start : start + batch_size]]
        for delay in [2, 4, 8, 0]:
            try:
                with _lock:
                    resp = embed_client.embeddings.create(
                        model=EMBED_MODEL,
                        input=batch,
                    )
                sorted_data = sorted(resp.data, key=lambda item: item.index)
                all_vectors.extend([item.embedding for item in sorted_data])
                break
            except Exception as e:
                if delay == 0:
                    if _EMBED_FALLBACK_ENABLED:
                        print(
                            "[llm] Embedding API unavailable "
                            f"({type(e).__name__}); switching to local hash embeddings."
                        )
                        _EMBED_API_DISABLED = True
                        return [_hash_embed(t) for t in texts]
                    raise e
                time.sleep(delay)

    return all_vectors

if __name__ == "__main__":
    vecs = call_embeddings(["Example document chunk", "Example search query"])
    print(f"Embedding API verified! Returned {len(vecs)} vectors of dimension {len(vecs[0])}.")
