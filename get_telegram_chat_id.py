import os
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    raise ValueError("Не заполнен TELEGRAM_BOT_TOKEN в .env")

url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"

response = requests.get(url, timeout=30)

print(response.status_code)
print(response.text)
