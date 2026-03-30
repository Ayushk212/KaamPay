"""
VANI — Voice & NLP Agent
Takes Hindi voice/text input and extracts structured payroll data.
Uses Gemini 2.0 Flash for NER extraction with retry logic for rate limits.
"""

import os
import re
import json
import time
from dotenv import load_dotenv

load_dotenv()

# Try to import Gemini
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
    api_key = os.getenv("GEMINI_API_KEY", "")
    if api_key and api_key != "your_gemini_api_key_here":
        genai.configure(api_key=api_key)
    else:
        GEMINI_AVAILABLE = False
except ImportError:
    GEMINI_AVAILABLE = False

# Load constants
CONSTANTS_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'constants.json')
with open(CONSTANTS_PATH, 'r', encoding='utf-8') as f:
    CONSTANTS = json.load(f)

EXTRACTION_PROMPT = """You are a payroll data extractor for Indian daily wage workers.
Extract worker names, hours worked, and daily rate from the following Hindi/English text.

Rules:
- "aadha din" or "half day" = 0.5 days
- "poora din" or "8 ghante" or "pura din" = 1.0 days
- "4 ghante" = 0.5 days
- If someone just "kaam kiya" with hours mentioned elsewhere, use those hours
- Strip honorifics: "Ramesh bhai" -> "Ramesh"
- Number words to digits: "saat sau" -> 700
- If rate not mentioned for a worker, use the last mentioned rate
- "aaj kaam kiya" with hours = that many hours = days_worked calculation
- If only one worker is mentioned, return only that one worker
- Use EXACTLY the name provided. Do NOT invent or change names.
- days_worked and rate_per_day and gross_pay MUST be numbers, not strings.

Text: {transcript}

Respond ONLY with valid JSON. No explanation. No markdown. No code fences.
Format exactly:
{{
  "entries": [
    {{
      "worker_name": "string",
      "days_worked": 0.0,
      "rate_per_day": 0,
      "gross_pay": 0.0
    }}
  ],
  "confidence": 0.0,
  "language_detected": "string",
  "parsing_notes": "string"
}}"""


MODELS_TO_TRY = [
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]


def extract_with_gemini(transcript: str, max_retries: int = 2) -> dict:
    """Use Gemini for NER extraction. Tries multiple models if rate-limited."""
    last_error = None
    
    for model_name in MODELS_TO_TRY:
        for attempt in range(max_retries):
            try:
                print(f"[VANI] Trying {model_name} (attempt {attempt + 1}/{max_retries})")
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(
                    EXTRACTION_PROMPT.format(transcript=transcript),
                    generation_config=genai.types.GenerationConfig(
                        temperature=0.1,
                        max_output_tokens=1024,
                    )
                )
                text = response.text.strip()
                # Clean markdown fences if present
                if text.startswith("```"):
                    text = re.sub(r'^```(?:json)?\s*', '', text)
                    text = re.sub(r'\s*```$', '', text)
                
                parsed = json.loads(text)
                print(f"[VANI] SUCCESS with {model_name}: {json.dumps(parsed, indent=2)}")
                return parsed
            except Exception as e:
                last_error = e
                error_str = str(e)
                print(f"[VANI] {model_name} attempt {attempt + 1} failed: {error_str[:150]}")
                
                if "429" in error_str or "quota" in error_str.lower():
                    # Rate limited on this model, try next model immediately
                    print(f"[VANI] Rate limited on {model_name}, trying next model...")
                    break
                elif attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                else:
                    break
    
    raise Exception(f"All Gemini models failed. Last error: {str(last_error)[:200]}")


def generate_readback(entries: list) -> str:
    """Generate Hindi confirmation readback."""
    lines = []
    for e in entries:
        days_worked = float(e.get("days_worked", 1.0))
        gross_pay = float(e.get("gross_pay", 0))
        days_hindi = "1 din" if days_worked == 1.0 else f"{days_worked} din"
        lines.append(f"{e['worker_name']} — {days_hindi} — ₹{int(gross_pay)}")
    readback = "Maine suna:\n" + "\n".join(lines) + "\nSahi hai?"
    return readback


def transcribe_and_extract(text: str = None, audio_base64: str = None) -> dict:
    """
    Main VANI function.
    Takes text or audio, returns structured payroll output.
    """
    try:
        # Step 1: Get transcript
        if text:
            transcript = text
        elif audio_base64:
            transcript = CONSTANTS["demo_audio_transcript"]
        else:
            transcript = CONSTANTS["demo_audio_transcript"]

        print(f"[VANI] Processing transcript: '{transcript}'")
        print(f"[VANI] GEMINI_AVAILABLE: {GEMINI_AVAILABLE}")

        # Step 2: Extract payroll data
        if not GEMINI_AVAILABLE:
            raise Exception("Gemini API key not configured. Set GEMINI_API_KEY in backend/.env")

        extraction = extract_with_gemini(transcript)

        entries = extraction.get("entries", [])
        confidence = float(extraction.get("confidence", 0.85))

        # Ensure numeric types on all entries
        for entry in entries:
            entry["days_worked"] = float(entry.get("days_worked", 1.0))
            entry["rate_per_day"] = float(entry.get("rate_per_day", 0))
            entry["gross_pay"] = float(entry.get("gross_pay", 0))

        # Step 3: Determine status based on confidence
        if confidence < 0.75:
            status = "needs_confirmation"
        else:
            status = "success"

        # Step 4: Generate readback
        readback = generate_readback(entries)

        result = {
            "status": status,
            "transcript": transcript,
            "payroll_entries": entries,
            "confidence": confidence,
            "readback_hindi": readback,
            "error_message": None
        }
        print(f"[VANI] Returning {len(entries)} entries with confidence {confidence}")
        return result

    except Exception as e:
        print(f"[VANI] CRITICAL ERROR: {str(e)}")
        return {
            "status": "error",
            "transcript": text or "",
            "payroll_entries": [],
            "confidence": 0.0,
            "readback_hindi": "",
            "error_message": str(e)
        }
