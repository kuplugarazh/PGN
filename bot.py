import uuid
import asyncio
import html
import os
import time
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import pai_g as pai  # Импортируем твой pai_g.py

# Безопасно читаем токен из переменных окружения (больше не хардкодим в файле!)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Ошибка: не задана переменная окружения TELEGRAM_BOT_TOKEN или BOT_TOKEN!")

allowed_user_id_str = os.environ.get("TELEGRAM_ALLOWED_USER_ID") or os.environ.get("ALLOWED_USER_ID")
if not allowed_user_id_str:
    raise ValueError("Ошибка: не задана переменная окружения TELEGRAM_ALLOWED_USER_ID или ALLOWED_USER_ID!")
ALLOWED_USER_ID = int(allowed_user_id_str)

MAX_USER_TURNS = 3           # Сброс чата каждые 3 твоих запроса
IDLE_TIMEOUT_SEC = 600       # Выгружать llama-server из VRAM через 10 минут тишины
SERVER_STARTUP_TIMEOUT = 90  # Макс. время ожидания холодного старта модели (сек)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Состояние сессии
messages: list[dict] = [{"role": "system", "content": pai.SYSTEM_PROMPT}]
user_turns_count = 0
last_activity = time.time()
idle_task: asyncio.Task | None = None
agent_lock = asyncio.Lock()

# Мосты для синхронного потока pai_g.py -> asyncio Telegram
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

        deadline = time.monotonic() + SERVER_STARTUP_TIMEOUT
        loading_notified = False

        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            try:
                r = await http.get("http://127.0.0.1:8080/health", timeout=1.5)
                if r.status_code == 200:
                    return True
                if r.status_code == 503 and status_msg and not loading_notified:
                    await status_msg.edit_text("🧠 Сервер запущен, загружаю веса модели в VRAM...")
                    loading_notified = True
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
        if agent_lock.locked():
            continue
        if time.time() - last_activity > IDLE_TIMEOUT_SEC:
            await stop_llama_server()
            break


def request_soft_cancel() -> bool:
    if not agent_lock.locked():
        return False
    pai.CANCEL_EVENT.set()
    if pending_confirm_future and not pending_confirm_future.done():
        pending_confirm_future.set_result(False)
    return True


pending_confirm_futures: dict[str, asyncio.Future] = {}

def tg_confirm_bridge(tool_name: str, arguments: dict, level: pai.PermissionLevel) -> bool:
    global last_activity
    last_activity = time.time()
    action = html.escape(pai.confirmation_text(tool_name, arguments))
    req_id = uuid.uuid4().hex[:8]

    async def _ask_user() -> bool:
        fut = main_loop.create_future()
        pending_confirm_futures[req_id] = fut
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Разрешить", callback_data=f"conf_yes_{req_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"conf_no_{req_id}"),
        ]])
        await bot.send_message(
            ALLOWED_USER_ID,
            f"⚠️ <b>PAI запрашивает подтверждение [{level.value}]</b>:\n<code>{action}</code>",
            parse_mode="HTML",
            reply_markup=kb,
        )
        try:
            return await asyncio.wait_for(fut, timeout=120.0)
        except asyncio.TimeoutError:
            return False
        finally:
            pending_confirm_futures.pop(req_id, None)

    future = asyncio.run_coroutine_threadsafe(_ask_user(), main_loop)
    result = future.result()
    last_activity = time.time()
    return result

@dp.callback_query(F.from_user.id == ALLOWED_USER_ID, F.data.startswith(("conf_yes_", "conf_no_")))
async def on_confirm_callback(call: CallbackQuery) -> None:
    global last_activity
    last_activity = time.time()

    parts = call.data.split("_")
    if len(parts) < 3:
        await call.answer("Некорректные данные кнопки.", show_alert=True)
        return

    action_type, req_id = parts[1], parts[2]
    allowed = (action_type == "yes")

    fut = pending_confirm_futures.get(req_id)
    if fut and not fut.done():
        fut.set_result(allowed)
        status = "✅ Разрешено" if allowed else "❌ Отклонено"
        await call.message.edit_text(
            f"{call.message.html_text}\n\n<b>Итог:</b> {status}",
            parse_mode="HTML",
        )
    else:
        await call.message.edit_text(
            f"{call.message.html_text}\n\n<b>Итог:</b> Запрос устарел или уже обработан.",
            parse_mode="HTML",
        )
    await call.answer()

@dp.callback_query(F.from_user.id == ALLOWED_USER_ID, F.data == "cancel_task")
async def on_cancel_callback(call: CallbackQuery) -> None:
    if request_soft_cancel():
        await call.answer("Останавливаю...")
        await call.message.edit_text("🛑 Прерываю выполнение текущего шага...")
    else:
        await call.answer("Задача уже завершена.", show_alert=False)


# Добавлено: F.chat.type == "private" для всех обработчиков ниже
@dp.message(F.from_user.id == ALLOWED_USER_ID, F.chat.type == "private", Command("new"))
async def cmd_new(message: Message) -> None:
    reset_chat_context()
    await message.answer(f"🧹 Контекст PAI полностью очищен (0/{MAX_USER_TURNS}).")


@dp.message(F.from_user.id == ALLOWED_USER_ID, F.chat.type == "private", Command("stop"))
async def cmd_stop(message: Message) -> None:
    await stop_llama_server()
    await message.answer("🛑 llama-server остановлен, VRAM освобождена, контекст сброшен.")


@dp.message(F.from_user.id == ALLOWED_USER_ID, F.chat.type == "private", Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    if request_soft_cancel():
        await message.answer("🛑 Сигнал остановки отправлен. Завершаю текущий шаг...")
    else:
        await message.answer("Сейчас нет активной задачи для остановки.")


@dp.message(F.from_user.id == ALLOWED_USER_ID, F.chat.type == "private", F.text)
async def handle_user_message(message: Message) -> None:
    global user_turns_count, last_activity, idle_task

    if agent_lock.locked():
        await message.answer("⏳ Подожди, я ещё выполняю предыдущий запрос...")
        return

    async with agent_lock:
        pai.CANCEL_EVENT.clear()
        last_activity = time.time()

        if idle_task is None or idle_task.done():
            idle_task = asyncio.create_task(idle_watchdog())

        reset_notice = ""
        if user_turns_count >= MAX_USER_TURNS:
            reset_chat_context()
            reset_notice = f"🧹 <i>[Автосброс после {MAX_USER_TURNS} сообщений: начат новый диалог]</i>\n\n"

        status_msg = await message.answer("⏳ Думаю и выполняю инструменты...")

        if not await ensure_llama_server_running(status_msg):
            await status_msg.edit_text("❌ Не удалось запустить llama-server (превышен лимит 90 сек).")
            return

        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🛑 Прервать", callback_data="cancel_task")
        ]])
        await status_msg.edit_text("⚙️ Выполняю задачу...", reply_markup=cancel_kb)

        user_turns_count += 1
        messages.append({"role": "user", "content": message.text})
        task_state = pai.TaskState.for_goal(message.text) if pai.needs_workflow(message.text) else None

        try:
            await asyncio.to_thread(pai.run_agent_turn, messages, 0, task_state)
            raw_reply = pai.LAST_REPLY or "(пустой ответ)"
        except Exception as e:
            raw_reply = f"Ошибка выполнения PAI: {e}"
        finally:
            last_activity = time.time()

        header = f"{reset_notice}💬 <b>Запрос {user_turns_count}/{MAX_USER_TURNS}</b>\n\n"

        try:
            await status_msg.delete()
        except Exception:
            pass

        chunk_size = 3800
        chunks = [raw_reply[i:i + chunk_size] for i in range(0, len(raw_reply), chunk_size)] or ["(пустой ответ)"]

        for idx, chunk in enumerate(chunks):
            escaped_chunk = html.escape(chunk)
            text_to_send = (header + escaped_chunk) if idx == 0 else escaped_chunk
            await message.answer(text_to_send, parse_mode="HTML")


async def main() -> None:
    global main_loop
    main_loop = asyncio.get_running_loop()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
