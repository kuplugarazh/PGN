import html
import asyncio
import time
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command

import pai_g as pai  # Импортируем твой pai.py

BOT_TOKEN = "9195941411:AAFpzVyldTEMDoWlLZh6QRgqHY3wf-MAeZU"
ALLOWED_USER_ID = 1058252337  # Твой Telegram ID

MAX_USER_TURNS = 3           # Сброс чата каждые 3 твоих запроса
IDLE_TIMEOUT_SEC = 600       # Выгружать llama-server из VRAM через 10 минут тишины

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Состояние сессии
messages: list[dict] = [{"role": "system", "content": pai.SYSTEM_PROMPT}]
user_turns_count = 0
last_activity = time.time()
idle_task: asyncio.Task | None = None

# Очередь для подтверждений HIGH/MEDIUM действий через кнопки в Telegram
main_loop: asyncio.AbstractEventLoop | None = None
pending_confirm_future: asyncio.Future | None = None


def reset_chat_context() -> None:
    global messages, user_turns_count
    messages = [{"role": "system", "content": pai.SYSTEM_PROMPT}]
    user_turns_count = 0


async def ensure_llama_server_running(status_msg: Message | None = None) -> bool:
    """Проверяет, запущен ли llama-server, и поднимает его при необходимости."""
    async with httpx.AsyncClient() as http:
        try:
            r = await http.get("http://127.0.0.1:8080/health", timeout=1.5)
            if r.status_code == 200:
                return True
        except Exception:
            pass

    if status_msg:
        await status_msg.edit_text("⚡ Бужу llama-server (загрузка модели в VRAM)...")

    proc = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "start", "llama-server.service"
    )
    await proc.wait()

    # Ждём готовности сервера (до 30 секунд)
    async with httpx.AsyncClient() as http:
        for _ in range(60):
            await asyncio.sleep(0.5)
            try:
                r = await http.get("http://127.0.0.1:8080/health", timeout=1.0)
                if r.status_code == 200:
                    return True
            except Exception:
                continue
    return False


async def stop_llama_server() -> None:
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "stop", "llama-server.service"
    )
    await proc.wait()
    reset_chat_context()


async def idle_watchdog() -> None:
    """Гасит llama-server через 10 минут бездействия, освобождая RTX 4060."""
    while True:
        await asyncio.sleep(30)
        if time.time() - last_activity > IDLE_TIMEOUT_SEC:
            await stop_llama_server()
            break


def tg_confirm_bridge(tool_name: str, arguments: dict, level: pai.PermissionLevel) -> bool:
    """Мост из синхронного потока pai.py в асинхронные кнопки Telegram."""
    global pending_confirm_future
    action = html.escape(pai.confirmation_text(tool_name, arguments))

    async def _ask_user() -> bool:
        global pending_confirm_future
        pending_confirm_future = main_loop.create_future()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Разрешить", callback_data="confirm_yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="confirm_no"),
        ]])
        await bot.send_message(
            ALLOWED_USER_ID,
            f"⚠️ <b>PAI запрашивает подтверждение [{level.value}]</b>:\n<code>{action}</code>",
            parse_mode="HTML",
            reply_markup=kb,
        )
        try:
            return await asyncio.wait_for(pending_confirm_future, timeout=120.0)
        except asyncio.TimeoutError:
            return False

    future = asyncio.run_coroutine_threadsafe(_ask_user(), main_loop)
    return future.result()


pai.CONFIRM_CALLBACK = tg_confirm_bridge


@dp.callback_query(F.from_user.id == ALLOWED_USER_ID, F.data.in_({"confirm_yes", "confirm_no"}))
async def on_confirm_callback(call: CallbackQuery) -> None:
    global pending_confirm_future
    allowed = call.data == "confirm_yes"
    if pending_confirm_future and not pending_confirm_future.done():
        pending_confirm_future.set_result(allowed)
    status = "✅ Разрешено" if allowed else "❌ Отклонено"
    await call.message.edit_text(f"{call.message.text}\n\n<b>Итог:</b> {status}")
    await call.answer()


@dp.message(F.from_user.id == ALLOWED_USER_ID, Command("new"))
async def cmd_new(message: Message) -> None:
    reset_chat_context()
    await message.answer("🧹 Контекст PAI полностью очищен (0/3).")


@dp.message(F.from_user.id == ALLOWED_USER_ID, Command("stop"))
async def cmd_stop(message: Message) -> None:
    await stop_llama_server()
    await message.answer("🛑 llama-server остановлен, VRAM освобождена, контекст сброшен.")


@dp.message(F.from_user.id == ALLOWED_USER_ID, F.text)
async def handle_user_message(message: Message) -> None:
    global user_turns_count, last_activity, idle_task
    last_activity = time.time()

    if idle_task is None or idle_task.done():
        idle_task = asyncio.create_task(idle_watchdog())

    # Автосброс каждые 3 твоих сообщения
    reset_notice = ""
    if user_turns_count >= MAX_USER_TURNS:
        reset_chat_context()
        reset_notice = "🧹 <i>[Автосброс после 3 сообщений: начат новый диалог]</i>\n\n"

    status_msg = await message.answer("⏳ Думаю и выполняю инструменты...")

    if not await ensure_llama_server_running(status_msg):
        await status_msg.edit_text("❌ Не удалось запустить llama-server.")
        return

    user_turns_count += 1
    messages.append({"role": "user", "content": message.text})
    task_state = pai.TaskState.for_goal(message.text) if pai.needs_workflow(message.text) else None

    # Запускаем цикл агента в отдельном потоке, чтобы не блокировать кнопки ТГ
    try:
        await asyncio.to_thread(pai.run_agent_turn, messages, 0, task_state)
        reply_text = html.escape(pai.LAST_REPLY or "(пустой ответ)")
    except Exception as e:
        reply_text = html.escape(f"Ошибка выполнения PAI: {e}")

    header = f"{reset_notice}💬 <b>Запрос {user_turns_count}/{MAX_USER_TURNS}</b>\n\n"
    full_reply = header + (reply_text or "(пустой ответ)")

    # Разбиваем длинный ответ, если он больше лимита ТГ (4096 символов)
    await status_msg.delete()
    for i in range(0, len(full_reply), 4000):
        await message.answer(full_reply[i:i + 4000], parse_mode="HTML")


async def main() -> None:
    global main_loop
    main_loop = asyncio.get_running_loop()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
