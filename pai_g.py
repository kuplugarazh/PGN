import configparser
import json
import os
import py_compile
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

from openai import OpenAI

# =========================
# Конфигурация ядра PGN
# =========================

MODEL = "Ornith-1.5-9B-Q4_K_M.gguf"

HOME_DIR = Path.home()
PAI_ROOT = HOME_DIR / "pai"
WORKSPACE = HOME_DIR / "PAIWorkspace"

PGN_STATE_DIR = HOME_DIR / ".local/share/pgn"
BACKUP_DIR = PGN_STATE_DIR / "backups"
TRASH_DIR = PGN_STATE_DIR / "trash"
SANDBOX_BIN_DIR = PGN_STATE_DIR / "bin"

MAX_CONTEXT_TOKENS = 53248
COMPACT_THRESHOLD_CHARS = 110_000
MAX_AGENT_STEPS = 30
RESTART_STATE_FILE = PAI_ROOT / ".pgn_restart_state.json"
MAX_CONSECUTIVE_SELF_RESTARTS = 3

# Хуки совместимости с Telegram-ботом (bot.py)
CONFIRM_CALLBACK: Callable[[str, dict, "PermissionLevel"], bool] | None = None
SEND_PHOTO_CALLBACK: Callable[[str, str], bool] | None = None
LAST_REPLY: str = ""
CANCEL_EVENT = threading.Event()
LAST_SCREENSHOT_PATH: str | None = None

APP_ALIASES: dict[str, list[str]] = {
    "телеграм": ["telegram", "org.telegram.desktop"],
    "тг": ["telegram", "org.telegram.desktop"],
    "дискорд": ["discord", "vesktop", "webcord"],
    "стим": ["steam"],
    "браузер": ["firefox", "chromium", "brave", "google-chrome", "zen-browser"],
    "фаерфокс": ["firefox"],
    "хром": ["chromium", "google-chrome"],
    "яндекс музыка": ["yandex-music", "yandexmusic"],
    "музыка": ["yandex-music", "yandexmusic", "spotify"],
    "спотифай": ["spotify"],
    "код": ["code", "codium", "cursor"],
    "вскод": ["code", "codium"],
    "обсидиан": ["obsidian"],
    "терминал": ["kitty", "alacritty", "wezterm", "foot"],
    "китти": ["kitty"],
    "файлы": ["thunar", "nautilus", "dolphin", "nemo", "pcmanfm"],
    "проводник": ["thunar", "nautilus", "dolphin", "nemo"],
    "обс": ["obs", "com.obsproject.studio"],
    "влс": ["vlc"],
    "блендер": ["blender"],
    "гимп": ["gimp"],
    "майнкрафт": ["prismlauncher", "minecraft", "modrinth"],
    "призма": ["prismlauncher"],
}

client = OpenAI(
    base_url="http://127.0.0.1:8080/v1",
    api_key="local",
)


class PermissionLevel(str, Enum):
    READ = "READ"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# =========================
# Подготовка среды, карантин rm и автобэкапы
# =========================

SAFE_RM_SCRIPT = """#!/usr/bin/env python3
import shutil
import sys
from datetime import datetime
from pathlib import Path

trash_base = Path.home() / ".local/share/pgn/trash"
args = [a for a in sys.argv[1:] if not a.startswith("-")]
force = any(a.startswith("-") and "f" in a for a in sys.argv[1:])

if not args:
    sys.exit(0)

batch_dir = trash_base / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
batch_dir.mkdir(parents=True, exist_ok=True)

exit_code = 0
for raw in args:
    p = Path(raw)
    if not p.exists() and not p.is_symlink():
        if not force:
            print(f"rm (pgn-quarantine): невозможно удалить '{raw}': Нет такого файла или каталога", file=sys.stderr)
            exit_code = 1
        continue
    dest = batch_dir / p.name
    counter = 1
    while dest.exists():
        dest = batch_dir / f"{p.name}.{counter}"
        counter += 1
    try:
        shutil.move(str(p), str(dest))
        print(f"[PGN Quarantine] '{raw}' перемещён в карантин: {dest}")
    except Exception as e:
        print(f"rm (pgn-quarantine): ошибка перемещения '{raw}': {e}", file=sys.stderr)
        exit_code = 1

sys.exit(exit_code)
"""


def ensure_pgn_environment() -> None:
    """Создаёт служебные директории PGN и скрипт-перехватчик rm для песочницы."""
    for d in (WORKSPACE, BACKUP_DIR, TRASH_DIR, SANDBOX_BIN_DIR):
        d.mkdir(parents=True, exist_ok=True)

    rm_wrapper = SANDBOX_BIN_DIR / "rm"
    if not rm_wrapper.exists() or rm_wrapper.read_text(encoding="utf-8") != SAFE_RM_SCRIPT:
        rm_wrapper.write_text(SAFE_RM_SCRIPT, encoding="utf-8")
        rm_wrapper.chmod(rm_wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def backup_file_before_modify(target: Path) -> Path | None:
    """Автоматически сохраняет резервную копию существующего файла перед изменением."""
    if not target.exists() or not target.is_file():
        return None
    ensure_pgn_environment()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_rel = str(target.resolve()).lstrip("/").replace("/", "__")
    backup_path = BACKUP_DIR / f"{timestamp}__{safe_rel}"
    shutil.copy2(target, backup_path)
    return backup_path


# Защищённые секреты пользователя, к которым агент не имеет доступа ни через bash, ни через read/write
PROTECTED_SECRET_PATHS = (
    HOME_DIR / ".ssh",
    HOME_DIR / ".gnupg",
    HOME_DIR / ".local/share/keyrings",
    HOME_DIR / ".aws",
    HOME_DIR / ".config/gh",
    PAI_ROOT / ".env",
)


def is_protected_secret(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    if resolved.name == ".env" or resolved.name.startswith(".env."):
        return True
    for secret in PROTECTED_SECRET_PATHS:
        sec_res = secret.resolve()
        if resolved == sec_res or sec_res in resolved.parents:
            return True
    return False


# =========================
# 1. Изолированный Execution Engine (bwrap + bash)
# =========================

def build_bwrap_command(command: str, cwd: Path) -> list[str]:
    """Собирает команду bwrap с ro-корнем, скрытием секретов и запретом повышения привилегий."""
    ensure_pgn_environment()

    bwrap_cmd: list[str] = [
        "bwrap",
        "--die-with-parent",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--bind", "/tmp", "/tmp",
    ]

    # Пробрасываем XDG_RUNTIME_DIR (Wayland, Hyprland IPC, PipeWire, D-Bus)
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime and Path(xdg_runtime).exists():
        bwrap_cmd.extend(["--bind", xdg_runtime, xdg_runtime])

    # Домашняя директория доступна на запись
    bwrap_cmd.extend(["--bind", str(HOME_DIR), str(HOME_DIR)])

    # Скрываем критические секреты пустыми tmpfs или /dev/null
    for secret_dir in (
        HOME_DIR / ".ssh",
        HOME_DIR / ".gnupg",
        HOME_DIR / ".local/share/keyrings",
        HOME_DIR / ".aws",
        HOME_DIR / ".config/gh",
    ):
        if secret_dir.exists() and secret_dir.is_dir():
            bwrap_cmd.extend(["--tmpfs", str(secret_dir)])

    for env_file in (PAI_ROOT / ".env", HOME_DIR / ".env"):
        if env_file.exists() and env_file.is_file():
            bwrap_cmd.extend(["--ro-bind", "/dev/null", str(env_file)])

    # Защищаем саму папку бэкапов и карантина от модификации напрямую из bash (только чтение, кроме добавления через враппер)
    # Ставим SANDBOX_BIN_DIR первым в PATH, чтобы rm перехватывался карантином
    current_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    sandbox_path = f"{SANDBOX_BIN_DIR}:{current_path}"

    bwrap_cmd.extend([
        "--setenv", "PATH", sandbox_path,
        "--chdir", str(cwd),
        "/bin/bash", "-c", command,
    ])
    return bwrap_cmd


def execute_bash(command: str, cwd: str | None = None, timeout: int = 30) -> str:
    """Выполняет bash-команду в песочнице Bubblewrap (или обычном bash, если bwrap недоступен)."""
    work_dir = Path(cwd).expanduser().resolve() if cwd else WORKSPACE.resolve()
    if not work_dir.exists() or not work_dir.is_dir():
        work_dir = HOME_DIR

    timeout = max(1, min(300, int(timeout)))

    use_bwrap = shutil.which("bwrap") is not None
    if use_bwrap:
        cmd_to_run = build_bwrap_command(command, work_dir)
    else:
        ensure_pgn_environment()
        cmd_to_run = ["/bin/bash", "-c", command]

    env = dict(os.environ)
    env["PATH"] = f"{SANDBOX_BIN_DIR}:{env.get('PATH', '/usr/bin:/bin')}"

    try:
        process = subprocess.run(
            cmd_to_run,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
        )

        output = [f"Код завершения: {process.returncode} (cwd: {work_dir})"]

        if process.stdout:
            stdout = process.stdout[:15_000]
            if len(process.stdout) > 15_000:
                stdout += "\n[STDOUT обрезан: превышен лимит 15000 символов]"
            output.append(f"STDOUT:\n{stdout}")

        if process.stderr:
            stderr = process.stderr[:15_000]
            if len(process.stderr) > 15_000:
                stderr += "\n[STDERR обрезан: превышен лимит 15000 символов]"
            output.append(f"STDERR:\n{stderr}")

        return "\n".join(output)

    except subprocess.TimeoutExpired:
        return f"Ошибка: команда превысила лимит времени ({timeout} сек). Возможно, процесс ожидал интерактивного ввода."
    except Exception as e:
        return f"Ошибка выполнения команды: {e}"


# Алиас обратной совместимости
def execute_shell(command: str) -> str:
    return execute_bash(command)


# =========================
# 2. Работа с файлами (с автобэкапом и проверкой синтаксиса)
# =========================

def resolve_user_path(raw_path: str) -> Path:
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = WORKSPACE / p
    return p.resolve()


def read_file(path: str, start_line: int = 1, end_line: int = 500) -> str:
    target = resolve_user_path(path)
    if is_protected_secret(target):
        return f"Ошибка безопасности: доступ к секретному файлу запрещён: {target}"

    if not target.exists():
        return f"Ошибка: файл не существует: {target}"
    if not target.is_file():
        return f"Ошибка: путь не является файлом: {target}"

    try:
        content = target.read_text(encoding="utf-8")
        lines = content.splitlines()
        total = len(lines)
        s = max(1, int(start_line))
        e = min(total, max(s, int(end_line)))
        selected = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(s, e + 1))
        if len(selected) > 25_000:
            selected = selected[:25_000] + "\n[Вывод обрезан до 25000 символов]"
        header = f"Файл: {target} (строки {s}..{e} из {total})\n"
        return header + selected
    except UnicodeDecodeError:
        return "Ошибка: файл не является текстовым UTF-8."
    except OSError as e:
        return f"Ошибка чтения файла: {e}"


def validate_python_file_if_needed(target: Path) -> str | None:
    if target.suffix != ".py":
        return None
    try:
        py_compile.compile(str(target), doraise=True)
        return "Синтаксис Python корректен."
    except py_compile.PyCompileError as e:
        return f"ВНИМАНИЕ — Ошибка синтаксиса Python: {e}"


def write_file(path: str, content: str) -> str:
    target = resolve_user_path(path)
    if is_protected_secret(target):
        return f"Ошибка безопасности: запрещено перезаписывать защищённый путь: {target}"

    if target.exists() and target.is_dir():
        return f"Ошибка: указанный путь является директорией: {target}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        backup_path = backup_file_before_modify(target)
        target.write_text(content, encoding="utf-8")
        msg = f"Файл записан: {target}"
        if backup_path:
            msg += f" (резервная копия: {backup_path.name})"
        syntax_note = validate_python_file_if_needed(target)
        if syntax_note:
            msg += f"\n{syntax_note}"
        return msg
    except OSError as e:
        return f"Ошибка записи файла: {e}"


def edit_file(path: str, old_text: str, new_text: str) -> str:
    target = resolve_user_path(path)
    if is_protected_secret(target):
        return f"Ошибка безопасности: запрещено изменять защищённый путь: {target}"

    if not target.exists() or not target.is_file():
        return f"Ошибка: файл не найден: {target}"

    try:
        content = target.read_text(encoding="utf-8")
    except Exception as e:
        return f"Ошибка чтения файла: {e}"

    count = content.count(old_text)
    if count == 0:
        return "Ошибка: исходный фрагмент old_text не найден в файле."
    if count > 1:
        return f"Ошибка: фрагмент old_text найден {count} раз. Укажи более точный контекст."

    try:
        backup_path = backup_file_before_modify(target)
        target.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        msg = f"Файл успешно изменён: {target}"
        if backup_path:
            msg += f" (бэкап: {backup_path.name})"
        syntax_note = validate_python_file_if_needed(target)
        if syntax_note:
            msg += f"\n{syntax_note}"
        return msg
    except OSError as e:
        return f"Ошибка сохранения изменений: {e}"


# =========================
# 3. Контролируемые привилегированные операции (ВНЕ песочницы)
# =========================

ALLOWED_SYSTEMCTL_ACTIONS = {"start", "stop", "restart", "reload", "enable", "disable", "status"}


def privileged_system(action: str, target: str) -> str:
    """
    Выполняет привилегированные операции (установка/удаление пакетов, управление сервисами)
    вне песочницы bwrap ТОЛЬКО после подтверждения пользователем через CONFIRM_CALLBACK.
    """
    clean_target = target.strip()
    if not clean_target or not re.fullmatch(r"[a-zA-Z0-9@._+-]+(\s+[a-zA-Z0-9@._+-]+)*", clean_target):
        return f"Ошибка: недопустимое имя пакета или сервиса: '{target}'"

    targets = clean_target.split()

    if action == "install_pkg":
        cmd = ["yay", "-S", "--noconfirm", "--needed", "--sudoflags=-n", *targets]
        timeout = 180
    elif action == "remove_pkg":
        cmd = ["yay", "-Rns", "--noconfirm", "--sudoflags=-n", *targets]
        timeout = 90
    elif action.startswith("systemctl_"):
        sub = action.replace("systemctl_", "")
        if sub not in ALLOWED_SYSTEMCTL_ACTIONS:
            return f"Ошибка: неподдерживаемое действие systemctl: {sub}"
        cmd = ["sudo", "-n", "/usr/bin/systemctl", sub, *targets]
        timeout = 30
    else:
        return f"Ошибка: неизвестное привилегированное действие: {action}"

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired:
        return f"Ошибка: операция {action} превысила таймаут {timeout} сек."
    except Exception as e:
        return f"Ошибка запуска привилегированной операции: {e}"

    combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if "password is required" in combined.lower():
        return (
            "Ошибка: системе требуется правило NOPASSWD в /etc/sudoers.d/ для неинтерактивного выполнения "
            "(пароли через агента не передаются по соображениям безопасности)."
        )
    if proc.returncode != 0:
        return f"Ошибка ({action}, код {proc.returncode}):\n{combined[:4000]}"
    return f"Успешно выполнено ({action} -> {clean_target}):\n{combined[-2500:]}"


# =========================
# 4. Hyprland & Desktop Control
# =========================

class HyprlandBackend:
    """Адаптер для управления Hyprland (JSON IPC и Lua-диспатч hyprctl)."""

    def _require_session(self) -> str | None:
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return "Ошибка: PGN не запущен внутри активной Hyprland-сессии."
        return None

    def _query(self, subject: str) -> tuple[object | None, str | None]:
        err = self._require_session()
        if err:
            return None, err
        try:
            proc = subprocess.run(
                ["hyprctl", "-j", subject],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception as e:
            return None, f"Ошибка запуска hyprctl: {e}"
        if proc.returncode != 0:
            return None, f"Ошибка hyprctl {subject}: {proc.stderr.strip() or proc.stdout.strip()}"
        try:
            return json.loads(proc.stdout), None
        except json.JSONDecodeError as e:
            return None, f"Некорректный JSON от hyprctl: {e}"

    def _dispatch(self, expr: str) -> tuple[bool, str]:
        err = self._require_session()
        if err:
            return False, err
        try:
            proc = subprocess.run(
                ["hyprctl", "dispatch", expr],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception as e:
            return False, f"Ошибка hyprctl dispatch: {e}"
        if proc.returncode != 0:
            return False, f"Ошибка Hyprland ({expr}): {proc.stderr.strip() or proc.stdout.strip()}"
        return True, proc.stdout.strip()

    def get_clients(self) -> str:
        payload, error = self._query("clients")
        if error:
            return error
        if not isinstance(payload, list):
            return "Ошибка: hyprctl clients вернул не список."
        simplified = [
            {
                "address": c.get("address"),
                "class": c.get("class"),
                "title": c.get("title"),
                "workspace": c.get("workspace"),
                "pid": c.get("pid"),
                "focused": c.get("focused"),
            }
            for c in payload
            if isinstance(c, dict)
        ]
        return json.dumps(simplified, ensure_ascii=False)

    def get_workspaces(self) -> str:
        payload, error = self._query("workspaces")
        if error:
            return error
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def validate_workspace(workspace: object) -> tuple[str | None, str | None]:
        if isinstance(workspace, int) and not isinstance(workspace, bool) and workspace > 0:
            return str(workspace), None
        if isinstance(workspace, str):
            val = workspace.strip()
            if val.isdecimal() and int(val) > 0:
                return val, None
            if re.fullmatch(r"[A-Za-z0-9_.:-]+", val):
                return val, None
        return None, "Ошибка: некорректный идентификатор рабочего стола."

    def switch_workspace(self, workspace: object) -> str:
        target, err = self.validate_workspace(workspace)
        if err:
            return err
        ok, detail = self._dispatch(f'hl.dsp.focus({{ workspace = "{target}" }})')
        return f"Переключено на рабочий стол {target}." if ok else detail

    def exec_command(self, command: str, workspace: int | str | None = None) -> str:
        err = self._require_session()
        if err:
            return err
        if workspace is not None:
            ws_target, ws_err = self.validate_workspace(workspace)
            if not ws_err:
                self._dispatch(f'hl.dsp.focus({{ workspace = "{ws_target}" }})')
        lua_cmd = json.dumps(command, ensure_ascii=False)
        ok, detail = self._dispatch(f"hl.dsp.exec_cmd({lua_cmd})")
        if not ok:
            return f"Ошибка запуска в Hyprland: {detail}"
        return f"Запущено в Hyprland (workspace={workspace}): {command}"

    def find_client(self, target: str | None) -> tuple[dict | None, str | None]:
        payload, error = self._query("clients")
        if error:
            return None, error
        if not isinstance(payload, list) or not payload:
            return None, "Ошибка: нет открытых окон в Hyprland."
        if not target:
            for c in payload:
                if isinstance(c, dict) and c.get("focused"):
                    return c, None
            return payload[0], None

        q = target.strip().lower()
        search_terms = [q] + APP_ALIASES.get(q, [])
        for term in search_terms:
            for c in payload:
                if not isinstance(c, dict):
                    continue
                cls = str(c.get("class") or "").lower()
                title = str(c.get("title") or "").lower()
                if term == cls or term in cls or term in title:
                    return c, None
        return None, f"Ошибка: окно '{target}' не найдено."

    def control_window(
        self,
        action: str,
        target: str | None = None,
        workspace: int | str | None = None,
    ) -> str:
        client_obj, error = self.find_client(target)
        if error:
            return error

        cls_name = client_obj.get("class") or "unknown"
        title_name = client_obj.get("title") or ""
        addr = client_obj.get("address")
        ws_info = client_obj.get("workspace")
        current_ws = ws_info.get("name") or ws_info.get("id") if isinstance(ws_info, dict) else ws_info

        if target and not client_obj.get("focused"):
            if addr:
                self._dispatch(f"focuswindow address:{addr}")
                time.sleep(0.05)
            elif current_ws:
                self._dispatch(f'hl.dsp.focus({{ workspace = "{current_ws}" }})')
                time.sleep(0.1)

        if action == "focus":
            return f"Фокус переключён на окно {cls_name} ('{title_name}', стол {current_ws})."
        if action == "close":
            ok, detail = self._dispatch("hl.dsp.window.close()")
            return f"Окно {cls_name} ('{title_name}') закрыто." if ok else detail
        if action == "fullscreen":
            ok, detail = self._dispatch("hl.dsp.window.fullscreen()")
            return f"Переключён полноэкранный режим для {cls_name}." if ok else detail
        if action == "minimize":
            ok, detail = self._dispatch('hl.dsp.window.move({ workspace = "special:minimized" })')
            return f"Окно {cls_name} свёрнуто в special:minimized." if ok else detail
        if action == "move":
            if workspace is None:
                return "Ошибка: для перемещения окна укажи параметр workspace."
            valid_ws, ws_err = self.validate_workspace(workspace)
            if ws_err:
                return ws_err
            ws_arg = valid_ws if valid_ws.isdecimal() else f'"{valid_ws}"'
            ok, detail = self._dispatch(f"hl.dsp.window.move({{ workspace = {ws_arg} }})")
            return f"Окно {cls_name} перемещено на рабочий стол {valid_ws}." if ok else detail

        return f"Ошибка: неизвестное действие окна: {action}"


hyprland_backend = HyprlandBackend()


def app_launcher(app_name: str, workspace: int | str | None = None) -> str:
    """Умный запуск приложения по .desktop файлам и русским алиасам."""
    query = app_name.strip().lower()
    if not query:
        return "Ошибка: название приложения не может быть пустым."

    if query in ("яндекс музыка", "yandex music", "yandex-music", "yandexmusic"):
        ws_int = int(workspace) if isinstance(workspace, str) and workspace.isdecimal() else workspace
        return yandex_music_control(action="open_only", workspace=ws_int if isinstance(ws_int, int) else None)

    search_terms = [query] + APP_ALIASES.get(query, [])
    desktop_dirs = [
        HOME_DIR / ".local/share/applications",
        Path("/usr/share/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
        HOME_DIR / ".local/share/flatpak/exports/share/applications",
    ]

    candidates: list[tuple[int, str, str]] = []
    for d_dir in desktop_dirs:
        if not d_dir.exists():
            continue
        for desktop_file in d_dir.glob("*.desktop"):
            try:
                cp = configparser.ConfigParser(interpolation=None, strict=False)
                cp.read(desktop_file, encoding="utf-8")
                if "Desktop Entry" not in cp:
                    continue
                entry = cp["Desktop Entry"]
                if entry.get("NoDisplay", "").lower() == "true":
                    continue
                raw_exec = entry.get("Exec", "").strip()
                if not raw_exec:
                    continue

                name_en = entry.get("Name", "").strip()
                name_ru = entry.get("Name[ru]", "").strip()
                generic = entry.get("GenericName", "").strip()
                generic_ru = entry.get("GenericName[ru]", "").strip()
                keywords = entry.get("Keywords", "").strip()
                stem = desktop_file.stem.lower()

                primary = [name_en.lower(), name_ru.lower(), stem]
                secondary = [generic.lower(), generic_ru.lower(), keywords.lower(), raw_exec.lower()]

                best_score = 0
                for term in search_terms:
                    for val in primary:
                        if not val:
                            continue
                        if val == term:
                            best_score = max(best_score, 100)
                        elif val.startswith(term):
                            best_score = max(best_score, 80)
                        elif term in val:
                            best_score = max(best_score, 60)
                    for val in secondary:
                        if val and term in val:
                            best_score = max(best_score, 40)

                if best_score > 0:
                    clean_exec = re.sub(r"\s+%[fFuUdDnNickvm]", "", raw_exec).strip()
                    display = name_ru or name_en or desktop_file.stem
                    candidates.append((best_score, display, clean_exec))
            except Exception:
                continue

    if not candidates:
        return f"Ошибка: приложение '{app_name}' не найдено в .desktop файлах."

    candidates.sort(key=lambda item: (-item[0], len(item[1])))
    _, best_name, best_exec = candidates[0]
    res = hyprland_backend.exec_command(best_exec, workspace)
    return f"Найдено приложение '{best_name}' (команда: {best_exec}). {res}"


def desktop_control(
    action: str,
    target: str | None = None,
    workspace: int | str | None = None,
) -> str:
    """Единая точка управления графической средой Hyprland и запуском приложений."""
    if action == "launch_app":
        if not target:
            return "Ошибка: для launch_app укажи название приложения в target."
        return app_launcher(target, workspace)
    if action == "exec_gui":
        if not target:
            return "Ошибка: для exec_gui укажи команду в target."
        return hyprland_backend.exec_command(target, workspace)
    if action == "list_windows":
        return hyprland_backend.get_clients()
    if action == "list_workspaces":
        return hyprland_backend.get_workspaces()
    if action == "switch_workspace":
        if workspace is None and target is not None:
            workspace = target
        return hyprland_backend.switch_workspace(workspace)
    if action in ("focus", "close", "move", "fullscreen", "minimize"):
        return hyprland_backend.control_window(action, target, workspace)
    return f"Ошибка: неизвестное действие desktop_control: {action}"


# =========================
# 5. Специализированные интеграции (Screenshot & Yandex Music)
# =========================

def take_screenshot(filename: str = "pgn_screenshot.png") -> str:
    """Делает скриншот экрана через grim и отправляет его в Telegram."""
    global LAST_SCREENSHOT_PATH

    safe_name = Path(filename).name
    if not safe_name.endswith(".png"):
        safe_name += ".png"
    target_path = Path("/tmp") / safe_name

    try:
        proc = subprocess.run(
            ["grim", str(target_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return "Ошибка: утилита 'grim' не установлена."
    except Exception as e:
        return f"Ошибка создания скриншота: {e}"

    if proc.returncode != 0 or not target_path.exists():
        return f"Ошибка grim: {proc.stderr.strip() or proc.stdout.strip()}"

    LAST_SCREENSHOT_PATH = str(target_path)

    if SEND_PHOTO_CALLBACK is not None:
        try:
            if SEND_PHOTO_CALLBACK(str(target_path), f"📸 Скриншот ({safe_name})"):
                return f"Скриншот сохранён в {target_path} и отправлен в Telegram."
        except Exception as e:
            return f"Скриншот сохранён в {target_path}, но отправка не удалась: {e}"

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID")
    if bot_token and chat_id:
        try:
            curl_proc = subprocess.run(
                [
                    "curl", "-sS", "-X", "POST",
                    f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                    "-F", f"chat_id={chat_id}",
                    "-F", f"photo=@{target_path}",
                    "-F", "caption=📸 Скриншот рабочего стола",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if curl_proc.returncode == 0 and '"ok":true' in curl_proc.stdout:
                return f"Скриншот сохранён ({target_path}) и отправлен в Telegram."
        except Exception:
            pass

    return f"Скриншот сохранён: {target_path}"


def yandex_music_control(action: str = "play_pause", workspace: int | None = None) -> str:
    """Управление Яндекс Музыкой через CDP WebSocket на порту 9222 в фоновом режиме."""
    import websocket

    def get_ym_ws_url() -> str | None:
        try:
            data = urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=1).read()
            for t in json.loads(data):
                if t.get("type") == "page" and "music-application" in t.get("url", ""):
                    return t.get("webSocketDebuggerUrl")
        except Exception:
            return None
        return None

    ws_url = get_ym_ws_url()
    launched_now = False

    if not ws_url:
        cmd = "/opt/yandex-music/yandexmusic --gtk-version=3 --remote-debugging-port=9222"
        hyprland_backend.exec_command(cmd, workspace)
        launched_now = True
        for _ in range(20):
            time.sleep(0.5)
            ws_url = get_ym_ws_url()
            if ws_url:
                break
        if not ws_url:
            return "Яндекс Музыка запущена, но порт 9222 не ответил."
        time.sleep(3.0)

    if action == "open_only":
        return "Яндекс Музыка открыта."

    key_map = {
        "play": ("k", "KeyK", 75),
        "pause": ("k", "KeyK", 75),
        "play_pause": ("k", "KeyK", 75),
        "next": ("n", "KeyN", 78),
        "prev": ("p", "KeyP", 80),
        "like": ("l", "KeyL", 76),
    }
    key, code_str, vk = key_map.get(action, ("k", "KeyK", 75))

    try:
        ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=3)
        for ev_type in ("keyDown", "keyUp"):
            ws.send(json.dumps({
                "id": 1,
                "method": "Input.dispatchKeyEvent",
                "params": {
                    "type": ev_type,
                    "key": key,
                    "code": code_str,
                    "windowsVirtualKeyCode": vk,
                    "nativeVirtualKeyCode": vk,
                },
            }))
            ws.recv()
        ws.close()
        status = "запущена и " if launched_now else ""
        return f"Яндекс Музыка {status}выполнила действие: {action}."
    except Exception as e:
        return f"Ошибка WebSocket Яндекс Музыки: {e}"


# =========================
# Реестр инструментов PGN (Tool Registry — 8 ядерных инструментов)
# =========================

tools = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Универсальный неинтерактивный терминал Linux (в изолированной среде bwrap без root-доступа). "
                "Используй его для всех базовых операций ОС: навигации и поиска файлов (ls, find, rg), "
                "работы с git, запуска тестов (pytest) и скриптов, проверки и завершения процессов (ps, pgrep, kill), "
                "управления звуком PipeWire (wpctl), буфером обмена (wl-copy, wl-paste), поиска пакетов (yay -Ss), "
                "системной информации. Удаление через rm внутри терминала безопасно перемещает файлы в карантин."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Bash-команда для выполнения.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Рабочая директория (по умолчанию ~/PAIWorkspace).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Лимит времени в секундах (по умолчанию 30, максимум 300).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать содержимое текстового файла с нумерацией строк.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу (абсолютный, с ~ или относительно ~/PAIWorkspace)."},
                    "start_line": {"type": "integer", "description": "Начальная строка (по умолчанию 1)."},
                    "end_line": {"type": "integer", "description": "Конечная строка (по умолчанию 500)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Создать новый файл или полностью перезаписать существующий. "
                "Если файл уже существует, автоматически создаётся его резервная копия в ~/.local/share/pgn/backups/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу (по умолчанию относительно ~/PAIWorkspace)."},
                    "content": {"type": "string", "description": "Полное текстовое содержимое файла."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Точечно изменить существующий файл, заменив ровно одно вхождение old_text на new_text. "
                "Автоматически сохраняет резервную копию перед записью и проверяет синтаксис Python-файлов."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу."},
                    "old_text": {"type": "string", "description": "Точный исходный фрагмент текста для замены."},
                    "new_text": {"type": "string", "description": "Новый фрагмент текста."},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_control",
            "description": (
                "Управление графической средой Hyprland и приложениями: "
                "launch_app (умный запуск по названию на русском/английском через .desktop, например 'телеграм', 'браузер', 'стим'), "
                "exec_gui (запуск кастомной GUI-команды или терминала, например 'kitty --hold fish -c fastfetch'), "
                "list_windows, list_workspaces, switch_workspace, "
                "focus, close, move, fullscreen, minimize (управление открытыми окнами)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "launch_app", "exec_gui", "list_windows", "list_workspaces",
                            "switch_workspace", "focus", "close", "move", "fullscreen", "minimize",
                        ],
                    },
                    "target": {
                        "type": "string",
                        "description": "Имя приложения/окна или команда для exec_gui.",
                    },
                    "workspace": {
                        "anyOf": [{"type": "integer", "minimum": 1}, {"type": "string", "minLength": 1}],
                        "description": "Номер или имя рабочего стола Hyprland (опционально).",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Сделать скриншот экрана (через grim) и отправить его в Telegram.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Имя файла (по умолчанию pgn_screenshot.png)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "yandex_music_control",
            "description": (
                "Специализированный инструмент управления Яндекс Музыкой через WebSocket (порт 9222) в фоне: "
                "play_pause, play, pause, next, prev, like, open_only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play_pause", "play", "pause", "next", "prev", "like", "open_only"],
                    },
                    "workspace": {"type": "integer", "description": "Рабочий стол при первом открытии."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "privileged_system",
            "description": (
                "Выполнение привилегированных системных операций ВНЕ песочницы (установка и удаление пакетов Arch/AUR, "
                "управление системными службами systemd). Всегда запрашивает подтверждение у пользователя."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "install_pkg", "remove_pkg",
                            "systemctl_start", "systemctl_stop", "systemctl_restart",
                            "systemctl_enable", "systemctl_disable", "systemctl_status",
                        ],
                    },
                    "target": {
                        "type": "string",
                        "description": "Имя пакета (или нескольких через пробел) либо имя systemd-сервиса.",
                    },
                },
                "required": ["action", "target"],
            },
        },
    },
]


# =========================
# Политика разрешений (Security & Permissions)
# =========================

CRITICAL_CONFIG_FILES = (
    HOME_DIR / ".config/hypr/hyprland.lua",
    PAI_ROOT / "pai_g.py",
    PAI_ROOT / "pgn_core.py",
    PAI_ROOT / "bot.py",
)


def is_critical_config_or_core(raw_path: str) -> bool:
    target = resolve_user_path(raw_path)
    if target in CRITICAL_CONFIG_FILES:
        return True
    if PAI_ROOT.resolve() in target.parents:
        return True
    return False


def permission_for_tool(tool_name: str, arguments: dict) -> PermissionLevel:
    """
    Определяет уровень риска по типу операции и целевым объектам,
    не занимаясь ненадёжным парсингом bash-строк.
    """
    if tool_name in ("read_file", "take_screenshot"):
        return PermissionLevel.READ

    if tool_name in ("bash", "yandex_music_control", "desktop_control"):
        # bash изолирован через bwrap (ro-корень, скрытые секреты, rm-карантин, NoNewPrivileges),
        # поэтому выполняется автоматически без назойливых подтверждений каждого шага.
        return PermissionLevel.LOW

    if tool_name in ("write_file", "edit_file"):
        path_str = str(arguments.get("path", ""))
        # Обычные файлы меняются автоматически (с автобэкапом),
        # но изменение исходного кода самого агента или главного конфига Hyprland требует подтверждения
        if is_critical_config_or_core(path_str):
            return PermissionLevel.HIGH
        return PermissionLevel.LOW

    if tool_name == "privileged_system":
        if arguments.get("action") == "systemctl_status":
            return PermissionLevel.READ
        return PermissionLevel.HIGH

    return PermissionLevel.MEDIUM


def confirmation_text(tool_name: str, arguments: dict) -> str:
    if tool_name in ("write_file", "edit_file"):
        return f"изменить защищённый системный файл/конфиг: {arguments.get('path', '')}"
    if tool_name == "privileged_system":
        return f"выполнить привилегированную операцию {arguments.get('action')} -> {arguments.get('target')}"
    return f"выполнить {tool_name} ({arguments})"


def confirm_action(
    tool_name: str, arguments: dict, level: PermissionLevel = PermissionLevel.HIGH
) -> bool:
    if CONFIRM_CALLBACK is not None:
        return CONFIRM_CALLBACK(tool_name, arguments, level)

    action = confirmation_text(tool_name, arguments)
    answer = input(
        f"\nPGN запрашивает подтверждение [{level.value}]: {action}\n"
        "Разрешить? [y/N] "
    )
    return answer.strip().lower() in ("y", "yes", "д", "да")


def authorize_tool(tool_name: str, arguments: dict) -> tuple[bool, PermissionLevel, str | None]:
    level = permission_for_tool(tool_name, arguments)
    if level in (PermissionLevel.READ, PermissionLevel.LOW):
        return True, level, None

    if confirm_action(tool_name, arguments, level):
        return True, level, None

    return False, level, "Операция отменена пользователем."


TOOL_DISPATCH: dict[str, Callable[[dict], str]] = {
    "bash": lambda a: execute_bash(a["command"], a.get("cwd"), a.get("timeout", 30)),
    "read_file": lambda a: read_file(a["path"], a.get("start_line", 1), a.get("end_line", 500)),
    "write_file": lambda a: write_file(a["path"], a["content"]),
    "edit_file": lambda a: edit_file(a["path"], a["old_text"], a["new_text"]),
    "desktop_control": lambda a: desktop_control(a["action"], a.get("target"), a.get("workspace")),
    "take_screenshot": lambda a: take_screenshot(a.get("filename", "pgn_screenshot.png")),
    "yandex_music_control": lambda a: yandex_music_control(a.get("action", "play_pause"), a.get("workspace")),
    "privileged_system": lambda a: privileged_system(a["action"], a["target"]),
}


def call_tool(tool_name: str, arguments: dict) -> str:
    handler = TOOL_DISPATCH.get(tool_name)
    if handler is None:
        return f"Ошибка: неизвестный инструмент: {tool_name}"
    return handler(arguments)


# =========================
# Системный промпт PGN
# =========================

SYSTEM_PROMPT = f"""Ты — PGN (Personal AI Environment), автономный компьютерный ИИ-агент и ассистент пользователя на Arch Linux + Hyprland.

ТВОЯ ФИЛОСОФИЯ:
Максимальная самостоятельность при технически контролируемых последствиях.
Не переспрашивай пользователя перед каждым шагом: самостоятельно анализируй задачу, выстраивай план, выполняй команды, проверяй результат и исправляй ошибки, если что-то пошло не так.

СРЕДА И ПУТИ:
- Домашняя директория: {HOME_DIR} (~)
- Рабочая директория по умолчанию для проектов и скриптов: {WORKSPACE}
- Исходный код PGN: {PAI_ROOT}
- Конфигурация Hyprland управляется ИСКЛЮЧИТЕЛЬНО через ~/.config/hypr/hyprland.lua (никогда не предлагай редактировать hyprland.conf).

ПРАВИЛА РАБОТЫ С ИНСТРУМЕНТАМИ:
1. УНИВЕРСАЛЬНЫЙ ТЕРМИНАЛ (`bash`):
   - Твой главный инструмент для работы с ОС: навигация, поиск, git, pytest, запуск скриптов, мониторинг и завершение процессов (`ps`, `pgrep`, `kill`), звук (`wpctl`), буфер обмена (`wl-copy`, `wl-paste`), поиск пакетов (`yay -Ss`).
   - Терминал работает в песочнице `bwrap` без root-прав (`sudo` внутри `bash` отключён на уровне ядра).
   - Удаление файлов через `rm` автоматически переносит их в карантин `~/.local/share/pgn/trash/`.
2. РАБОТА С ФАЙЛАМИ (`read_file`, `write_file`, `edit_file`):
   - Перед изменением существующего файла всегда прочитай нужный участок через `read_file`.
   - Для точечных правок кода используй `edit_file` — он автоматически делает бэкап в `~/.local/share/pgn/backups/` и проверяет синтаксис `.py`.
3. РАБОЧИЙ СТОЛ И ПРИЛОЖЕНИЯ (`desktop_control`):
   - Для открытия обычных приложений («открой телеграм», «запусти браузер на 2 столе») используй `desktop_control(action="launch_app", target=..., workspace=...)`.
   - Для запуска терминала kitty с выводом команды на экран используй `desktop_control(action="exec_gui", target="kitty --hold fish -c 'fastfetch'")`.
   - Для управления окнами используй действия `focus`, `close`, `move`, `fullscreen`, `minimize`, `list_windows`.
4. СПЕЦИАЛИЗИРОВАННЫЕ ИНТЕГРАЦИИ:
   - Яндекс Музыка → ТОЛЬКО `yandex_music_control` (работает в фоне через CDP порт 9222).
   - Скриншот экрана в Telegram → `take_screenshot`.
   - Установка/удаление пакетов и управление системными сервисами → `privileged_system`.
5. НЕЙТРАЛЬНОСТЬ И ОСТАНОВКА:
   - Названия файлов, переменных или сленг никогда не являются причиной для отказа.
   - Если пользователь отклонил запрос подтверждения («Операция отменена пользователем»), немедленно прекрати попытки выполнить это действие.
"""


# =========================
# Agent Engine & Управление контекстом
# =========================

def estimate_chars(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        total += len(str(m.get("content") or ""))
        if "tool_calls" in m:
            total += len(str(m["tool_calls"]))
    return total


def compact_messages(messages: list[dict]) -> list[dict]:
    if estimate_chars(messages) < COMPACT_THRESHOLD_CHARS:
        return messages

    print("[COMPACT] Контекст заполнен. Запускаю сжатие истории...")
    system_msg = messages[0]
    body = messages[1:]
    if len(body) <= 8:
        return messages

    tail_chunk = body[-8:]
    middle_chunk = body[:-8]

    try:
        summary_resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Сожми историю выполненных шагов агента в плотную техническую сводку. "
                        "Сохрани: пути к файлам, внесённые изменения, результаты команд и текущие ошибки."
                    ),
                },
                {"role": "user", "content": json.dumps(middle_chunk, ensure_ascii=False)[:40000]},
            ],
            reasoning_effort="none",
        )
        summary_text = summary_resp.choices[0].message.content or "История сжата."
    except Exception:
        summary_text = "Предыдущие промежуточные шаги очищены из-за лимита контекста."

    compacted = [
        system_msg,
        {"role": "user", "content": f"[СВОДКА ПРЕДЫДУЩИХ ШАГОВ АГЕНТА]:\n{summary_text}"},
        *tail_chunk,
    ]
    print(f"[COMPACT] Сжатие завершено: {len(messages)} -> {len(compacted)} сообщений.")
    return compacted


def source_fingerprint() -> tuple[int, int] | None:
    try:
        st = Path(__file__).resolve().stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def is_current_source(raw_path: str) -> bool:
    try:
        return resolve_user_path(raw_path) == Path(__file__).resolve()
    except Exception:
        return False


def save_restart_state(messages: list[dict], restart_count: int) -> None:
    payload = {
        "version": 3,
        "restart_count": restart_count,
        "messages": messages,
    }
    fd, temp_name = tempfile.mkstemp(
        prefix=f"{RESTART_STATE_FILE.name}.",
        dir=str(RESTART_STATE_FILE.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, RESTART_STATE_FILE)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def load_restart_state() -> tuple[list[dict], int] | None:
    if not RESTART_STATE_FILE.exists():
        return None
    try:
        payload = json.loads(RESTART_STATE_FILE.read_text(encoding="utf-8"))
        messages = payload["messages"]
        restart_count = int(payload["restart_count"])
        RESTART_STATE_FILE.unlink(missing_ok=True)
        return messages, restart_count
    except Exception as e:
        print(f"PGN > Не удалось восстановить состояние после перезапуска: {e}")
        RESTART_STATE_FILE.unlink(missing_ok=True)
        return None


def run_agent_turn(
    messages: list[dict],
    restart_count: int = 0,
    task_state: object | None = None,
) -> object | None:
    """Основной цикл автономного агента PGN (совместим по сигнатуре с pai_g.py и bot.py)."""
    global LAST_REPLY
    LAST_REPLY = ""
    CANCEL_EVENT.clear()
    ensure_pgn_environment()

    self_updated = False

    for step in range(1, MAX_AGENT_STEPS + 1):
        if CANCEL_EVENT.is_set():
            LAST_REPLY = "🛑 Выполнение прервано пользователем."
            print(f"PGN > {LAST_REPLY}")
            return None

        messages[:] = compact_messages(messages)

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
            reasoning_effort="none",
        )

        if CANCEL_EVENT.is_set():
            LAST_REPLY = "🛑 Выполнение прервано пользователем."
            print(f"PGN > {LAST_REPLY}")
            return None

        message = response.choices[0].message
        assistant_message: dict = {
            "role": "assistant",
            "content": message.content or "",
        }

        if message.tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ]

        messages.append(assistant_message)

        # Если модель завершила цепочку вызовов и дала финальный ответ
        if not message.tool_calls:
            if self_updated and CONFIRM_CALLBACK is None and restart_count < MAX_CONSECUTIVE_SELF_RESTARTS:
                save_restart_state(messages, restart_count + 1)
                print("PGN > Исходный код обновлён и проверен. Перезапуск процесса...")
                sys.stdout.flush()
                os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])

            LAST_REPLY = message.content or "(пустой ответ)"
            print(f"PGN > {LAST_REPLY}")
            return None

        cancelled_by_user = False

        for tc in message.tool_calls:
            if cancelled_by_user or CANCEL_EVENT.is_set():
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "Операция отменена пользователем.",
                })
                continue

            tool_name = tc.function.name
            fp_before = source_fingerprint()

            try:
                arguments = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as e:
                result = f"Ошибка JSON в аргументах инструмента: {e}"
                arguments = {}
            else:
                allowed, level, denial_reason = authorize_tool(tool_name, arguments)
                if not allowed:
                    result = denial_reason or "Операция отклонена."
                else:
                    try:
                        print(f"[EXEC] {tool_name} [{level.value}]")
                        result = call_tool(tool_name, arguments)
                    except KeyError as e:
                        result = f"Ошибка: отсутствует обязательный аргумент {e}."
                    except Exception as e:
                        result = f"Ошибка выполнения {tool_name}: {e}"

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

            if result == "Операция отменена пользователем.":
                cancelled_by_user = True

            if (
                tool_name in ("write_file", "edit_file")
                and is_current_source(str(arguments.get("path", "")))
                and fp_before != source_fingerprint()
                and "Синтаксис Python корректен" in result
            ):
                self_updated = True

        if cancelled_by_user or CANCEL_EVENT.is_set():
            LAST_REPLY = "🛑 Действие остановлено пользователем."
            print(f"PGN > {LAST_REPLY}")
            return None

    LAST_REPLY = f"Достигнут лимит {MAX_AGENT_STEPS} шагов агента."
    print(f"PGN > {LAST_REPLY}")
    return None


def main() -> None:
    ensure_pgn_environment()
    print("PGN (Personal AI Environment) запущен.")
    print("Для выхода: exit")
    print(f"Workspace: {WORKSPACE}")
    print(f"Backups:   {BACKUP_DIR}")
    print(f"Quarantine:{TRASH_DIR}")
    print(f"Model:     {MODEL}\n")

    restored = load_restart_state()
    if restored is None:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        restart_count = 0
    else:
        messages, restart_count = restored
        print("PGN > Сессия восстановлена после самообновления.")
        try:
            run_agent_turn(messages, restart_count)
        except Exception as e:
            print(f"PGN > Ошибка связи с LLM: {e}")

    while True:
        try:
            user_input = input("Ты > ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if user_input.strip().lower() == "exit":
            break
        if not user_input.strip():
            continue

        messages.append({"role": "user", "content": user_input})
        CANCEL_EVENT.clear()

        try:
            run_agent_turn(messages, restart_count)
        except Exception as e:
            print(f"PGN > Ошибка связи с LLM: {e}")


if __name__ == "__main__":
    main()
