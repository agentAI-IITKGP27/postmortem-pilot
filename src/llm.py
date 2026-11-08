import os
import ssl
import time
import threading
from dotenv import load_dotenv
from openai import OpenAI, DefaultHttpxClient, APIStatusError, APIConnectionError
import instructor

load_dotenv()

_api_lock = threading.Lock()
RETRY_DELAYS = [2, 4, 8]

def get_llm_client():
    mode = os.getenv("ENV_MODE", "local").lower()

    if mode == "university":
        base_url = os.getenv("UNI_BASE_URL")
        api_key = os.getenv("UNI_API_KEY")
        model_name = os.getenv("UNI_MODEL_NAME", "Qwen/Qwen3-8B")
        ca_cert = os.getenv("UNI_CA_CERT_PATH")

        if not ca_cert or not os.path.exists(ca_cert):
            raise FileNotFoundError(f"CA certificate not found at: {ca_cert}")

        ssl_ctx = ssl.create_default_context(cafile=ca_cert)
        http_client = DefaultHttpxClient(verify=ssl_ctx, timeout=60.0)
        raw_client = OpenAI(base_url=base_url, api_key=api_key, http_client=http_client, max_retries=0)
    else:
        base_url = os.getenv("LOCAL_BASE_URL", "http://localhost:11434/v1")
        api_key = os.getenv("LOCAL_API_KEY", "ollama")
        model_name = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:7b-instruct")
        raw_client = OpenAI(base_url=base_url, api_key=api_key, max_retries=2)

    structured_client = instructor.from_openai(raw_client, mode=instructor.Mode.JSON)
    return raw_client, structured_client, model_name

def call_chat(messages, max_tokens=512, temperature=0.1, response_format=None, tools=None):
    raw_client, _, model_name = get_llm_client()
    safe_max_tokens = min(max(1, max_tokens), 1024)

    kwargs = {
        "model": model_name,
        "messages": messages,
        "max_tokens": safe_max_tokens,
        "temperature": temperature,
    }
    if response_format:
        kwargs["response_format"] = response_format
    if tools:
        kwargs["tools"] = tools

    with _api_lock:
        for attempt, delay in enumerate([0] + RETRY_DELAYS):
            if delay > 0:
                print(f"API busy (429/503). Waiting {delay}s before retry {attempt}/{len(RETRY_DELAYS)}...")
                time.sleep(delay)
            try:
                return raw_client.chat.completions.create(**kwargs)
            except APIStatusError as e:
                if e.status_code in (429, 503) and attempt < len(RETRY_DELAYS):
                    continue
                raise
            except APIConnectionError:
                if attempt < len(RETRY_DELAYS):
                    continue
                raise

if __name__ == "__main__":
    resp = call_chat(
        messages=[{"role": "user", "content": "Return a JSON object with keys 'project' set to 'PostmortemPilot' and 'status' set to 'ready'."}],
        max_tokens=100,
        response_format={"type": "json_object"}
    )
    print("Python Client Test Output:", resp.choices[0].message.content)