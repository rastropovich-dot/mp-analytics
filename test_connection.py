import os
from dotenv import load_dotenv
from supabase import create_client
from openai import OpenAI

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not SUPABASE_URL or "сюда_вставьте" in SUPABASE_URL:
    raise ValueError("Не заполнен SUPABASE_URL в .env")

if not SUPABASE_SERVICE_KEY or "сюда_вставьте" in SUPABASE_SERVICE_KEY:
    raise ValueError("Не заполнен SUPABASE_SERVICE_KEY в .env")

if not OPENAI_API_KEY or "сюда_вставьте" in OPENAI_API_KEY:
    raise ValueError("Не заполнен OPENAI_API_KEY в .env")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

result = supabase.table("marketplaces").select("*").execute()

print("✅ Supabase подключен")
print("marketplaces:")
print(result.data)

client = OpenAI(api_key=OPENAI_API_KEY)

response = client.responses.create(
    model="gpt-5.5-mini",
    input="Ответь одним словом: работает?"
)

print("✅ OpenAI API подключен")
print(response.output_text)
