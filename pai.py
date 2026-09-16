import json
import os
import subprocess
from pathlib import Path

from openai import OpenAI


client = OpenAI(
    base_url="http://127.0.0.1:20128/v1",
    api_key=os.environ["OMNIROUTE_API_KEY"],
)

WORKSPACE = Path.home() / "PAIWorkspace"


def list_directory(path: str) -> str:
    target = (WORKSPACE / path).resolve()

    try:
        target.relative_to(WORKSPACE.resolve())
    except ValueError:
        return "Ошибка: нельзя просматривать директории за пределами PAIWorkspace."
    if not target.exists():
        return f"Ошибка: директория не существует: {target}"

    if not target.is_dir():
        return f"Ошибка: это не директория: {target}"

    items = []
    for item in sorted(target.iterdir()):
        kind = "DIR " if item.is_dir() else "FILE"
        items.append(f"{kind} {item.name}")

    return "\n".join(items) if items else "(пусто)"

def create_directory(path: str) -> str:
    target = (WORKSPACE / path).resolve()

    try:
        target.relative_to(WORKSPACE.resolve())
    except ValueError:
        return "Ошибка: нельзя создавать директории за пределами PAIWorkspace."

    if target.exists():
        if target.is_dir():
            return f"Директория уже существует: {target}"
        return f"Ошибка: файл уже существует: {target}"

    target.mkdir(parents=True)

    return f"Директория создана: {target}"

def read_file(path: str) -> str:
    target = (WORKSPACE / path).resolve()

    try:
        target.relative_to(WORKSPACE.resolve())
    except ValueError:
        return "Ошибка: нельзя читать файлы за пределами PAIWorkspace."

    if not target.exists():
        return f"Ошибка: файл не существует: {target}"

    if not target.is_file():
        return f"Ошибка: это не файл: {target}"

    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "Ошибка: файл не является UTF-8 текстовым файлом."
    except OSError as e:
        return f"Ошибка чтения файла: {e}"

def write_file(path: str, content: str) -> str:
    target = (WORKSPACE / path).resolve()

    try:
        target.relative_to(WORKSPACE.resolve())
    except ValueError:
        return "Ошибка: нельзя записывать файлы за пределами PAIWorkspace."

    if target.exists() and target.is_dir():
        return f"Ошибка: указанный путь является директорией: {target}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Файл записан: {target}"
    except OSError as e:
        return f"Ошибка записи файла: {e}"

def confirm_action(action: str) -> bool:
    answer = input(f"PAI хочет выполнить действие: {action}\nРазрешить? [y/N] ")
    return answer.strip().lower() in ("y", "yes", "д", "да")

def run_tests() -> str:
    try:
        process = subprocess.run(
            ["pytest", "-q", "test_pai.py"],
            cwd=str(Path.home() / "pai"),
            capture_output=True,
            text=True,
            timeout=60,
        )

        output = [
            f"Код завершения: {process.returncode}",
        ]

        if process.stdout:
            output.append(f"STDOUT:\n{process.stdout[:20000]}")

        if process.stderr:
            output.append(f"STDERR:\n{process.stderr[:20000]}")

        return "\n".join(output)

    except subprocess.TimeoutExpired:
        return "Ошибка: тесты превысили лимит времени 60 секунд."

    except Exception as e:
        return f"Ошибка запуска тестов: {e}"

def git_diff() -> str:
    try:
        process = subprocess.run(
            ["git", "-C", str(Path.home() / "pai"), "diff", "--", "pai.py"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if process.returncode != 0:
            return f"Ошибка git diff:\n{process.stderr}"

        return process.stdout if process.stdout else "(изменений в pai.py нет)"

    except subprocess.TimeoutExpired:
        return "Ошибка: git diff превысил лимит времени 10 секунд."

    except Exception as e:
        return f"Ошибка git diff: {e}"

def execute_shell(command: str) -> str:
    try:
        process = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        output = []

        output.append(f"Код завершения: {process.returncode}")

        if process.stdout:
             stdout = process.stdout[:20000]
             if len(process.stdout) > 20000:
                stdout += "\n[STDOUT обрезан: превышен лимит 20000 символов]"
             output.append(f"STDOUT:\n{stdout}")
        if process.stderr:
            stderr = process.stderr[:20000]
            if len(process.stderr) > 20000:
               stderr += "\n[STDERR обрезан: превышен лимит 20000 символов]"
            output.append(f"STDERR:\n{stderr}")
        return "\n".join(output)

    except subprocess.TimeoutExpired:
        return "Ошибка: команда превысила лимит времени 30 секунд."

    except Exception as e:
        return f"Ошибка выполнения команды: {e}"

def delete_file(path: str) -> str:
    target = (WORKSPACE / path).resolve()

    try:
        target.relative_to(WORKSPACE.resolve())
    except ValueError:
        return "Ошибка: нельзя удалять файлы за пределами PAIWorkspace."

    if not target.exists():
        return f"Ошибка: файл не существует: {target}"

    if not target.is_file():
        return f"Ошибка: это не файл: {target}"

    try:
        target.unlink()
        return f"Файл удалён: {target}"
    except OSError as e:
        return f"Ошибка удаления файла: {e}"


tools = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "Показать содержимое директории.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь к директории.",
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
            "description": "Прочитать содержимое текстового файла внутри PAIWorkspace.",
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
            "description": "Записать содержимое в текстовый файл внутри PAIWorkspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Имя или относительный путь к файлу внутри PAIWorkspace.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Текст, который нужно записать в файл.",
                    },
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
        "name": "execute_shell",
        "description": "Выполнить shell-команду в системе Linux.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell-команда для выполнения.",
                }
            },
            "required": ["command"],
        },
    },
},

{
    "type": "function",
    "function": {
        "name": "git_diff",
        "description": "Показать текущие изменения в файле pai.py относительно последнего Git-коммита.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
},

{
    "type": "function",
    "function": {
        "name": "run_tests",
        "description": "Запустить автоматические тесты PAI из файла test_pai.py.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
},

]

TOOL_PERMISSIONS = {
    "list_directory": "safe",
    "read_file": "safe",
    "write_file": "confirm",
    "delete_file": "confirm",
    "execute_shell": "confirm",
    "git_diff": "safe",
    "run_tests": "safe",
}

messages = [
    {
        "role": "system",
        "content": """Ты — PAI (Personal AI Environment), персональный AI-агент пользователя.

Ты работаешь на Linux-системе пользователя.

У тебя есть доступ только к тем инструментам, которые переданы тебе в запросе.
Никогда не утверждай, что выполнил действие, если соответствующий инструмент не был вызван.

Когда тебе нужен инструмент, используй его.
После получения результата инструмента проанализируй результат и продолжи выполнение задачи.

Правила выбора инструментов:
- Если задача связана с запуском автоматических тестов PAI, используй run_tests, а не execute_shell.
- Если задача связана с просмотром изменений pai.py, используй git_diff, а не execute_shell.
- Используй execute_shell только тогда, когда для задачи нет подходящего специализированного инструмента.
- После изменения исходного кода сначала запусти run_tests, затем используй git_diff для проверки изменений.
- Никогда не утверждай, что действие выполнено успешно, если соответствующий инструмент не был вызван или вернул ошибку."""
    }
]


if __name__ == "__main__":
    print("PAI запущен.")
    print("Для выхода: exit")
    print(f"Workspace: {WORKSPACE}")
    print()


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

        messages.append({
            "role": "user",
            "content": user_input,
        })

        while True:
            response = client.chat.completions.create(
                model="kr/claude-sonnet-4.5",
                messages=messages,
                tools=tools,
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
                print(f"PAI > {message.content}")
                break

            for tool_call in message.tool_calls:

                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                except Exception as e:
                    result = f"Ошибка разбора аргументов инструмента: {e}"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })
                    continue
  
                permission = TOOL_PERMISSIONS.get(tool_call.function.name)

                if permission is None:
                    result = "Ошибка: инструмент не имеет настроенного уровня доступа."
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })
                    continue

                if permission == "confirm":

                    if tool_call.function.name == "write_file":
                        action = f"записать файл {arguments['path']}"
                    elif tool_call.function.name == "delete_file":
                        action = f"удалить файл {arguments['path']}"
                    elif tool_call.function.name == "execute_shell":
                        action = f"выполнить команду: {arguments['command']}"
                    else:
                        action = f"выполнить инструмент {tool_call.function.name}"

                    if not confirm_action(action):
                        result = "Операция отменена пользователем."
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result,
                        })
                        continue

                if tool_call.function.name == "list_directory":
                    try:
                        result = list_directory(arguments["path"])
                    except Exception as e:
                        result = f"Ошибка инструмента: {e}"

                elif tool_call.function.name == "create_directory":
                    try:
                        result = create_directory(arguments["path"])
                    except Exception as e:
                        result = f"Ошибка инструмента: {e}"

                elif tool_call.function.name == "read_file":
                    try:
                        result = read_file(arguments["path"])
                    except Exception as e:
                        result = f"Ошибка инструмента: {e}"

                elif tool_call.function.name == "write_file":
                    try:
                        result = write_file(
                            arguments["path"],
                            arguments["content"],
                        )
                    except Exception as e:
                        result = f"Ошибка инструмента: {e}"

                elif tool_call.function.name == "delete_file":
                    try:
                        result = delete_file(arguments["path"])
                    except Exception as e:
                        result = f"Ошибка инструмента: {e}"

                elif tool_call.function.name == "execute_shell":
                    try:
                        result = execute_shell(arguments["command"])
                    except Exception as e:
                        result = f"Ошибка инструмента: {e}"

                elif tool_call.function.name == "git_diff":
                    try:
                        result = git_diff()
                    except Exception as e:
                        result = f"Ошибка инструмента: {e}"
                elif tool_call.function.name == "run_tests":
                    try:
                        result = run_tests()
                    except Exception as e:
                        result = f"Ошибка инструмента: {e}"

                else:
                    result = "Ошибка: неизвестный инструмент."          
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
