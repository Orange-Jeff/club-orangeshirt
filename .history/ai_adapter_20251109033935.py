"""
ai_adapter.py

Pluggable adapter for room text + image generation.
Supports:
 - Hugging Face Inference API (text + image)
 - Google Gemini / Vertex AI (text via API key placeholder)
 - OpenAI (optional)
 - Local (placeholder)

Environment variables:
 - PROVIDER_TEXT: huggingface | gemini | openai | local  (default: huggingface)
 - PROVIDER_IMAGE: huggingface | gemini | openai | local (default: huggingface)
 - IMAGE_SIZE: e.g. "512x512" (default: "512x512")
 - HUGGINGFACE_API_KEY
 - HF_MODEL_TEXT (e.g. 'google/flan-t5-large')
 - HF_MODEL_IMAGE (e.g. 'stabilityai/stable-diffusion-2')
 - GEMINI_API_KEY (optional for Gemini)
 - NO_IMAGES=1 to skip image generation
"""
from typing import Dict, Optional
import os
import json
import base64
import requests
import random
import logging

IMAGE_SIZE = os.environ.get("IMAGE_SIZE", "512x512")
NO_IMAGES = os.environ.get("NO_IMAGES", "") in ("1", "true", "True")

logger = logging.getLogger("ai_adapter")
logger.setLevel(logging.INFO)

HF_API = "https://api-inference.huggingface.co"
HF_TOKEN = os.environ.get("HUGGINGFACE_API_KEY")
HF_MODEL_TEXT = os.environ.get("HF_MODEL_TEXT", "google/flan-t5-large")
HF_MODEL_IMAGE = os.environ.get("HF_MODEL_IMAGE", "stabilityai/stable-diffusion-2")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL_TEXT = os.environ.get("GEMINI_MODEL_TEXT", "gemini-medium")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

def _hf_headers():
    if not HF_TOKEN:
        raise RuntimeError("HUGGINGFACE_API_KEY not set for Hugging Face provider.")
    return {"Authorization": f"Bearer {HF_TOKEN}"}

def hf_generate_text(prompt: str, max_tokens: int = 256, temperature: float = 0.8) -> Dict:
    model = os.environ.get("HF_MODEL_TEXT", HF_MODEL_TEXT)
    url = f"{HF_API}/models/{model}"
    payload = {"inputs": prompt, "parameters": {"max_new_tokens": max_tokens, "temperature": temperature}}
    resp = requests.post(url, headers=_hf_headers(), json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list) and data and isinstance(data[0], dict) and "generated_text" in data[0]:
        text = data[0]["generated_text"]
    elif isinstance(data, dict) and "generated_text" in data:
        text = data["generated_text"]
    elif isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"Hugging Face text generation error: {data['error']}")
    else:
        text = json.dumps(data)
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = {
            "title": f"Room {random.randint(1000,9999)}",
            "description": text,
            "image_prompt": text,
            "exit_labels": {"1": "Left", "2": "Right"}
        }
    return parsed

def hf_generate_image(prompt: str, size: str = IMAGE_SIZE) -> bytes:
    model = os.environ.get("HF_MODEL_IMAGE", HF_MODEL_IMAGE)
    url = f"{HF_API}/models/{model}"
    width, height = [int(x) for x in size.split("x")]
    payload = {"inputs": prompt, "parameters": {"width": width, "height": height}}
    resp = requests.post(url, headers=_hf_headers(), json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"Hugging Face image generation error: {data['error']}")
    if isinstance(data, list) and data and isinstance(data[0], dict) and "generated_image" in data[0]:
        return base64.b64decode(data[0]["generated_image"])
    if isinstance(data, dict) and "generated_image" in data:
        return base64.b64decode(data["generated_image"])
    if resp.headers.get("content-type", "").startswith("image/"):
        return resp.content
    text = json.dumps(data)
    import re
    m = re.search(r"([A-Za-z0-9+/=]{200,})", text)
    if m:
        return base64.b64decode(m.group(1))
    raise RuntimeError("Could not parse Hugging Face image response.")

def gemini_generate_text(prompt: str, max_tokens: int = 256) -> Dict:
    if GEMINI_API_KEY:
        url = f"https://generativelanguage.googleapis.com/v1beta2/models/{GEMINI_MODEL_TEXT}:generateText?key={GEMINI_API_KEY}"
        body = {"prompt": {"text": prompt}, "maxOutputTokens": max_tokens}
        resp = requests.post(url, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        text = ""
        if "candidates" in data and data["candidates"]:
            text = data["candidates"][0].get("content", "")
        elif "output" in data and isinstance(data["output"], list) and data["output"]:
            text = "".join(part.get("content", "") for part in data["output"])
        else:
            text = json.dumps(data)
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {
                "title": f"Room {random.randint(1000,9999)}",
                "description": text,
                "image_prompt": text,
                "exit_labels": {"1": "Left", "2": "Right"}
            }
        return parsed
    raise RuntimeError("GEMINI_API_KEY not configured for Gemini provider.")

def gemini_generate_image(prompt: str, size: str = IMAGE_SIZE) -> bytes:
    raise NotImplementedError("Gemini image generation not implemented in this adapter. See README for instructions.")

def openai_generate_text(prompt: str, max_tokens: int = 256) -> Dict:
    try:
        import openai
    except Exception as e:
        raise RuntimeError("openai package is not installed.") from e
    openai.api_key = OPENAI_API_KEY
    resp = openai.ChatCompletion.create(
        model=os.environ.get("OPENAI_TEXT_MODEL", "gpt-3.5-turbo"),
        messages=[{"role":"system","content":"You are a creative room generator. Return ONLY a single JSON object describing the room."},
                  {"role":"user","content":prompt}],
        max_tokens=max_tokens,
        temperature=0.9
    )
    text = resp.choices[0].message.content
    try:
        return json.loads(text)
    except Exception:
        return {"title":"Room","description":text,"image_prompt":text,"exit_labels":{"1":"Left","2":"Right"}}

def openai_generate_image(prompt: str, size: str = IMAGE_SIZE) -> bytes:
    try:
        import openai
    except Exception as e:
        raise RuntimeError("openai package is not installed.") from e
    openai.api_key = OPENAI_API_KEY
    result = openai.Image.create(prompt=prompt, size=size, response_format="b64_json")
    b64 = result.data[0].b64_json
    return base64.b64decode(b64)

def generate_room_text(seed: Optional[str] = None, prompt_override: Optional[str] = None) -> Dict:
    provider = os.environ.get("PROVIDER_TEXT", "huggingface")
    if prompt_override:
        user_prompt = prompt_override
    else:
        user_prompt = (
            "Produce a single JSON object and only JSON with keys: title (short), "
            "description (2-6 evocative sentences), image_prompt (short prompt for an illustration), "
            "exit_labels (object with keys '1' and '2' for short labels). Keep content SFW and imaginative."
        )
        if seed:
            user_prompt += f" Seed: {seed}"
    if provider == "huggingface":
        return hf_generate_text(user_prompt)
    if provider == "gemini":
        return gemini_generate_text(user_prompt)
    if provider == "openai":
        return openai_generate_text(user_prompt)
    if provider == "local":
        return {
            "title": f"Local Room {random.randint(1000,9999)}",
            "description": "A locally-generated placeholder room. No external API configured.",
            "image_prompt": "a simple abstract room",
            "exit_labels": {"1":"Left","2":"Right"}
        }
    raise RuntimeError(f"Unsupported text provider: {provider}")

def generate_room_image(image_prompt: str, size: Optional[str] = None) -> bytes:
    if NO_IMAGES:
        raise RuntimeError("Image generation disabled (NO_IMAGES set).")
    size = size or IMAGE_SIZE
    provider = os.environ.get("PROVIDER_IMAGE", os.environ.get("PROVIDER_TEXT", "huggingface"))
    if provider == "huggingface":
        return hf_generate_image(image_prompt, size=size)
    if provider == "gemini":
        return gemini_generate_image(image_prompt, size=size)
    if provider == "openai":
        return openai_generate_image(image_prompt, size=size)
    if provider == "local":
        raise NotImplementedError("Local image generation not implemented in adapter.")
    raise RuntimeError(f"Unsupported image provider: {provider}")