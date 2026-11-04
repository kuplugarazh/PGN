import json
import os
import py_compile
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from openai import OpenAI


MODEL ="Qwen3-8B-Q4_K_M.gguf"

PAI_ROOT = Path.home() / "pai"
WORKSPACE = Path.home() / "PAIWorkspace"


client = OpenAI(
    base_url="http://127.0.0.1:8080/v1",
    api_key="local",
)


def resolve_inside(root: Path, path: str) -> tuple[Path | None, str | None]:
    """Resolve path and make sure it stays inside root."""
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
            # Если целевой путь — директория, перемещаем файл внутрь неё с тем же именем
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


def read_source(path: str = "pai.py") -> str:
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
    """Backward-compatible wrapper for editing pai.py."""
    return edit_project_file("pai.py", old_text, new_text)


def check_syntax(path: str = "pai.py") -> str:
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
# Hyprland read-only backend
# =========================

class HyprlandBackend:
    """Small read-only adapter for Hyprland's JSON IPC via hyprctl."""

    def _require_session(self) -> str | None:
        # Hyprland exports this per-instance socket identifier to its clients.
        # Without it, hyprctl could target no compositor or an unrelated session.
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

    @staticmethod
    def normalize_client(client: object) -> dict:
        """Keep the stable client fields and any additional Hyprland fields."""
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
        """Keep the stable workspace fields and any additional Hyprland fields."""
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
        """Accept only a positive numeric workspace or a safe workspace name."""
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

        session_error = self._require_session()
        if session_error:
            return session_error

        try:
            process = subprocess.run(
                ["hyprctl", "dispatch", "workspace", target],
                capture_output=True,
                text=True,
                timeout=5,
                shell=False,
            )
        except FileNotFoundError:
            return "Ошибка Hyprland: утилита hyprctl не найдена."
        except subprocess.TimeoutExpired:
            return "Ошибка Hyprland: переключение рабочего стола превысило лимит времени 5 секунд."
        except OSError as e:
            return f"Ошибка Hyprland: не удалось запустить hyprctl: {e}"

        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip() or f"код {process.returncode}"
            return f"Ошибка Hyprland: не удалось переключиться на рабочий стол {target}: {detail}"
        return f"Переключение на рабочий стол {target} выполнено."


hyprland_backend = HyprlandBackend()


def hyprland_get_clients() -> str:
    """Return structured information about currently mapped Hyprland clients."""
    return hyprland_backend.get_clients()


def hyprland_get_workspaces() -> str:
    """Return structured information about current Hyprland workspaces."""
    return hyprland_backend.get_workspaces()


def hyprland_switch_workspace(workspace: object) -> str:
    """Switch the active Hyprland workspace after central HIGH authorization."""
    return hyprland_backend.switch_workspace(workspace)


# =========================
# System tools
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
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Имя или относительный путь новой директории внутри PAIWorkspace.",
                    }
                },
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
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Имя или относительный путь к файлу внутри PAIWorkspace.",
                    }
                },
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
                "properties": {
                    "path": {"type": "string"},
                },
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
                    "source": {
                        "type": "string",
                        "description": "Исходный путь файла относительно PAIWorkspace.",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Целевой путь относительно PAIWorkspace. Может быть именем файла или директорией.",
                    },
                },
                "required": ["source", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_project_directory",
            "description": "Показать содержимое директории внутри проекта PAI (~ /pai).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь относительно корня проекта PAI.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_project_file",
            "description": "Прочитать UTF-8 файл внутри проекта PAI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
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
            "name": "read_source",
            "description": "Прочитать Python или другой UTF-8 исходный файл проекта PAI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь относительно ~/pai, например pai.py или src/agent.py.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_project_file",
            "description": "Точно изменить один файл проекта PAI: заменить ровно одно совпадение old_text на new_text. Перед изменением сначала прочитай файл.",
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
            "name": "edit_source",
            "description": "Устаревший алиас: изменить pai.py. Предпочитай edit_project_file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_syntax",
            "description": "Проверить синтаксис любого Python-файла внутри проекта PAI. Это безопасный специализированный инструмент; не используй execute_shell для py_compile.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь относительно ~/pai, например pai.py или pai_v3.py.",
                    }
                },
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
                "properties": {
                    "path": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Запустить pytest в проекте PAI.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hyprland_get_clients",
            "description": "Получить структурированный список текущих окон Hyprland (address, class, title, workspace, pid, mapped/focused и доступные поля). Работает только внутри Hyprland-сессии.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hyprland_get_workspaces",
            "description": "Получить структурированный список рабочих столов Hyprland (id, name, windows, focused и доступные поля). Работает только внутри Hyprland-сессии.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hyprland_switch_workspace",
            "description": "Переключить текущий рабочий стол Hyprland по положительному номеру или безопасному имени. Изменяет состояние и требует подтверждения.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace": {
                        "description": "Положительный номер рабочего стола или его имя из букв, цифр, _, -, . и :.",
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
            "name": "execute_shell",
            "description": "Выполнить shell-команду в Linux. Используй только если нет специализированного инструмента.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
]


class PermissionLevel(str, Enum):
    """Risk levels applied centrally before every tool invocation."""

    READ = "READ"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# Every declared tool has a baseline level. execute_shell is refined from its
# arguments by permission_for_tool() before it can run.
TOOL_PERMISSIONS = {
    "list_directory": PermissionLevel.READ,
    "create_directory": PermissionLevel.LOW,
    "read_file": PermissionLevel.READ,
    "write_file": PermissionLevel.MEDIUM,
    "delete_file": PermissionLevel.HIGH,
    "move_file": PermissionLevel.HIGH,
    "list_project_directory": PermissionLevel.READ,
    "read_project_file": PermissionLevel.READ,
    "write_project_file": PermissionLevel.HIGH,
    "read_source": PermissionLevel.READ,
    "edit_source": PermissionLevel.HIGH,
    "edit_project_file": PermissionLevel.HIGH,
    "check_syntax": PermissionLevel.READ,
    "git_diff": PermissionLevel.READ,
    "run_tests": PermissionLevel.READ,
    "hyprland_get_clients": PermissionLevel.READ,
    "hyprland_get_workspaces": PermissionLevel.READ,
    "hyprland_switch_workspace": PermissionLevel.HIGH,
    "execute_shell": PermissionLevel.HIGH,
}


READ_ONLY_SHELL_COMMANDS = {
    "cat", "diff", "find", "git", "grep", "head", "ls", "printf", "pwd",
    "rg", "sed", "stat", "tail", "wc", "which",
}
LOW_RISK_SHELL_COMMANDS = {"echo", "mkdir", "touch"}
HIGH_RISK_SHELL_COMMANDS = {
    "chown", "chmod", "kill", "killall", "pkill", "renice", "rm", "rmdir",
    "systemctl", "service", "docker", "podman", "mv", "cp", "tee",
}
CRITICAL_SHELL_MARKERS = {
    "sudo", "su", "doas", "pkexec", "shutdown", "reboot", "poweroff", "halt",
    "mkfs", "fdisk", "parted", "mount", "umount", "dd", "wipefs", "useradd",
    "userdel", "usermod", "passwd", "visudo", "iptables", "nft", "setcap",
}
SYSTEM_PATH_PREFIXES = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/boot", "/proc", "/sys", "/dev", "/root")
# These critical system commands may run only after a confirmation immediately
# before call_tool(). All other CRITICAL classifications remain denied.
ALLOWED_CRITICAL_SHELL_COMMANDS = {"shutdown", "reboot", "poweroff", "halt"}


def shell_command_level(command: str) -> PermissionLevel:
    """Classify shell text conservatively; ambiguous syntax is never low risk."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return PermissionLevel.HIGH
    if not tokens:
        return PermissionLevel.HIGH

    executable = Path(tokens[0]).name.lower()
    if executable in CRITICAL_SHELL_MARKERS:
        return PermissionLevel.CRITICAL
    if any(token.startswith(SYSTEM_PATH_PREFIXES) for token in tokens):
        return PermissionLevel.CRITICAL
    if any(operator in command for operator in (";", "&&", "||", "|", "`", "$(", ">", "<")):
        return PermissionLevel.HIGH

    if executable == "git":
        subcommand = next((token for token in tokens[1:] if not token.startswith("-")), "")
        return (
            PermissionLevel.READ
            if subcommand in {"diff", "log", "show", "status"}
            else PermissionLevel.HIGH
        )
    if executable == "sed" and "-i" in tokens:
        return PermissionLevel.HIGH
    if executable == "find" and any(
        token in {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
        for token in tokens[1:]
    ):
        return PermissionLevel.HIGH
    if executable in READ_ONLY_SHELL_COMMANDS:
        return PermissionLevel.READ
    if executable in LOW_RISK_SHELL_COMMANDS:
        return PermissionLevel.LOW
    if executable in HIGH_RISK_SHELL_COMMANDS:
        return PermissionLevel.HIGH
    return PermissionLevel.HIGH


def permission_for_tool(tool_name: str, arguments: dict) -> PermissionLevel | None:
    """Return the effective level, refusing unknown tools and malformed levels."""
    level = TOOL_PERMISSIONS.get(tool_name)
    if not isinstance(level, PermissionLevel):
        return None
    if tool_name == "execute_shell":
        command = arguments.get("command")
        if not isinstance(command, str):
            return None
        return shell_command_level(command)
    return level


SYSTEM_PROMPT = """Ты — PAI (Personal AI Environment), персональный AI-агент пользователя.

Твоя главная задача — не просто отвечать на вопросы, а самостоятельно выполнять поставленные пользователем задачи на Linux-системе через доступные инструменты.

АРХИТЕКТУРА:
Ты работаешь итеративно:
1. Понимаешь цель пользователя.
2. Сам определяешь, какие файлы и состояние системы нужно изучить.
3. Составляешь рабочий план внутри текущего ответа.
4. Вызываешь нужные инструменты.
5. Анализируешь результаты.
6. Если обнаружена ошибка — меняешь план и исправляешь её.
7. После изменений проверяешь результат.
8. Продолжаешь цикл до выполнения задачи или до объективной блокировки.
9. Только после этого даёшь пользователю итог.

НЕ ПЕРЕКЛАДЫВАЙ РАБОТУ НА ПОЛЬЗОВАТЕЛЯ. Если задачу можно выполнить имеющимися инструментами, выполняй её сам. Не проси пользователя вручную запускать команды, читать файлы или указывать очевидный следующий шаг.

ОБЛАСТИ:
- PAIWorkspace = ~/PAIWorkspace. Рабочие файлы пользователя.
- Проект PAI = ~/pai. Исходный код агента и его проект.
- Никогда не путай эти две области.

РАБОТА С КОДОМ:
- Перед изменением файла сначала прочитай его.
- Для изменения одного существующего фрагмента используй edit_project_file.
- Для создания/полной перезаписи нового файла используй write_project_file.
- После изменения Python-кода используй check_syntax с реальным путём изменённого файла.
- После успешной проверки запускай run_tests, если тестовая инфраструктура существует или это уместно.
- Используй git_diff после изменений, чтобы проверить фактический результат.
- Если проверка или тесты нашли ошибку, проанализируй её и продолжай исправление самостоятельно.
- Не останавливайся после первой успешной записи файла, если задача пользователя подразумевает рабочую реализацию.
- Для задачи по развитию самого PAI можешь изменять pai.py и другие файлы проекта PAI, но соблюдай подтверждения.

ИНСТРУМЕНТЫ:
- Используй специализированный инструмент, если он существует.
- Не используй execute_shell для операций, для которых есть специализированный инструмент.
- В частности, для проверки Python используй check_syntax, а не `python -m py_compile` через shell.
- execute_shell оставлен для действий, для которых отдельного инструмента пока нет.

ПОДТВЕРЖДЕНИЯ:
- Если инструмент требует подтверждения, запроси его обычным способом.
- Никогда не обходи подтверждение через другой инструмент.
- После отказа пользователя не выполняй запрещённую операцию другим способом.

ПРАВДА О РЕЗУЛЬТАТЕ:
- Никогда не говори, что действие выполнено, если инструмент не подтверждает успех.
- Если инструмент вернул ошибку, считай действие невыполненным.
- Если после разумных попыток задача заблокирована, объясни конкретную причину и что именно уже было сделано.

ПЛАН:
Для многошаговых задач держи план в контексте разговора и обновляй его по мере появления результатов. Не спрашивай разрешение на каждый безопасный шаг — выполняй безопасные шаги самостоятельно.
"""



def confirmation_text(tool_name: str, arguments: dict) -> str:
    if tool_name == "write_file":
        return f"записать файл PAIWorkspace/{arguments.get('path', '')}"
    if tool_name == "delete_file":
        return f"удалить файл PAIWorkspace/{arguments.get('path', '')}"
    if tool_name == "move_file":
        return f"переместить файл PAIWorkspace/{arguments.get('source', '')} → PAIWorkspace/{arguments.get('destination', '')}"
    if tool_name == "write_project_file":
        return f"изменить файл проекта {arguments.get('path', '')}"
    if tool_name == "edit_project_file":
        return f"изменить файл проекта {arguments.get('path', '')}"
    if tool_name == "edit_source":
        return "изменить исходный код pai.py"
    if tool_name == "hyprland_switch_workspace":
        return f"переключить Hyprland на рабочий стол {arguments.get('workspace', '')}"
    if tool_name == "execute_shell":
        return f"выполнить shell-команду: {arguments.get('command', '')}"
    return f"выполнить инструмент {tool_name}"


def confirm_action(
    tool_name: str, arguments: dict, level: PermissionLevel = PermissionLevel.MEDIUM
) -> bool:
    action = confirmation_text(tool_name, arguments)
    consequences = {
        PermissionLevel.MEDIUM: "Это изменит данные в разрешённой области.",
        PermissionLevel.HIGH: "Это может удалить данные, изменить процессы или затронуть широкое состояние системы.",
        PermissionLevel.CRITICAL: "Это критическая операция с возможным системным или необратимым эффектом.",
    }[level]
    answer = input(
        f"\nPAI хочет [{level.value}]: {action}\n{consequences}\n"
        "Разрешить? [y/N] "
    )
    return answer.strip().lower() in ("y", "yes", "д", "да")


def critical_shell_command_is_allowed(command: str) -> bool:
    """Allow only supported critical system executables, without shell syntax."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return (
        bool(tokens)
        and Path(tokens[0]).name.lower() in ALLOWED_CRITICAL_SHELL_COMMANDS
        and not any(operator in command for operator in (";", "&&", "||", "|", "`", "$(", ">", "<"))
    )


def authorize_tool(tool_name: str, arguments: dict) -> tuple[bool, PermissionLevel | None, str | None]:
    """Central policy gate used by every tool call, regardless of its origin."""
    level = permission_for_tool(tool_name, arguments)

    if level is None:
        return False, None, "Ошибка: инструмент не имеет известного permission level."

    # READ и LOW выполняются автоматически. MEDIUM и выше требуют подтверждения.
    if level in {
        PermissionLevel.READ,
        PermissionLevel.LOW,
    }:
        return True, level, None

    # CRITICAL дополнительно проходит отдельную проверку.
    if level == PermissionLevel.CRITICAL:
        command = arguments.get("command", "")
        if tool_name != "execute_shell" or not critical_shell_command_is_allowed(command):
            return False, level, "Ошибка: критическая операция не разрешена политикой."

    # HIGH и разрешённые CRITICAL требуют явного подтверждения.
    if confirm_action(tool_name, arguments, level):
        return True, level, None

    return False, level, "Операция отменена пользователем."

def call_tool(tool_name: str, arguments: dict) -> str:
    if tool_name == "list_directory":
        return list_directory(arguments["path"])
    if tool_name == "create_directory":
        return create_directory(arguments["path"])
    if tool_name == "read_file":
        return read_file(arguments["path"])
    if tool_name == "write_file":
        return write_file(arguments["path"], arguments["content"])
    if tool_name == "delete_file":
        return delete_file(arguments["path"])
    if tool_name == "move_file":
        return move_file(arguments["source"], arguments["destination"])
    if tool_name == "list_project_directory":
        return list_project_directory(arguments.get("path", "."))
    if tool_name == "read_project_file":
        return read_project_file(arguments["path"])
    if tool_name == "write_project_file":
        return write_project_file(arguments["path"], arguments["content"])
    if tool_name == "read_source":
        return read_source(arguments.get("path", "pai.py"))
    if tool_name == "edit_project_file":
        return edit_project_file(
            arguments["path"],
            arguments["old_text"],
            arguments["new_text"],
        )
    if tool_name == "edit_source":
        return edit_source(arguments["old_text"], arguments["new_text"])
    if tool_name == "check_syntax":
        return check_syntax(arguments["path"])
    if tool_name == "git_diff":
        return git_diff(arguments.get("path", "."))
    if tool_name == "run_tests":
        return run_tests()
    if tool_name == "hyprland_get_clients":
        return hyprland_get_clients()
    if tool_name == "hyprland_get_workspaces":
        return hyprland_get_workspaces()
    if tool_name == "hyprland_switch_workspace":
        return hyprland_switch_workspace(arguments["workspace"])
    if tool_name == "execute_shell":
        return execute_shell(arguments["command"])

    return f"Ошибка: неизвестный инструмент: {tool_name}"


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
    """Small serializable workflow state for one non-trivial user task."""
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
        """Append the corrective action as the active step of the FIX phase."""
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
        state = cls(
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
        return state

    def summary(self) -> str:
        steps = "; ".join(f"{step.status.value}: {step.description}" for step in self.plan)
        update_state = "pending" if self.self_update_pending else "none"
        return (
            f"Workflow={self.phase.value}; fixes={self.fix_cycles}/{MAX_FIX_CYCLES}; "
            f"self-update={update_state}; plan={steps}"
        )


def set_workflow_phase(task_state: TaskState, new_phase: WorkflowPhase) -> None:
    """Change phase and emit one compact workflow journal entry."""
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
    """Log outcomes without arguments, adding permission context when useful."""
    permission = f" [{level.value}]" if level else ""
    if confirmation_requested:
        print(f"[{phase.value}] {tool_name}{permission} → CONFIRM")
    if tool_succeeded(result):
        print(f"[{phase.value}] {tool_name}{permission} → OK")
    else:
        # Tool output can contain file contents, command output, or credentials.
        print(f"[FIX] {tool_name}{permission} → ERROR: инструмент вернул ошибку")


def needs_workflow(user_input: str) -> bool:
    """Keep conversational/simple requests out of the structured workflow."""
    lowered = user_input.lower()
    signals = ("измен", "создай", "удали", "перемест", "исправ", "реализ", "проверь", "тест", "файл", "проект")
    return len(user_input) > 120 or any(signal in lowered for signal in signals)


def tool_succeeded(result: str) -> bool:
    return not result.startswith("Ошибка") and "отменена пользователем" not in result


def source_fingerprint() -> tuple[int, int] | None:
    """Return a fingerprint of the on-disk pai.py currently being run."""
    try:
        stat = Path(__file__).resolve().stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def is_current_source(path: str) -> bool:
    """True only for the running project's pai.py, never similarly named files."""
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
    """Validate a written pai.py before a replacement process is allowed."""
    syntax_result = check_syntax("pai.py")
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
    """Atomically persist conversation and workflow state for replacement process."""
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
    """Consume valid restart state, including any active task workflow."""
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
    """Replace this process with the just-validated on-disk pai.py."""
    if restart_count >= MAX_CONSECUTIVE_SELF_RESTARTS:
        print(
            "PAI > Самообновление применено, но перезапуск отменён: "
            f"достигнут лимит {MAX_CONSECUTIVE_SELF_RESTARTS} последовательных перезапусков."
        )
        return

    if task_state:
        # The restored process already runs the accumulated source changes.
        task_state.self_update_pending = False
        task_state.self_update_validated = False
    save_restart_state(messages, restart_count + 1, task_state)
    print("PAI > Самообновление проверено. Перезапуск новой версии PAI...")
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])


def compact_messages(messages: list[dict]) -> list[dict]:
    """Compact messages to prevent context overflow by removing old tool call/result pairs 
    while preserving system prompt, user goal, and the latest valid tool call + result pairs.
    """
    if len(messages) <= 30:
        return messages
        
    # Keep system prompt
    compacted = [msg for msg in messages if msg.get("role") == "system"]
    
    # Collect all user messages and tool interactions
    user_messages = [msg for msg in messages if msg.get("role") == "user"]
    tool_interactions = [msg for msg in messages if msg.get("role") in ("assistant", "tool")]
    
    # Keep only the most recent tool call/result pairs (max 8 pairs)
    recent_tool_pairs = []
    tool_call_count = 0
    
    # Process messages in reverse to collect recent tool results and calls
    for msg in reversed(tool_interactions):
        if msg.get("role") == "tool":
            recent_tool_pairs.append(msg)
        elif msg.get("role") == "assistant" and "tool_calls" in msg:
            if tool_call_count < 8:  # Keep last 8 tool calls
                recent_tool_pairs.append(msg)
                tool_call_count += 1
        if tool_call_count >= 8:
            break
    
    # Add user messages at the end (most recent first)
    for msg in reversed(user_messages):
        compacted.append(msg)
        
    # Add recent tool pairs at the end  
    for msg in reversed(recent_tool_pairs):
        compacted.append(msg)
        
    return compacted


def run_agent_turn(
    messages: list[dict],
    restart_count: int = 0,
    task_state: TaskState | None = None,
) -> TaskState | None:
    """Run the existing tool loop while maintaining optional workflow state."""
    if task_state and task_state.phase == WorkflowPhase.PLAN:
        print(f"[PLAN] {WorkflowPhase.PLAN.value} → {WorkflowPhase.PLAN.value}")
        task_state.activate_next()
        task_state.finish_active()
        set_workflow_phase(task_state, WorkflowPhase.EXECUTE)
        task_state.activate_next()

    for step in range(1, MAX_AGENT_STEPS + 1):
       
        print(f"\n[DEBUG] messages: {len(messages)}")
        print(f"[DEBUG] chars: {sum(len(str(m)) for m in messages)}")       
 
        # Compact messages before each request to prevent overflow
        if len(messages) > 50:
            messages = compact_messages(messages)
            print(f"[COMPACT] Уменьшено до {len(messages)} сообщений")
        
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
            reasoning_effort="none",
        )

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
            if task_state and task_state.phase != WorkflowPhase.DONE:
                if task_state.phase == WorkflowPhase.VERIFY:
                    print("PAI > Нельзя завершить задачу без успешной проверки результата.")
                else:
                    set_workflow_phase(task_state, WorkflowPhase.VERIFY)
                    task_state.activate_next()
            if task_state and task_state.self_update_pending and task_state.self_update_validated:
                restart_with_updated_source(messages, restart_count, task_state)
            print(f"PAI > {message.content or '(пустой ответ)'}")
            return task_state

        self_source_changed = False
        batch_failed = False
        verification_ran = False
        changed_something = False
        for tool_call in message.tool_calls:
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
            if tool_name in {"write_file", "delete_file", "move_file", "write_project_file", "edit_project_file", "edit_source"}:
                if task_state and tool_succeeded(result):
                    changed_something = True
                    task_state.requires_verification = True
            if (
                tool_name in {"edit_source", "edit_project_file", "write_project_file"}
                and is_current_source(arguments.get("path", "pai.py"))
                and source_before != source_fingerprint()
                and result.startswith("Файл")
            ):
                self_source_changed = True
                if task_state:
                    task_state.self_update_pending = True
                    task_state.self_update_validated = False

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
                # A later source fix can arrive after an earlier VERIFY reached
                # DONE; every new change must start a fresh verification round.
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

        # Give the model an explicit continuation cue after every tool batch.
        messages.append(
            {
                "role": "user",
                "content": (
                    "Продолжай выполнение исходной задачи самостоятельно. "
                    "Проанализируй результаты последних инструментов, исправь ошибки "
                    "при необходимости и выполни следующие нужные шаги. "
                    "Не останавливайся только потому, что один инструмент успешно сработал. "
                    "Заверши работу только когда исходная задача действительно выполнена "
                    "или объективно заблокирована."
                    + (f"\nТекущее состояние workflow: {task_state.summary()}" if task_state else "")
                ),
            }
        )

        # A source update is deliberately not restarted here. More VERIFY/FIX
        # tool batches may follow; restart only after the validated series settles.

    print(
        f"PAI > Достигнут лимит {MAX_AGENT_STEPS} шагов агента. "
        "Остановка выполнена для защиты от бесконечного цикла."
    )
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
        task_state = TaskState.for_goal(user_input) if needs_workflow(user_input) else None
        if task_state:
            print(f"PAI > Создан план: {task_state.summary()}")

        try:
            task_state = run_agent_turn(messages, restart_count, task_state)
        except Exception as e:
            print(f"PAI > Ошибка связи с моделью или API: {e}")


if __name__ == "__main__":
    main()
