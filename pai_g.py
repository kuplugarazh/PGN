import threading
import configparser
import json
import os
import py_compile
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from openai import OpenAI


MODEL = "Ornith-1.5-9B-Q4_K_M.gguf"

PAI_ROOT = Path.home() / "pai"
WORKSPACE = Path.home() / "PAIWorkspace"

MAX_CONTEXT_TOKENS = 53248
COMPACT_THRESHOLD_CHARS = 110_000

CONFIRM_CALLBACK: Callable[[str, dict, "PermissionLevel"], bool] | None = None
SEND_PHOTO_CALLBACK: Callable[[str, str], bool] | None = None
LAST_REPLY: str = ""
CANCEL_EVENT = threading.Event()
LAST_SCREENSHOT_PATH: str | None = None

SHELL_OPERATORS = (";", "&&", "||", "`", "$(", ">", "<")

# Русские алиасы для умного поиска приложений и окон
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


def resolve_inside(root: Path, path: str) -> tuple[Path | None, str | None]:
    target = (root / path).expanduser().resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None, f"Ошибка: путь выходит за пределы разрешённой директории: {root}"
    return target, None


# =========================
# PAIWorkspace tools
# =========================

def list_directory(path: str) -> str:
    if path in ("", ".", "PAIWorkspace", "~/PAIWorkspace"):
        target = WORKSPACE.resolve()
    else:
        target, error = resolve_inside(WORKSPACE, path)
        if error:
            return error

    if not target.exists():
        return f"Ошибка: директория не существует: {target}"
    if not target.is_dir():
        return f"Ошибка: это не директория: {target}"

    try:
        items = []
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            kind = "DIR " if item.is_dir() else "FILE"
            items.append(f"{kind} {item.name}")
        return "\n".join(items) if items else "(пусто)"
    except OSError as e:
        return f"Ошибка чтения директории: {e}"


def create_directory(path: str) -> str:
    target, error = resolve_inside(WORKSPACE, path)
    if error:
        return error

    if target == WORKSPACE.resolve():
        return "Ошибка: PAIWorkspace уже существует."

    if target.exists():
        if target.is_dir():
            return f"Директория уже существует: {target}"
        return f"Ошибка: файл уже существует: {target}"

    try:
        target.mkdir(parents=True)
        return f"Директория создана: {target}"
    except OSError as e:
        return f"Ошибка создания директории: {e}"


def read_file(path: str) -> str:
    target, error = resolve_inside(WORKSPACE, path)
    if error:
        return error

    if not target.exists():
        return f"Ошибка: файл не существует: {target}"
    if not target.is_file():
        return f"Ошибка: это не файл: {target}"

    try:
        content = target.read_text(encoding="utf-8")
        if len(content) > 20_000:
            return content[:20_000] + "\n[Файл обрезан: превышен лимит 20000 символов]"
        return content
    except UnicodeDecodeError:
        return "Ошибка: файл не является UTF-8 текстовым файлом."
    except OSError as e:
        return f"Ошибка чтения файла: {e}"


def write_file(path: str, content: str) -> str:
    target, error = resolve_inside(WORKSPACE, path)
    if error:
        return error

    if target.exists() and target.is_dir():
        return f"Ошибка: указанный путь является директорией: {target}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Файл записан: {target}"
    except OSError as e:
        return f"Ошибка записи файла: {e}"


def delete_file(path: str) -> str:
    target, error = resolve_inside(WORKSPACE, path)
    if error:
        return error

    if not target.exists():
        return f"Ошибка: файл не существует: {target}"
    if not target.is_file():
        return f"Ошибка: это не файл: {target}"

    try:
        target.unlink()
        return f"Файл удалён: {target}"
    except OSError as e:
        return f"Ошибка удаления файла: {e}"


def move_file(source: str, destination: str) -> str:
    source_target, error = resolve_inside(WORKSPACE, source)
    if error:
        return f"Ошибка исходного пути: {error}"

    dest_target, error = resolve_inside(WORKSPACE, destination)
    if error:
        return f"Ошибка целевого пути: {error}"

    if not source_target.exists():
        return f"Ошибка: исходный файл не существует: {source_target}"
    if not source_target.is_file():
        return f"Ошибка: исходный путь не является файлом: {source_target}"

    if dest_target.exists():
        if dest_target.is_dir():
            dest_target = dest_target / source_target.name
            if dest_target.exists():
                return f"Ошибка: файл уже существует: {dest_target}"
        else:
            return f"Ошибка: целевой файл уже существует: {dest_target}"

    try:
        dest_target.parent.mkdir(parents=True, exist_ok=True)
        source_target.rename(dest_target)
        return f"Файл перемещён: {source_target} → {dest_target}"
    except OSError as e:
        return f"Ошибка перемещения файла: {e}"


# =========================
# PAI project tools
# =========================

def list_project_directory(path: str = ".") -> str:
    target, error = resolve_inside(PAI_ROOT, path)
    if error:
        return "Ошибка: нельзя просматривать директории за пределами проекта PAI."

    if not target.exists():
        return f"Ошибка: директория не существует: {target}"
    if not target.is_dir():
        return f"Ошибка: это не директория: {target}"

    try:
        items = []
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            kind = "DIR " if item.is_dir() else "FILE"
            items.append(f"{kind} {item.name}")
        return "\n".join(items) if items else "(пусто)"
    except OSError as e:
        return f"Ошибка чтения директории проекта: {e}"


def read_project_file(path: str) -> str:
    target, error = resolve_inside(PAI_ROOT, path)
    if error:
        return "Ошибка: нельзя читать файлы за пределами проекта PAI."

    if not target.exists():
        return f"Ошибка: файл не существует: {target}"
    if not target.is_file():
        return f"Ошибка: это не файл: {target}"

    try:
        content = target.read_text(encoding="utf-8")
        if len(content) > 20_000:
            return content[:20_000] + "\n[Файл обрезан: превышен лимит 20000 символов]"
        return content
    except UnicodeDecodeError:
        return "Ошибка: файл не является UTF-8 текстовым файлом."
    except OSError as e:
        return f"Ошибка чтения файла проекта: {e}"


def write_project_file(path: str, content: str) -> str:
    target, error = resolve_inside(PAI_ROOT, path)
    if error:
        return "Ошибка: нельзя записывать файлы за пределами проекта PAI."

    if target.exists() and target.is_dir():
        return f"Ошибка: указанный путь является директорией: {target}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Файл записан: {target}"
    except OSError as e:
        return f"Ошибка записи файла проекта: {e}"


def read_source(path: str = "pai_g.py") -> str:
    return read_project_file(path)


def edit_project_file(path: str, old_text: str, new_text: str) -> str:
    target, error = resolve_inside(PAI_ROOT, path)
    if error:
        return "Ошибка: нельзя изменять файлы за пределами проекта PAI."

    if not target.exists():
        return f"Ошибка: файл не существует: {target}"
    if not target.is_file():
        return f"Ошибка: это не файл: {target}"

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "Ошибка: файл не является UTF-8 текстовым файлом."
    except OSError as e:
        return f"Ошибка чтения файла для редактирования: {e}"

    count = content.count(old_text)

    if count == 0:
        return "Ошибка: исходный фрагмент не найден."
    if count > 1:
        return (
            f"Ошибка: исходный фрагмент найден {count} раз. "
            "Требуется ровно одно совпадение."
        )

    try:
        target.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Файл успешно изменён: {target}"
    except OSError as e:
        return f"Ошибка записи файла: {e}"


def edit_source(old_text: str, new_text: str) -> str:
    return edit_project_file(Path(__file__).name, old_text, new_text)


def check_syntax(path: str = "pai_g.py") -> str:
    target, error = resolve_inside(PAI_ROOT, path)
    if error:
        return "Ошибка: нельзя проверять файлы за пределами проекта PAI."

    if not target.exists():
        return f"Ошибка: файл не существует: {target}"
    if not target.is_file():
        return f"Ошибка: это не файл: {target}"

    if target.suffix != ".py":
        return f"Ошибка: check_syntax работает только с Python-файлами: {target}"

    try:
        py_compile.compile(str(target), doraise=True)
        return f"Синтаксис {target.relative_to(PAI_ROOT.resolve())} корректен."
    except py_compile.PyCompileError as e:
        return f"Ошибка синтаксиса {target.relative_to(PAI_ROOT.resolve())}: {e}"
    except Exception as e:
        return f"Ошибка проверки синтаксиса: {e}"


def git_diff(path: str = ".") -> str:
    target, error = resolve_inside(PAI_ROOT, path)
    if error:
        return "Ошибка: нельзя смотреть diff за пределами проекта PAI."

    relative_path = target.relative_to(PAI_ROOT.resolve())

    try:
        process = subprocess.run(
            ["git", "-C", str(PAI_ROOT), "diff", "--", str(relative_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "Ошибка: git diff превысил лимит времени 10 секунд."
    except OSError as e:
        return f"Ошибка запуска git diff: {e}"

    if process.returncode != 0:
        return f"Ошибка git diff:\n{process.stderr}"

    return process.stdout if process.stdout else f"(изменений в {relative_path} нет)"


def run_tests() -> str:
    try:
        process = subprocess.run(
            ["pytest", "-q"],
            cwd=str(PAI_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )

        output = [f"Код завершения: {process.returncode}"]

        if process.stdout:
            stdout = process.stdout[:20_000]
            if len(process.stdout) > 20_000:
                stdout += "\n[STDOUT обрезан: превышен лимит 20000 символов]"
            output.append(f"STDOUT:\n{stdout}")

        if process.stderr:
            stderr = process.stderr[:20_000]
            if len(process.stderr) > 20_000:
                stderr += "\n[STDERR обрезан: превышен лимит 20000 символов]"
            output.append(f"STDERR:\n{stderr}")

        return "\n".join(output)

    except subprocess.TimeoutExpired:
        return "Ошибка: тесты превысили лимит времени 60 секунд."
    except Exception as e:
        return f"Ошибка запуска тестов: {e}"


# =========================
# Hyprland backend & Window Control
# =========================

class HyprlandBackend:
    """Adapter for Hyprland's JSON IPC and dispatch via hyprctl."""

    def _require_session(self) -> str | None:
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return "Ошибка: PAI не запущен внутри активной Hyprland-сессии."
        return None

    def _query(self, subject: str) -> tuple[object | None, str | None]:
        session_error = self._require_session()
        if session_error:
            return None, session_error

        try:
            process = subprocess.run(
                ["hyprctl", "-j", subject],
                capture_output=True,
                text=True,
                timeout=5,
                shell=False,
            )
        except FileNotFoundError:
            return None, "Ошибка Hyprland: утилита hyprctl не найдена."
        except subprocess.TimeoutExpired:
            return None, "Ошибка Hyprland: hyprctl превысил лимит времени 5 секунд."
        except OSError as e:
            return None, f"Ошибка Hyprland: не удалось запустить hyprctl: {e}"

        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip() or f"код {process.returncode}"
            return None, f"Ошибка Hyprland: hyprctl {subject} завершился с ошибкой: {detail}"

        try:
            return json.loads(process.stdout), None
        except json.JSONDecodeError as e:
            return None, f"Ошибка Hyprland: hyprctl {subject} вернул некорректный JSON: {e.msg}"

    def _dispatch(self, expr: str) -> tuple[bool, str]:
        session_error = self._require_session()
        if session_error:
            return False, session_error

        try:
            process = subprocess.run(
                ["hyprctl", "dispatch", expr],
                capture_output=True,
                text=True,
                timeout=5,
                shell=False,
            )
        except FileNotFoundError:
            return False, "Ошибка Hyprland: утилита hyprctl не найдена."
        except subprocess.TimeoutExpired:
            return False, "Ошибка Hyprland: команда превысила лимит времени 5 секунд."
        except OSError as e:
            return False, f"Ошибка Hyprland: не удалось запустить hyprctl: {e}"

        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip() or f"код {process.returncode}"
            return False, f"Ошибка Hyprland ({expr}): {detail}"
        return True, process.stdout.strip()

    @staticmethod
    def normalize_client(client: object) -> dict:
        if not isinstance(client, dict):
            return {}
        normalized = dict(client)
        workspace = client.get("workspace")
        normalized["address"] = client.get("address")
        normalized["class"] = client.get("class")
        normalized["title"] = client.get("title")
        normalized["workspace"] = workspace if isinstance(workspace, dict) else workspace
        normalized["pid"] = client.get("pid")
        if "mapped" in client:
            normalized["mapped"] = client["mapped"]
        if "focused" in client:
            normalized["focused"] = client["focused"]
        return normalized

    @staticmethod
    def normalize_workspace(workspace: object) -> dict:
        if not isinstance(workspace, dict):
            return {}
        normalized = dict(workspace)
        normalized["id"] = workspace.get("id")
        normalized["name"] = workspace.get("name")
        normalized["windows"] = workspace.get("windows")
        if "focused" in workspace:
            normalized["focused"] = workspace["focused"]
        return normalized

    def get_clients(self) -> str:
        payload, error = self._query("clients")
        if error:
            return error
        if not isinstance(payload, list):
            return "Ошибка Hyprland: hyprctl clients вернул JSON не в виде списка."
        return json.dumps([self.normalize_client(item) for item in payload], ensure_ascii=False)

    def get_workspaces(self) -> str:
        payload, error = self._query("workspaces")
        if error:
            return error
        if not isinstance(payload, list):
            return "Ошибка Hyprland: hyprctl workspaces вернул JSON не в виде списка."
        return json.dumps([self.normalize_workspace(item) for item in payload], ensure_ascii=False)

    @staticmethod
    def validate_workspace(workspace: object) -> tuple[str | None, str | None]:
        if isinstance(workspace, int) and not isinstance(workspace, bool):
            if workspace > 0:
                return str(workspace), None
            return None, "Ошибка: номер рабочего стола должен быть положительным целым числом."
        if not isinstance(workspace, str):
            return None, "Ошибка: рабочий стол должен быть номером или непустым именем."

        value = workspace.strip()
        if not value:
            return None, "Ошибка: имя рабочего стола не должно быть пустым."
        if value.isdecimal():
            if int(value) > 0:
                return value, None
            return None, "Ошибка: номер рабочего стола должен быть положительным целым числом."
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            return None, "Ошибка: имя рабочего стола содержит недопустимые символы."
        return value, None

    def switch_workspace(self, workspace: object) -> str:
        target, validation_error = self.validate_workspace(workspace)
        if validation_error:
            return validation_error

        ok, detail = self._dispatch(f'hl.dsp.focus({{ workspace = "{target}" }})')
        if not ok:
            return f"Ошибка Hyprland: не удалось переключиться на рабочий стол {target}: {detail}"
        return f"Переключение на рабочий стол {target} выполнено."

    def exec_command(self, command: str, workspace: int | None = None) -> str:
        session_error = self._require_session()
        if session_error:
            return session_error

        try:
            if workspace is not None:
                self._dispatch(f'hl.dsp.focus({{ workspace = "{workspace}" }})')

            lua_cmd = json.dumps(command, ensure_ascii=False)
            ok, detail = self._dispatch(f"hl.dsp.exec_cmd({lua_cmd})")
        except Exception as e:
            return f"Ошибка Hyprland exec: {e}"

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

        return None, f"Ошибка: окно '{target}' не найдено среди открытых клиентов."

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

        # Если нужно управлять конкретным неактивным окном — переключаемся на его стол
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
            if not ok:
                return detail
            return f"Окно {cls_name} ('{title_name}') закрыто."

        if action == "fullscreen":
            ok, detail = self._dispatch("hl.dsp.window.fullscreen()")
            if not ok:
                return detail
            return f"Переключён полноэкранный режим для окна {cls_name} ('{title_name}')."

        if action == "minimize":
            ok, detail = self._dispatch('hl.dsp.window.move({ workspace = "special:minimized" })')
            if not ok:
                return detail
            return f"Окно {cls_name} ('{title_name}') свёрнуто в special:minimized."

        if action == "move":
            if workspace is None:
                return "Ошибка: для действия 'move' необходимо указать параметр workspace."
            valid_ws, ws_err = self.validate_workspace(workspace)
            if ws_err:
                return ws_err
            ws_arg = valid_ws if valid_ws.isdecimal() else f'"{valid_ws}"'
            ok, detail = self._dispatch(f"hl.dsp.window.move({{ workspace = {ws_arg} }})")
            if not ok:
                return detail
            return f"Окно {cls_name} ('{title_name}') перемещено на рабочий стол {valid_ws}."

        return f"Ошибка: неизвестное действие управления окном: {action}"


hyprland_backend = HyprlandBackend()


def hyprland_get_clients() -> str:
    return hyprland_backend.get_clients()


def hyprland_get_workspaces() -> str:
    return hyprland_backend.get_workspaces()


def hyprland_switch_workspace(workspace: object) -> str:
    return hyprland_backend.switch_workspace(workspace)


def hyprland_exec(command: str, workspace: int | None = None) -> str:
    return hyprland_backend.exec_command(command, workspace)


def hyprland_window_control(
    action: str,
    target: str | None = None,
    workspace: int | str | None = None,
) -> str:
    return hyprland_backend.control_window(action, target, workspace)


# =========================
# 1. Smart App Launcher (.desktop parser)
# =========================

def app_launcher(app_name: str, workspace: int | None = None) -> str:
    """Умный запуск приложения из системных .desktop файлов."""
    query = app_name.strip().lower()
    if not query:
        return "Ошибка: название приложения не может быть пустым."

    if query in ("яндекс музыка", "yandex music", "yandex-music", "yandexmusic"):
        return yandex_music_control(action="open_only", workspace=workspace)

    search_terms = [query] + APP_ALIASES.get(query, [])

    desktop_dirs = [
        Path.home() / ".local/share/applications",
        Path("/usr/share/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
        Path.home() / ".local/share/flatpak/exports/share/applications",
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

                searchable_primary = [name_en.lower(), name_ru.lower(), stem]
                searchable_secondary = [generic.lower(), generic_ru.lower(), keywords.lower(), raw_exec.lower()]

                best_score = 0
                for term in search_terms:
                    for field_val in searchable_primary:
                        if not field_val:
                            continue
                        if field_val == term:
                            best_score = max(best_score, 100)
                        elif field_val.startswith(term):
                            best_score = max(best_score, 80)
                        elif term in field_val:
                            best_score = max(best_score, 60)
                    for field_val in searchable_secondary:
                        if field_val and term in field_val:
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

    # ИСПРАВЛЕНИЕ: Раз это официальный .desktop файл системы, доверяем ему 
    # и запускаем без избыточных подтверждений (на уровне LOW).
    # Но если внутри команды кто-то попытается протащить явную дичь со спецсимволами оболочки — 
    # hyprland_exec всё равно это поймает.
    res = hyprland_exec(best_exec, workspace)
    return f"Найдено приложение '{best_name}' (команда: {best_exec}). {res}"
# =========================
# 2. Package Manager (pacman / yay)
# =========================

def package_manager(action: str, package: str) -> str:
    """Управление пакетами через yay (search, install, remove) без графического терминала."""
    pkg_clean = package.strip()
    if not pkg_clean or not re.fullmatch(r"[a-zA-Z0-9@._+-]+(\s+[a-zA-Z0-9@._+-]+)*", pkg_clean):
        return f"Ошибка: некорректное имя пакета или поискового запроса: '{package}'"

    pkg_list = pkg_clean.split()

    if action == "search":
        cmd = ["yay", "-Ss", *pkg_list]
        timeout = 30
    elif action == "install":
        cmd = ["yay", "-S", "--noconfirm", "--needed", "--sudoflags=-n", *pkg_list]
        timeout = 180
    elif action == "remove":
        cmd = ["yay", "-Rns", "--noconfirm", "--sudoflags=-n", *pkg_list]
        timeout = 90
    else:
        return f"Ошибка: неизвестное действие package_manager: {action}"

    try:
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except FileNotFoundError:
        return "Ошибка: утилита yay не найдена в системе."
    except subprocess.TimeoutExpired:
        return f"Ошибка: операция yay ({action}) превысила лимит времени ({timeout} сек)."
    except Exception as e:
        return f"Ошибка запуска yay: {e}"

    combined = (process.stdout or "") + "\n" + (process.stderr or "")
    if "a password is required" in combined.lower() or "sudo:" in combined.lower() and "password" in combined.lower():
        return (
            "Ошибка: yay потребовал пароль sudo в фоновом режиме. "
            "Чтобы фоновая установка работала без зависаний, разреши pacman без пароля: "
            "создай файл /etc/sudoers.d/pai-pacman со строкой:\n"
            f"{os.environ.get('USER', 'gomosek')} ALL=(ALL) NOPASSWD: /usr/bin/pacman"
        )

    if process.returncode != 0:
        return f"Ошибка yay ({action}, код {process.returncode}):\n{combined.strip()[:4000]}"

    out = (process.stdout or "").strip()
    if action == "search":
        if not out:
            return f"По запросу '{pkg_clean}' пакетов не найдено."
        lines = out.splitlines()
        if len(lines) > 60:
            out = "\n".join(lines[:60]) + f"\n...[показаны первые 60 строк из {len(lines)}]"
        return out

    return f"Успешно выполнено ({action}) для '{pkg_clean}':\n{out[-2000:]}"


# =========================
# 3. Screenshot to Telegram / Disk
# =========================

def take_screenshot(filename: str = "pai_screenshot.png") -> str:
    """Делает скриншот экрана через grim и отправляет его в Telegram (если бот активен)."""
    global LAST_SCREENSHOT_PATH

    safe_name = Path(filename).name
    if not safe_name.endswith(".png"):
        safe_name += ".png"
    target_path = Path("/tmp") / safe_name

    try:
        process = subprocess.run(
            ["grim", str(target_path)],
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
    except FileNotFoundError:
        return "Ошибка: утилита 'grim' не установлена (установи через package_manager: grim)."
    except subprocess.TimeoutExpired:
        return "Ошибка: создание скриншота grim превысило 10 секунд."
    except Exception as e:
        return f"Ошибка создания скриншота: {e}"

    if process.returncode != 0 or not target_path.exists():
        return f"Ошибка grim: {process.stderr.strip() or process.stdout.strip()}"

    LAST_SCREENSHOT_PATH = str(target_path)

    # 1. Если бот зарегистрировал callback для отправки фото
    if SEND_PHOTO_CALLBACK is not None:
        try:
            sent = SEND_PHOTO_CALLBACK(str(target_path), f"📸 Скриншот экрана ({safe_name})")
            if sent:
                return f"Скриншот сохранён в {target_path} и успешно отправлен в Telegram."
        except Exception as e:
            return f"Скриншот сохранён в {target_path}, но callback отправки вернул ошибку: {e}"

    # 2. Автоматический fallback через переменные окружения бота (если заданы)
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
                return f"Скриншот сохранён ({target_path}) и напрямую отправлен в твой Telegram-чат."
        except Exception:
            pass

    return f"Скриншот успешно сохранён: {target_path}"


# =========================
# 4. System Control (Volume, Clipboard, Processes)
# =========================

def system_volume(action: str, value: int | None = None) -> str:
    """Управление громкостью PipeWire/WirePlumber через wpctl."""
    sink = "@DEFAULT_AUDIO_SINK@"

    try:
        if action == "get":
            proc = subprocess.run(["wpctl", "get-volume", sink], capture_output=True, text=True, timeout=5)
            if proc.returncode != 0:
                return f"Ошибка wpctl: {proc.stderr.strip()}"
            return f"Текущая громкость: {proc.stdout.strip()}"

        if action == "mute":
            subprocess.run(["wpctl", "set-mute", sink, "1"], check=True, timeout=5)
            return "Звук выключен (Mute)."

        if action == "unmute":
            subprocess.run(["wpctl", "set-mute", sink, "0"], check=True, timeout=5)
            return "Звук включён (Unmute)."

        if action == "toggle_mute":
            subprocess.run(["wpctl", "set-mute", sink, "toggle"], check=True, timeout=5)
            proc = subprocess.run(["wpctl", "get-volume", sink], capture_output=True, text=True, timeout=5)
            return f"Состояние Mute переключено. {proc.stdout.strip()}"

        if action in ("set", "up", "down"):
            step = value if value is not None else 10
            step = max(0, min(150, int(step)))
            if action == "set":
                arg = f"{step}%"
            elif action == "up":
                arg = f"{step}%+"
            else:
                arg = f"{step}%-"

            subprocess.run(["wpctl", "set-volume", "-l", "1.5", sink, arg], check=True, timeout=5)
            proc = subprocess.run(["wpctl", "get-volume", sink], capture_output=True, text=True, timeout=5)
            return f"Громкость изменена ({action} {step}%). {proc.stdout.strip()}"

        return f"Ошибка: неизвестное действие system_volume: {action}"
    except FileNotFoundError:
        return "Ошибка: утилита wpctl не найдена."
    except Exception as e:
        return f"Ошибка управления звуком: {e}"


def clipboard_control(action: str, text: str | None = None) -> str:
    """Чтение и запись буфера обмена Wayland через wl-paste и wl-copy."""
    try:
        if action == "read":
            proc = subprocess.run(
                ["wl-paste", "--no-newline"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode != 0:
                return f"Буфер обмена пуст или недоступен: {proc.stderr.strip()}"
            content = proc.stdout
            if len(content) > 10_000:
                content = content[:10_000] + "\n...[объем буфера обрезан до 10000 символов]"
            return content if content else "(буфер обмена пуст)"

        if action == "write":
            if text is None:
                return "Ошибка: для записи в буфер обмена необходимо передать параметр text."
            subprocess.run(
                ["wl-copy"],
                input=text,
                text=True,
                check=True,
                timeout=5,
            )
            return f"Текст скопирован в буфер обмена ({len(text)} симв.)."

        if action == "clear":
            subprocess.run(["wl-copy", "--clear"], check=True, timeout=5)
            return "Буфер обмена очищен."

        return f"Ошибка: неизвестное действие clipboard_control: {action}"
    except FileNotFoundError:
        return "Ошибка: пакет wl-clipboard (wl-copy / wl-paste) не установлен."
    except Exception as e:
        return f"Ошибка работы с буфером обмена: {e}"


PROTECTED_PROCESSES = {
    "systemd", "init", "hyprland", "xwayland", "dbus-daemon",
    "pipewire", "wireplumber", "llama-server", "python", "python3",
}


def process_manager(action: str, query: str | None = None) -> str:
    """Просмотр топ-процессов, поиск и безопасное завершение зависших процессов."""
    try:
        if action == "list":
            proc = subprocess.run(
                ["ps", "-eo", "pid,pcpu,pmem,comm", "--sort=-pcpu"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = proc.stdout.strip().splitlines()
            return "\n".join(lines[:26])

        if not query or not query.strip():
            return "Ошибка: для действий 'search' и 'kill' необходимо указать параметр query (имя или PID)."

        q = query.strip()
        q_aliases = APP_ALIASES.get(q.lower(), [q])
        primary_target = q_aliases[0]

        if action == "search":
            proc = subprocess.run(
                ["pgrep", "-afi", primary_target],
                capture_output=True,
                text=True,
                timeout=5,
            )
            out = proc.stdout.strip()
            return out if out else f"Процессы по запросу '{primary_target}' не найдены."

        if action == "kill":
            if primary_target.lower() in PROTECTED_PROCESSES or "bot.py" in primary_target.lower() or "pai" in primary_target.lower():
                return f"Ошибка: процесс '{primary_target}' защищён от завершения."

            if primary_target.isdecimal():
                pid = int(primary_target)
                if pid in (os.getpid(), os.getppid(), 1):
                    return "Ошибка: нельзя убить процесс самого PAI, его родителя или init."
                
                # ИСПРАВЛЕНИЕ FAIL-OPEN: Если не удалось верифицировать имя через /proc, строго запрещаем kill
                try:
                    comm_path = Path(f"/proc/{pid}/comm")
                    if not comm_path.exists():
                        return f"Ошибка: процесс с PID {pid} не найден в системе."
                    comm = comm_path.read_text(encoding="utf-8").strip().lower()
                    if comm in PROTECTED_PROCESSES or comm in ("systemd", "init", "hyprland", "python", "python3"):
                        return f"Ошибка: процесс с PID {pid} ({comm}) защищён от завершения."
                except Exception as e:
                    return f"Ошибка безопасности (Fail-Safe): не удалось прочитать имя процесса PID {pid} ({e}). Операция отменена."
                
                proc = subprocess.run(["kill", "-15", str(pid)], capture_output=True, text=True, timeout=5)
            else:
                proc = subprocess.run(["pkill", "-x", primary_target[:15]], capture_output=True, text=True, timeout=5)

            if proc.returncode != 0:
                return f"Не удалось завершить '{primary_target}' (процесс не найден)."
            return f"Сигнал завершения (SIGTERM) отправлен процессу '{primary_target}'."

        return f"Ошибка: неизвестное действие process_manager: {action}"
    except Exception as e:
        return f"Ошибка управления процессами: {e}"

def yandex_music_control(action: str = "play_pause", workspace: int | None = None) -> str:
    """Launch Yandex Music if not running and control playback via CDP WebSocket in background."""
    import websocket

    def get_ym_ws_url() -> str | None:
        try:
            data = urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=1).read()
            targets = json.loads(data)
            for t in targets:
                if t.get("type") == "page" and "music-application" in t.get("url", ""):
                    return t.get("webSocketDebuggerUrl")
        except Exception:
            return None
        return None

    ws_url = get_ym_ws_url()
    launched_now = False

    if not ws_url:
        cmd = "/opt/yandex-music/yandexmusic --gtk-version=3 --remote-debugging-port=9222"
        hyprland_exec(cmd, workspace)
        launched_now = True
        for _ in range(20):
            time.sleep(0.5)
            ws_url = get_ym_ws_url()
            if ws_url:
                break
        if not ws_url:
            return "Яндекс Музыка запущена, но порт 9222 не ответил (возможно она уже была открыта без флага --remote-debugging-port=9222)."
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
        return f"Яндекс Музыка {status}выполнила действие: {action} (фоновый режим CDP)."
    except Exception as e:
        return f"Ошибка управления Яндекс Музыкой через WebSocket: {e}"


# =========================
# System shell fallback
# =========================

def execute_shell(command: str) -> str:
    try:
        process = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=30,
        )

        output = [f"Код завершения: {process.returncode}"]

        if process.stdout:
            stdout = process.stdout[:10_000]
            if len(process.stdout) > 10_000:
                stdout += "\n[STDOUT обрезан: превышен лимит 10000 символов]"
            output.append(f"STDOUT:\n{stdout}")

        if process.stderr:
            stderr = process.stderr[:10_000]
            if len(process.stderr) > 10_000:
                stderr += "\n[STDERR обрезан: превышен лимит 10000 символов]"
            output.append(f"STDERR:\n{stderr}")

        return "\n".join(output)

    except subprocess.TimeoutExpired:
        return "Ошибка: команда превысила лимит времени 30 секунд."
    except Exception as e:
        return f"Ошибка выполнения команды: {e}"


# =========================
# Tool definitions
# =========================

tools = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "Показать содержимое директории внутри PAIWorkspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь относительно PAIWorkspace. Для корня используй PAIWorkspace или '.'.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_directory",
            "description": "Создать директорию внутри PAIWorkspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать UTF-8 текстовый файл внутри PAIWorkspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Записать UTF-8 текстовый файл внутри PAIWorkspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Удалить файл внутри PAIWorkspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Переместить или переименовать файл внутри PAIWorkspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "destination": {"type": "string"},
                },
                "required": ["source", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_project_directory",
            "description": "Показать содержимое директории внутри проекта PAI (~/pai).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_project_file",
            "description": "Прочитать UTF-8 файл внутри проекта PAI (включая pai_g.py).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_project_file",
            "description": "Записать UTF-8 файл внутри проекта PAI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_project_file",
            "description": "Точно изменить файл проекта PAI: заменить ровно одно совпадение old_text на new_text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_syntax",
            "description": "Проверить синтаксис любого Python-файла внутри проекта PAI.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Показать git diff файла или каталога внутри проекта PAI.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Запустить pytest в проекте PAI.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "app_launcher",
            "description": (
                "Умный запуск любого установленного приложения по человеческому названию на русском или английском "
                "(например: 'телеграм', 'дискорд', 'браузер', 'стим', 'обсидиан', 'telegram', 'firefox'). "
                "Сам находит нужный .desktop файл и открывает на указанном рабочем столе."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Название приложения (например: 'телеграм', 'firefox', 'дискорд').",
                    },
                    "workspace": {
                        "type": "integer",
                        "description": "Опционально: номер рабочего стола Hyprland (например 2).",
                    },
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "package_manager",
            "description": "Поиск, установка и удаление пакетов Arch Linux / AUR через yay в фоновом режиме.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search", "install", "remove"],
                        "description": "Действие: search (поиск), install (установка), remove (удаление).",
                    },
                    "package": {
                        "type": "string",
                        "description": "Имя пакета (или нескольких через пробел).",
                    },
                },
                "required": ["action", "package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hyprland_get_clients",
            "description": "Получить список текущих открытых окон Hyprland.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hyprland_get_workspaces",
            "description": "Получить список рабочих столов Hyprland.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hyprland_switch_workspace",
            "description": "Переключить текущий рабочий стол Hyprland.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace": {
                        "anyOf": [{"type": "integer", "minimum": 1}, {"type": "string", "minLength": 1}],
                    }
                },
                "required": ["workspace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hyprland_window_control",
            "description": (
                "Управление открытыми окнами Hyprland: закрыть окно (close), переместить на другой рабочий стол (move), "
                "развернуть на весь экран (fullscreen), свернуть в фоновый стол (minimize), сфокусировать (focus)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["close", "move", "fullscreen", "minimize", "focus"],
                    },
                    "target": {
                        "type": "string",
                        "description": "Имя/класс окна (например 'telegram', 'kitty', 'firefox'). Если не указано — текущее активное окно.",
                    },
                    "workspace": {
                        "anyOf": [{"type": "integer", "minimum": 1}, {"type": "string", "minLength": 1}],
                        "description": "Целевой рабочий стол (обязателен только для action='move').",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hyprland_exec",
            "description": (
                "Запустить кастомную команду или терминал kitty в Hyprland (опционально на нужном рабочем столе). "
                "ВАЖНО: чтобы окно терминала kitty НЕ закрывалось сразу после выполнения команды (например fastfetch), "
                "всегда используй флаг --hold: kitty --hold fish -c 'fastfetch'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Команда для запуска, например: kitty --hold fish -c 'fastfetch'",
                    },
                    "workspace": {
                        "type": "integer",
                        "description": "Номер рабочего стола (например 3).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Сделать скриншот экрана ПК (через grim) и автоматически отправить его пользователю в Telegram.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Имя файла скриншота (по умолчанию 'pai_screenshot.png').",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_volume",
            "description": "Управление системной громкостью звука (get, set, up, down, mute, unmute, toggle_mute).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "set", "up", "down", "mute", "unmute", "toggle_mute"],
                    },
                    "value": {
                        "type": "integer",
                        "description": "Процент громкости для set (например 50) или шаг изменения для up/down (по умолчанию 10).",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clipboard_control",
            "description": "Чтение, запись или очистка буфера обмена Wayland (read, write, clear).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "write", "clear"],
                    },
                    "text": {
                        "type": "string",
                        "description": "Текст для записи в буфер обмена (только для action='write').",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_manager",
            "description": "Мониторинг процессов (list), поиск процесса по имени (search) и убийство зависших процессов (kill).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "search", "kill"],
                    },
                    "query": {
                        "type": "string",
                        "description": "Имя процесса или PID (для search и kill).",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "yandex_music_control",
            "description": (
                "Открыть Яндекс Музыку (если закрыта) и управлять воспроизведением в фоновом режиме "
                "(play_pause, play, pause, next, prev, like, open_only) без переключения фокуса окна."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play_pause", "play", "pause", "next", "prev", "like", "open_only"],
                    },
                    "workspace": {
                        "type": "integer",
                        "description": "Номер рабочего стола, если нужно открыть на конкретном столе.",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_shell",
            "description": "Выполнить shell-команду в Linux. Используй ТОЛЬКО если нет специализированного инструмента.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]


class PermissionLevel(str, Enum):
    READ = "READ"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


TOOL_PERMISSIONS = {
    "list_directory": PermissionLevel.READ,
    "create_directory": PermissionLevel.LOW,
    "read_file": PermissionLevel.READ,
    "write_file": PermissionLevel.LOW,
    "delete_file": PermissionLevel.HIGH,
    "move_file": PermissionLevel.LOW,
    "list_project_directory": PermissionLevel.READ,
    "read_project_file": PermissionLevel.READ,
    "write_project_file": PermissionLevel.HIGH,
    "read_source": PermissionLevel.READ,
    "edit_source": PermissionLevel.HIGH,
    "edit_project_file": PermissionLevel.HIGH,
    "check_syntax": PermissionLevel.READ,
    "git_diff": PermissionLevel.READ,
    "run_tests": PermissionLevel.READ,
    "app_launcher": PermissionLevel.LOW,
    "package_manager": PermissionLevel.MEDIUM,
    "hyprland_get_clients": PermissionLevel.READ,
    "hyprland_get_workspaces": PermissionLevel.READ,
    "hyprland_switch_workspace": PermissionLevel.LOW,
    "hyprland_window_control": PermissionLevel.LOW,
    "hyprland_exec": PermissionLevel.LOW,
    "take_screenshot": PermissionLevel.READ,
    "system_volume": PermissionLevel.LOW,
    "clipboard_control": PermissionLevel.LOW,
    "process_manager": PermissionLevel.LOW,
    "yandex_music_control": PermissionLevel.LOW,
    "execute_shell": PermissionLevel.HIGH,
}


CRITICAL_SHELL_MARKERS = {
    "sudo", "su", "doas", "pkexec", "shutdown", "reboot", "poweroff", "halt",
    "mkfs", "fdisk", "parted", "mount", "umount", "dd", "wipefs", "useradd",
    "userdel", "usermod", "passwd", "visudo", "iptables", "nft", "setcap",
}
ALLOWED_CRITICAL_SHELL_COMMANDS = {"shutdown", "reboot", "poweroff", "halt"}

# Белый список команд, которые только читают состояние и безопасны без подтверждения
SAFE_READ_SHELL_COMMANDS = {
    "ls", "pwd", "whoami", "id", "date", "uptime", "uname", "fastfetch",
    "cat", "head", "tail", "wc", "stat", "file", "du", "df", "free",
    "ps", "pgrep", "grep", "rg", "find", "which", "whereis", "echo",
    "git", "hyprctl", "wpctl", "playerctl", "sensors", "lspci", "lsblk",
    "touch",
}

HIGH_RISK_SHELL_COMMANDS = {
    "rm", "rmdir", "unlink", "shred", "truncate", "mv", "cp",
    "kill", "killall", "pkill", "chmod", "chown", "chgrp",
    "systemctl", "pacman", "yay", "paru", "pip", "curl", "wget",
    "ssh", "scp", "rsync", "python", "python3", "bash", "sh", "fish", "zsh",
}


def shell_command_level(command: str) -> PermissionLevel:
    cmd_stripped = command.strip()
    if not cmd_stripped:
        return PermissionLevel.READ

    try:
        tokens = shlex.split(cmd_stripped)
    except ValueError:
        tokens = cmd_stripped.split()

    if not tokens:
        return PermissionLevel.READ

    # Точный анализ для systemctl: проверяем первую реальную подкоманду после утилиты и её опций
    if Path(tokens[0]).name.lower() == "systemctl":
        subcmd = None
        for tok in tokens[1:]:
            if not tok.startswith("-"):
                subcmd = tok.lower()
                break
        if subcmd in ("status", "is-active", "is-enabled", "is-failed", "help"):
            return PermissionLevel.READ
        elif subcmd in ("start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask"):
            return PermissionLevel.HIGH

    words = {Path(t).name.lower() for t in tokens if not t.startswith("-")}

    # 1. Критические системные маркеры и системные директории
    if words.intersection(CRITICAL_SHELL_MARKERS):
        return PermissionLevel.CRITICAL

    cleaned_cmd = cmd_stripped.replace("/dev/null", "")
    if any(prefix in cleaned_cmd for prefix in ("/etc/", "/usr/", "/boot/", "/root", "/sys/", "/proc/")):
        return PermissionLevel.CRITICAL

    # 2. Любые операторы перенаправления (> / >>), подстановки $() / `` или пайпы/цепочки
    if any(op in cleaned_cmd for op in (">", "<", "`", "$(", ";", "&&", "||", "|")):
        return PermissionLevel.HIGH

    # 3. Опасные флаги у утилит из белого списка (например find -delete / -exec, git reset/clean)
    lowered = cleaned_cmd.lower()
    if "find" in words and any(flag in lowered for flag in ("-delete", "-exec", "-ok")):
        return PermissionLevel.HIGH
    if "git" in words and not any(sub in lowered for sub in ("status", "diff", "log", "branch", "show")):
        return PermissionLevel.MEDIUM

    # 4. Явно опасные команды (удаление, интерпретаторы, сеть, пакеты)
    if words.intersection(HIGH_RISK_SHELL_COMMANDS):
        if "systemctl" in words and ("status" in words or "is-active" in words):
            return PermissionLevel.READ
        return PermissionLevel.HIGH

    # 5. Если все слова входят в белый список чтения — выполняем без подтверждения
    if words and words.issubset(SAFE_READ_SHELL_COMMANDS):
        return PermissionLevel.READ

    # Все остальные неизвестные команды по умолчанию требуют подтверждения MEDIUM
    return PermissionLevel.MEDIUM


# Список утилит-обёрток, которые могут стоять перед основной командой
COMMAND_WRAPPERS = {"env", "nohup", "nice", "ionice", "stdbuf", "setsid", "time", "chrt", "taskset", "sudo", "doas", "pkexec"}
# Эмуляторы терминала
TERMINAL_BINARIES = {"kitty", "alacritty", "wezterm", "foot", "footclient", "gnome-terminal", "konsole"}
# Интерпретаторы и оболочки, исполняющие код через флаги (-c, -e)
INLINE_INTERPRETERS = {"bash", "sh", "zsh", "fish", "python", "python3", "perl", "ruby", "node", "lua", "busybox"}

def hyprland_exec_level(command: str) -> PermissionLevel:
    """Проверка безопасности произвольных команд для Hyprland (без хардкода GUI-приложений)."""
    cmd_stripped = command.strip()
    if not cmd_stripped:
        return PermissionLevel.MEDIUM

    # Безопасный запуск kitty с просмотрщиками
    m = re.fullmatch(r"kitty\s+--hold\s+fish\s+-c\s+'([^']+)'", cmd_stripped)
    if m:
        inner_level = shell_command_level(m.group(1))
        return PermissionLevel.LOW if inner_level == PermissionLevel.READ else inner_level

    if any(op in cmd_stripped for op in SHELL_OPERATORS) or "|" in cmd_stripped:
        return PermissionLevel.HIGH

    try:
        tokens = shlex.split(cmd_stripped)
    except ValueError:
        return PermissionLevel.HIGH

    if not tokens:
        return PermissionLevel.HIGH

    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        binary = Path(token).name.lower()

        if binary in CRITICAL_SHELL_MARKERS:
            return PermissionLevel.CRITICAL

        if binary in COMMAND_WRAPPERS:
            idx += 1
            if binary == "env" and idx < len(tokens):
                next_tok = tokens[idx]
                if next_tok in ("-S", "--split-string"):
                    idx += 1
                    if idx < len(tokens):
                        try:
                            sub_tokens = shlex.split(tokens[idx])
                            tokens = tokens[:idx] + sub_tokens + tokens[idx + 1:]
                        except ValueError:
                            return PermissionLevel.HIGH
                elif not next_tok.startswith("-"):
                    while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
                        idx += 1
            continue

        if binary in TERMINAL_BINARIES:
            idx += 1
            continue

        if binary in INLINE_INTERPRETERS:
            for i, t in enumerate(tokens[idx:]):
                if t in ("-c", "-e", "-eval") and idx + i + 1 < len(tokens):
                    inline_code = " ".join(tokens[idx + i + 1:])
                    return shell_command_level(inline_code)
            break

        break

    for token in tokens:
        b = Path(token).name.lower()
        if b in CRITICAL_SHELL_MARKERS:
            return PermissionLevel.CRITICAL
        if b in HIGH_RISK_SHELL_COMMANDS:
            return PermissionLevel.HIGH

    binary_base = Path(tokens[0]).name.lower() if tokens else ""
    if binary_base in HIGH_RISK_SHELL_COMMANDS:
        return PermissionLevel.HIGH

    return PermissionLevel.MEDIUM

def permission_for_tool(tool_name: str, arguments: dict) -> PermissionLevel | None:
    level = TOOL_PERMISSIONS.get(tool_name)
    if not isinstance(level, PermissionLevel):
        return None

    if tool_name == "execute_shell":
        command = arguments.get("command")
        if not isinstance(command, str):
            return None
        return shell_command_level(command)

    if tool_name == "hyprland_exec":
        command = arguments.get("command")
        if not isinstance(command, str):
            return None
        return hyprland_exec_level(command)

    if tool_name == "package_manager":
        action = arguments.get("action")
        if action == "search":
            return PermissionLevel.READ
        if action == "remove":
            return PermissionLevel.HIGH
        return PermissionLevel.MEDIUM

    if tool_name == "process_manager":
        if arguments.get("action") in ("list", "search"):
            return PermissionLevel.READ
        return PermissionLevel.HIGH

    if tool_name == "clipboard_control":
        # Чтение и запись буфера обмена требуют подтверждения MEDIUM, чтобы не утекали пароли
        return PermissionLevel.MEDIUM

    if tool_name == "system_volume" and arguments.get("action") == "get":
        return PermissionLevel.READ

    return level

HOME_DIR = str(Path.home())

SYSTEM_PROMPT = f"""Ты — PAI (Personal AI Environment), персональный AI-агент пользователя на Arch Linux + Hyprland.

Твоя задача — самостоятельно и безотказно выполнять задачи пользователя через специализированные инструменты.

ОБЛАСТИ:
- PAIWorkspace = {HOME_DIR}/PAIWorkspace. Рабочие файлы пользователя.
- Проект PAI = {HOME_DIR}/pai. Исходный код агента и его проект.
- Домашняя директория пользователя = {HOME_DIR} (~).

ПРАВИЛА ВЫБОРА ИНСТРУМЕНТОВ:
1. ЗАПУСК ПРИЛОЖЕНИЙ: Если просят открыть обычное приложение («открой телеграм», «запусти дискорд», «открой браузер на 2 столе»), ВСЕГДА используй `app_launcher`. Не пытайся угадывать бинарник через `hyprland_exec`.
2. ТЕРМИНАЛ И КОМАНДЫ В ОКНЕ: Для запуска терминала kitty с командой (например `kitty --hold fish -c 'fastfetch'`) используй `hyprland_exec`.
3. УПРАВЛЕНИЕ ОКНАМИ: Для закрытия, перемещения между рабочими столами, разворачивания во весь экран или сворачивания окон используй `hyprland_window_control`.
4. МУЗЫКА: Для любого управления Яндекс Музыкой (включить, пауза, следующий трек, лайк, просто открыть) ВСЕГДА используй ТОЛЬКО инструмент `yandex_music_control`. Не используй `playerctl` или `execute_shell`.
5. ПАКЕТЫ: Для поиска, установки и удаления пакетов Arch/AUR всегда используй `package_manager`.
6. СИСТЕМА И ЭКРАН:
   - Скриншот экрана в Telegram → `take_screenshot`
   - Громкость / Mute → `system_volume`
   - Буфер обмена → `clipboard_control`
   - Список процессов или убийство зависшего процесса → `process_manager`

ПРАВИЛО ОСТАНОВКИ:
- Никогда не вызывай инструменты больше 2-3 раз подряд, если команда вернула ошибку. Сразу сообщи результат пользователю.
- Если пользователь отклонил действие (Операция отменена пользователем), немедленно прекрати попытки выполнить его.

РАБОТА С КОДОМ:
- Перед изменением файла сначала прочитай его.
- После изменения Python-кода используй `check_syntax`.
"""

def confirmation_text(tool_name: str, arguments: dict) -> str:
    descriptions = {
        "write_file": lambda a: f"записать файл PAIWorkspace/{a.get('path', '')}",
        "delete_file": lambda a: f"удалить файл PAIWorkspace/{a.get('path', '')}",
        "move_file": lambda a: f"переместить файл PAIWorkspace/{a.get('source', '')} → PAIWorkspace/{a.get('destination', '')}",
        "write_project_file": lambda a: f"изменить файл проекта {a.get('path', '')}",
        "edit_project_file": lambda a: f"изменить файл проекта {a.get('path', '')}",
        "edit_source": lambda a: f"изменить исходный код {Path(__file__).name}",
        "hyprland_switch_workspace": lambda a: f"переключить Hyprland на рабочий стол {a.get('workspace', '')}",
        "package_manager": lambda a: f"выполнить yay ({a.get('action')}) для пакета: {a.get('package', '')}",
        "process_manager": lambda a: f"завершить процесс: {a.get('query', '')}",
        "execute_shell": lambda a: f"выполнить shell-команду: {a.get('command', '')}",
        "hyprland_exec": lambda a: f"запустить в Hyprland: {a.get('command', '')}",
        "clipboard_control": lambda a: f"доступ к буферу обмена ({a.get('action', '')})",
    }
    formatter = descriptions.get(tool_name)
    return formatter(arguments) if formatter else f"выполнить инструмент {tool_name}"


def confirm_action(
    tool_name: str, arguments: dict, level: PermissionLevel = PermissionLevel.MEDIUM
) -> bool:
    if CONFIRM_CALLBACK is not None:
        return CONFIRM_CALLBACK(tool_name, arguments, level)

    action = confirmation_text(tool_name, arguments)
    consequences = {
        PermissionLevel.MEDIUM: "Это изменит данные или пакеты в системе.",
        PermissionLevel.HIGH: "Это может удалить данные, пакеты или изменить процессы.",
        PermissionLevel.CRITICAL: "Это критическая операция с возможным системным или необратимым эффектом.",
    }[level]
    answer = input(
        f"\nPAI хочет [{level.value}]: {action}\n{consequences}\n"
        "Разрешить? [y/N] "
    )
    return answer.strip().lower() in ("y", "yes", "д", "да")


def critical_shell_command_is_allowed(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return (
        bool(tokens)
        and Path(tokens[0]).name.lower() in ALLOWED_CRITICAL_SHELL_COMMANDS
        and not any(operator in command for operator in SHELL_OPERATORS)
    )


def authorize_tool(tool_name: str, arguments: dict) -> tuple[bool, PermissionLevel | None, str | None]:
    level = permission_for_tool(tool_name, arguments)

    if level is None:
        return False, None, "Ошибка: инструмент не имеет известного permission level."

    if level in {PermissionLevel.READ, PermissionLevel.LOW}:
        return True, level, None

    if level == PermissionLevel.CRITICAL:
        command = arguments.get("command", "")
        if tool_name != "execute_shell" or not critical_shell_command_is_allowed(command):
            return False, level, "Ошибка: критическая операция не разрешена политикой."

    if confirm_action(tool_name, arguments, level):
        return True, level, None

    return False, level, "Операция отменена пользователем."


TOOL_DISPATCH: dict[str, Callable[[dict], str]] = {
    "list_directory": lambda a: list_directory(a["path"]),
    "create_directory": lambda a: create_directory(a["path"]),
    "read_file": lambda a: read_file(a["path"]),
    "write_file": lambda a: write_file(a["path"], a["content"]),
    "delete_file": lambda a: delete_file(a["path"]),
    "move_file": lambda a: move_file(a["source"], a["destination"]),
    "list_project_directory": lambda a: list_project_directory(a.get("path", ".")),
    "read_project_file": lambda a: read_project_file(a["path"]),
    "write_project_file": lambda a: write_project_file(a["path"], a["content"]),
    "read_source": lambda a: read_source(a.get("path", Path(__file__).name)),
    "edit_project_file": lambda a: edit_project_file(a["path"], a["old_text"], a["new_text"]),
    "edit_source": lambda a: edit_source(a["old_text"], a["new_text"]),
    "check_syntax": lambda a: check_syntax(a["path"]),
    "git_diff": lambda a: git_diff(a.get("path", ".")),
    "run_tests": lambda a: run_tests(),
    "app_launcher": lambda a: app_launcher(a["app_name"], a.get("workspace")),
    "package_manager": lambda a: package_manager(a["action"], a["package"]),
    "hyprland_get_clients": lambda a: hyprland_get_clients(),
    "hyprland_get_workspaces": lambda a: hyprland_get_workspaces(),
    "hyprland_switch_workspace": lambda a: hyprland_switch_workspace(a["workspace"]),
    "hyprland_window_control": lambda a: hyprland_window_control(
        a["action"], a.get("target"), a.get("workspace")
    ),
    "hyprland_exec": lambda a: hyprland_exec(a["command"], a.get("workspace")),
    "take_screenshot": lambda a: take_screenshot(a.get("filename", "pai_screenshot.png")),
    "system_volume": lambda a: system_volume(a["action"], a.get("value")),
    "clipboard_control": lambda a: clipboard_control(a["action"], a.get("text")),
    "process_manager": lambda a: process_manager(a["action"], a.get("query")),
    "yandex_music_control": lambda a: yandex_music_control(
        a.get("action", "play_pause"), a.get("workspace")
    ),
    "execute_shell": lambda a: execute_shell(a["command"]),
}


def call_tool(tool_name: str, arguments: dict) -> str:
    handler = TOOL_DISPATCH.get(tool_name)
    if handler is None:
        return f"Ошибка: неизвестный инструмент: {tool_name}"
    return handler(arguments)


MAX_AGENT_STEPS = 30
RESTART_STATE_FILE = PAI_ROOT / ".pai_restart_state.json"
MAX_CONSECUTIVE_SELF_RESTARTS = 3
MAX_FIX_CYCLES = 3


class WorkflowPhase(str, Enum):
    PLAN = "PLAN"
    EXECUTE = "EXECUTE"
    VERIFY = "VERIFY"
    FIX = "FIX"
    DONE = "DONE"


class StepStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"


@dataclass
class PlanStep:
    description: str
    status: StepStatus = StepStatus.PENDING


@dataclass
class TaskState:
    goal: str
    phase: WorkflowPhase = WorkflowPhase.PLAN
    plan: list[PlanStep] = field(default_factory=list)
    fix_cycles: int = 0
    requires_verification: bool = False
    self_update_pending: bool = False
    self_update_validated: bool = False

    @classmethod
    def for_goal(cls, goal: str) -> "TaskState":
        return cls(
            goal=goal,
            plan=[
                PlanStep("Изучить нужное состояние и составить краткий план"),
                PlanStep("Выполнить необходимые действия через инструменты"),
                PlanStep("Проверить результат подходящими инструментами"),
            ],
        )

    def activate_next(self) -> None:
        for step in self.plan:
            if step.status == StepStatus.PENDING:
                step.status = StepStatus.ACTIVE
                return

    def finish_active(self) -> None:
        for step in self.plan:
            if step.status == StepStatus.ACTIVE:
                step.status = StepStatus.DONE
                return

    def fail_active(self) -> None:
        for step in self.plan:
            if step.status == StepStatus.ACTIVE:
                step.status = StepStatus.FAILED
                return

    def add_fix_step(self, reason: str) -> None:
        self.plan.append(
            PlanStep(f"Исправить проблему: {reason[:160]}", StepStatus.ACTIVE)
        )

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "phase": self.phase.value,
            "plan": [
                {"description": step.description, "status": step.status.value}
                for step in self.plan
            ],
            "fix_cycles": self.fix_cycles,
            "requires_verification": self.requires_verification,
            "self_update_pending": self.self_update_pending,
            "self_update_validated": self.self_update_validated,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskState":
        return cls(
            goal=data["goal"],
            phase=WorkflowPhase(data["phase"]),
            plan=[
                PlanStep(item["description"], StepStatus(item["status"]))
                for item in data["plan"]
            ],
            fix_cycles=data.get("fix_cycles", 0),
            requires_verification=data.get("requires_verification", False),
            self_update_pending=data.get("self_update_pending", False),
            self_update_validated=data.get("self_update_validated", False),
        )

    def summary(self) -> str:
        steps = "; ".join(f"{step.status.value}: {step.description}" for step in self.plan)
        update_state = "pending" if self.self_update_pending else "none"
        return (
            f"Workflow={self.phase.value}; fixes={self.fix_cycles}/{MAX_FIX_CYCLES}; "
            f"self-update={update_state}; plan={steps}"
        )


def set_workflow_phase(task_state: TaskState, new_phase: WorkflowPhase) -> None:
    old_phase = task_state.phase
    if old_phase == new_phase:
        return
    task_state.phase = new_phase
    print(f"[{new_phase.value}] {old_phase.value} → {new_phase.value}")


def log_workflow_tool(
    phase: WorkflowPhase,
    tool_name: str,
    result: str,
    level: PermissionLevel | None = None,
    confirmation_requested: bool = False,
) -> None:
    permission = f" [{level.value}]" if level else ""
    if confirmation_requested:
        print(f"[{phase.value}] {tool_name}{permission} → CONFIRM")
    if tool_succeeded(result):
        print(f"[{phase.value}] {tool_name}{permission} → OK")
    else:
        print(f"[FIX] {tool_name}{permission} → ERROR: инструмент вернул ошибку")


def needs_workflow(user_input: str) -> bool:
    lowered = user_input.lower()
    signals = ("измен", "создай", "удали", "перемест", "исправ", "реализ", "проверь", "тест", "файл", "проект")
    return len(user_input) > 120 or any(signal in lowered for signal in signals)


def tool_succeeded(result: str) -> bool:
    return not result.startswith("Ошибка") and "отменена пользователем" not in result


def source_fingerprint() -> tuple[int, int] | None:
    try:
        stat = Path(__file__).resolve().stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def is_current_source(path: str) -> bool:
    target, error = resolve_inside(PAI_ROOT, path)
    return error is None and target == Path(__file__).resolve()


def project_has_tests() -> bool:
    ignored_parts = {".git", ".venv", "__pycache__", ".pytest_cache"}
    return any(
        candidate.is_file() and candidate.name.startswith("test")
        for candidate in PAI_ROOT.rglob("test*.py")
        if not ignored_parts.intersection(candidate.relative_to(PAI_ROOT).parts)
    )


def validate_self_update() -> tuple[bool, str]:
    syntax_result = check_syntax(Path(__file__).name)
    if not syntax_result.startswith("Синтаксис "):
        return False, f"Самообновление отменено: {syntax_result}"

    if project_has_tests():
        tests_result = run_tests()
        if not tests_result.startswith("Код завершения: 0"):
            return False, f"Самообновление отменено: тесты не прошли.\n{tests_result}"

    return True, "Проверки самообновления успешно пройдены."


def save_restart_state(
    messages: list[dict], restart_count: int, task_state: TaskState | None = None
) -> None:
    payload = {
        "version": 2,
        "restart_count": restart_count,
        "messages": messages,
        "task_state": task_state.to_dict() if task_state else None,
    }
    fd, temporary_name = tempfile.mkstemp(
        prefix=f"{RESTART_STATE_FILE.name}.",
        dir=str(RESTART_STATE_FILE.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
            json.dump(payload, temporary_file, ensure_ascii=False)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, RESTART_STATE_FILE)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def load_restart_state() -> tuple[list[dict], int, TaskState | None] | None:
    if not RESTART_STATE_FILE.exists():
        return None
    try:
        payload = json.loads(RESTART_STATE_FILE.read_text(encoding="utf-8"))
        messages = payload["messages"]
        restart_count = payload["restart_count"]
        raw_task_state = payload.get("task_state")
        task_state = TaskState.from_dict(raw_task_state) if raw_task_state else None
        if not isinstance(messages, list) or not isinstance(restart_count, int):
            raise ValueError("некорректная структура")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        print(f"PAI > Не удалось восстановить состояние самообновления: {e}")
        RESTART_STATE_FILE.unlink(missing_ok=True)
        return None

    RESTART_STATE_FILE.unlink(missing_ok=True)
    return messages, restart_count, task_state


def restart_with_updated_source(
    messages: list[dict], restart_count: int, task_state: TaskState | None = None
) -> None:
    if CONFIRM_CALLBACK is not None:
        print("PAI > Изменения кода сохранены (перезапуск процесса пропущен в режиме бота).")
        if task_state:
            task_state.self_update_pending = False
            task_state.self_update_validated = False
        return

    if restart_count >= MAX_CONSECUTIVE_SELF_RESTARTS:
        print(
            "PAI > Самообновление применено, но перезапуск отменён: "
            f"достигнут лимит {MAX_CONSECUTIVE_SELF_RESTARTS} последовательных перезапусков."
        )
        return

    if task_state:
        task_state.self_update_pending = False
        task_state.self_update_validated = False
    save_restart_state(messages, restart_count + 1, task_state)
    print("PAI > Самообновление проверено. Перезапуск новой версии PAI...")
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])


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

    print("[COMPACT] Контекст заполнен на ~70%. Запускаю умное сжатие...")

    system_msg = messages[0]
    body = messages[1:]

    if len(body) <= 8:
        return messages

    # Оставляем последние 8 сообщений нетронутыми, чтобы не разрывать цепочки инструмент-ответ
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
                        "Обязательно сохрани: имена файлов, найденные функции/строки, "
                        "внесённые изменения и текущие ошибки."
                    ),
                },
                {"role": "user", "content": json.dumps(middle_chunk, ensure_ascii=False)[:40000]},
            ],
            reasoning_effort="none",
        )
        summary_text = summary_resp.choices[0].message.content or "История сжата."
    except Exception:
        summary_text = "Предыдущие промежуточные шаги были очищены из-за лимита контекста."

    compacted = [
        system_msg,
        {
            "role": "user",
            "content": f"[СВОДКА ПРЕДЫДУЩИХ ШАГОВ АГЕНТА]:\n{summary_text}",
        },
    ]
    compacted.extend(tail_chunk)
    print(f"[COMPACT] Суммаризация завершена: сообщений было {len(messages)}, стало {len(compacted)}.")
    return compacted

def run_agent_turn(
    messages: list[dict],
    restart_count: int = 0,
    task_state: TaskState | None = None,
) -> TaskState | None:
    global LAST_REPLY
    LAST_REPLY = ""

    if task_state and task_state.phase == WorkflowPhase.PLAN:
        print(f"[PLAN] {WorkflowPhase.PLAN.value} → {WorkflowPhase.PLAN.value}")
        task_state.activate_next()
        task_state.finish_active()
        set_workflow_phase(task_state, WorkflowPhase.EXECUTE)
        task_state.activate_next()

    for step in range(1, MAX_AGENT_STEPS + 1):
        if CANCEL_EVENT.is_set():
            LAST_REPLY = "🛑 Выполнение прервано пользователем."
            print(f"PAI > {LAST_REPLY}")
            return task_state
        messages[:] = compact_messages(messages)

        if os.environ.get("PAI_DEBUG"):
            print(f"\n[DEBUG] messages: {len(messages)}")
            print(f"[DEBUG] chars: {estimate_chars(messages)}")

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
            reasoning_effort="none",
        )

        if CANCEL_EVENT.is_set():
            LAST_REPLY = "🛑 Выполнение прервано пользователем."
            print(f"PAI > {LAST_REPLY}")
            return task_state

        message = response.choices[0].message
        assistant_message = {
            "role": "assistant",
            "content": message.content or "",
        }

        if message.tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in message.tool_calls
            ]

        messages.append(assistant_message)

        if not message.tool_calls:
            # Если код проекта менялся, но агент забыл запустить check_syntax — заставляем проверить
            if task_state and task_state.requires_verification and task_state.fix_cycles < MAX_FIX_CYCLES:
                set_workflow_phase(task_state, WorkflowPhase.VERIFY)
                task_state.requires_verification = False
                messages.append({
                    "role": "user",
                    "content": "[SYSTEM]: Ты изменил файлы, но не проверил результат. Выполни check_syntax или чтение файла перед завершением.",
                })
                continue

            if task_state and task_state.self_update_pending and task_state.self_update_validated:
                restart_with_updated_source(messages, restart_count, task_state)
            LAST_REPLY = message.content or "(пустой ответ)"
            print(f"PAI > {LAST_REPLY}")
            return task_state

        batch_failed = False
        verification_ran = False
        changed_something = False
        cancelled_by_user = False

        for tool_call in message.tool_calls:
            if cancelled_by_user or CANCEL_EVENT.is_set():
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": "Операция отменена пользователем.",
                    }
                )
                continue

            tool_name = tool_call.function.name
            source_before = source_fingerprint()

            level: PermissionLevel | None = None
            confirmation_requested = False
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError as e:
                result = f"Ошибка разбора аргументов инструмента: {e}"
                arguments = {}
            else:
                allowed, level, denial_reason = authorize_tool(tool_name, arguments)
                confirmation_requested = level in {
                    PermissionLevel.MEDIUM,
                    PermissionLevel.HIGH,
                    PermissionLevel.CRITICAL,
                }
                if not allowed:
                    result = denial_reason or "Ошибка: выполнение инструмента отклонено политикой permissions."
                else:
                    try:
                        result = call_tool(tool_name, arguments)
                    except KeyError as e:
                        result = f"Ошибка: отсутствует обязательный аргумент {e}."
                    except Exception as e:
                        result = f"Ошибка инструмента {tool_name}: {e}"

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
            )
            if result == "Операция отменена пользователем.":
                cancelled_by_user = True

            if task_state:
                log_workflow_tool(
                    task_state.phase,
                    tool_name,
                    result,
                    level,
                    confirmation_requested,
                )
            if not tool_succeeded(result):
                batch_failed = True
            if tool_name in {"check_syntax", "run_tests", "git_diff", "read_file", "read_project_file"}:
                verification_ran = True
            if tool_name in {"write_project_file", "edit_project_file", "edit_source"}:
                if task_state and tool_succeeded(result):
                    changed_something = True
                    task_state.requires_verification = True
            if (
                tool_name in {"edit_source", "edit_project_file", "write_project_file"}
                and is_current_source(arguments.get("path", Path(__file__).name))
                and source_before != source_fingerprint()
                and result.startswith("Файл")
            ):
                if task_state:
                    task_state.self_update_pending = True
                    task_state.self_update_validated = False

        if cancelled_by_user or CANCEL_EVENT.is_set():
            LAST_REPLY = "🛑 Действие остановлено пользователем."
            print(f"PAI > {LAST_REPLY}")
            return task_state

        if task_state:
            if batch_failed:
                task_state.fail_active()
                task_state.fix_cycles += 1
                if task_state.fix_cycles > MAX_FIX_CYCLES:
                    set_workflow_phase(task_state, WorkflowPhase.DONE)
                    task_state.add_fix_step("превышен лимит циклов исправления")
                    task_state.fail_active()
                else:
                    set_workflow_phase(task_state, WorkflowPhase.FIX)
                    task_state.add_fix_step("ошибка последнего действия")
            elif changed_something and task_state.phase != WorkflowPhase.VERIFY:
                task_state.finish_active()
                set_workflow_phase(task_state, WorkflowPhase.VERIFY)
                task_state.activate_next()
            elif task_state.phase == WorkflowPhase.VERIFY and verification_ran:
                task_state.finish_active()
                task_state.requires_verification = False
                if task_state.self_update_pending:
                    valid, validation_result = validate_self_update()
                    print(f"PAI > {validation_result}")
                    task_state.self_update_validated = valid
                    if not valid:
                        task_state.fix_cycles += 1
                        set_workflow_phase(task_state, WorkflowPhase.FIX)
                        task_state.add_fix_step("проверки самообновления не прошли")
                    else:
                        set_workflow_phase(task_state, WorkflowPhase.DONE)
                else:
                    set_workflow_phase(task_state, WorkflowPhase.DONE)

    LAST_REPLY = (
        f"Достигнут лимит {MAX_AGENT_STEPS} шагов агента. "
        "Остановка выполнена для защиты от бесконечного цикла."
    )
    print(f"PAI > {LAST_REPLY}")
    return task_state


def main() -> None:
    print("PAI запущен.")
    print("Для выхода: exit")
    print(f"Workspace: {WORKSPACE}")
    print(f"Model: {MODEL}")
    print(f"Agent steps limit: {MAX_AGENT_STEPS}")
    print()

    restored_state = load_restart_state()
    if restored_state is None:
        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            }
        ]
        restart_count = 0
        task_state = None
    else:
        messages, restart_count, task_state = restored_state
        print("PAI > Новая версия запущена; продолжаю исходную задачу.")
        if task_state:
            print(f"[{task_state.phase.value}] RESTORED → {task_state.phase.value}")

    if restored_state is not None:
        try:
            task_state = run_agent_turn(messages, restart_count, task_state)
        except Exception as e:
            print(f"PAI > Ошибка связи с моделью или API: {e}")

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

        messages.append(
            {
                "role": "user",
                "content": user_input,
            }
        )
        CANCEL_EVENT.clear()
        task_state = TaskState.for_goal(user_input) if needs_workflow(user_input) else None
        if task_state:
            print(f"PAI > Создан план: {task_state.summary()}")

        try:
            task_state = run_agent_turn(messages, restart_count, task_state)
        except Exception as e:
            print(f"PAI > Ошибка связи с моделью или API: {e}")


if __name__ == "__main__":
    main()
