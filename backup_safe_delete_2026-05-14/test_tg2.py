import asyncio
from dotenv import load_dotenv
load_dotenv()
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from telegram import Bot
async def main():
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text='Test via Script no async with')
        print('Success')
    except Exception as e:
        print(f'Error: {e}')

asyncio.run(main())