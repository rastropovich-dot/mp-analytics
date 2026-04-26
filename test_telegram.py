import os
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TOKEN:
    raise ValueError("Не заполнен TELEGRAM_BOT_TOKEN")

if not CHAT_ID:
    raise ValueError("Не заполнен TELEGRAM_CHAT_ID")

message = "✅ MP Analytics: Telegram-уведомления подключены."

url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

payload = {
    "chat_id": CHAT_ID,
    "text": message,
    "parse_mode": "HTML"
}

response = requests.post(url, json=payload, timeout=30)

print(response.status_code)
print(response.text)
