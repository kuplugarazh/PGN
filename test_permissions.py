import json
from types import SimpleNamespace

import pai


def test_read_and_low_permissions_are_automatic(monkeypatch):
    monkeypatch.setattr(pai, "confirm_action", lambda *_args: (_ for _ in ()).throw(AssertionError()))

    assert pai.authorize_tool("read_file", {"path": "note.txt"}) == (
        True,
        pai.PermissionLevel.READ,
        None,
    )
    assert pai.authorize_tool("create_directory", {"path": "scratch"}) == (
        True,
        pai.PermissionLevel.LOW,
        None,
    )


def test_medium_permission_requires_confirmation(monkeypatch):
    observed = []
    monkeypatch.setattr(
        pai,
        "confirm_action",
        lambda tool, arguments, level: observed.append((tool, arguments, level)) or False,
    )

    allowed, level, reason = pai.authorize_tool("write_file", {"path": "note.txt", "content": "x"})

    assert not allowed
    assert level == pai.PermissionLevel.MEDIUM
    assert reason == "Операция отменена пользователем."
    assert observed == [("write_file", {"path": "note.txt", "content": "x"}, pai.PermissionLevel.MEDIUM)]


def test_unknown_tool_or_permission_is_denied(monkeypatch):
    monkeypatch.setattr(pai, "confirm_action", lambda *_args: True)

    assert pai.authorize_tool("not_a_tool", {})[0] is False
    monkeypatch.setitem(pai.TOOL_PERMISSIONS, "read_file", "safe")
    assert pai.authorize_tool("read_file", {"path": "note.txt"})[0] is False


def test_sudo_shell_is_critical_and_blocked_without_execution(monkeypatch):
    command = "sudo rm -rf /tmp/should-not-run"
    assert pai.permission_for_tool("execute_shell", {"command": command}) == pai.PermissionLevel.CRITICAL

    executed = []
    tool_call = SimpleNamespace(
        id="dangerous",
        function=SimpleNamespace(
            name="execute_shell",
            arguments=json.dumps({"command": "sudo rm -rf /tmp/should-not-run"}),
        ),
    )
    response_with_tool = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[tool_call]))]
    )
    response_done = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=None))]
    )
    responses = iter((response_with_tool, response_done))
    monkeypatch.setattr(
        pai,
        "client",
        SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_kwargs: next(responses))
            )
        ),
    )
    monkeypatch.setattr(pai, "execute_shell", lambda command: executed.append(command) or "unexpected")
    monkeypatch.setattr(pai, "confirm_action", lambda *_args: True)

    pai.run_agent_turn([{"role": "system", "content": "test"}])

    assert executed == []


def test_run_agent_turn_passes_current_shutdown_confirmation_to_execution(monkeypatch):
    command = "shutdown now"
    events = []
    executed = []
    tool_call = SimpleNamespace(
        id="shutdown",
        function=SimpleNamespace(
            name="execute_shell",
            arguments=json.dumps({"command": command}),
        ),
    )
    responses = iter((
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[tool_call]))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=None))]),
    ))
    monkeypatch.setattr(
        pai,
        "client",
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: next(responses)))),
    )
    monkeypatch.setattr(
        pai,
        "confirm_action",
        lambda tool, arguments, level: events.append(("confirm", tool, arguments, level)) or True,
    )

    def record_execution(received):
        assert events == [("confirm", "execute_shell", {"command": command}, pai.PermissionLevel.CRITICAL)]
        executed.append(received)
        return "mocked"

    monkeypatch.setattr(pai, "execute_shell", record_execution)
    pai.run_agent_turn([{"role": "system", "content": "test"}])

    assert executed == [command]


def test_unapproved_critical_shell_remains_blocked_after_confirmation(monkeypatch):
    monkeypatch.setattr(pai, "confirm_action", lambda *_args: True)

    assert pai.authorize_tool("execute_shell", {"command": "sudo true"}) == (
        False,
        pai.PermissionLevel.CRITICAL,
        "Ошибка: критическая операция не разрешена политикой.",
    )


def test_shell_levels_are_argument_sensitive():
    assert pai.shell_command_level("pwd") == pai.PermissionLevel.READ
    assert pai.shell_command_level("touch temporary.txt") == pai.PermissionLevel.LOW
    assert pai.shell_command_level("rm temporary.txt") == pai.PermissionLevel.HIGH
    assert pai.shell_command_level("git status") == pai.PermissionLevel.READ
    assert pai.shell_command_level("git commit -m test") == pai.PermissionLevel.HIGH
    assert pai.shell_command_level("sed -i s/a/b/ note.txt") == pai.PermissionLevel.HIGH
    assert pai.shell_command_level("find . -delete") == pai.PermissionLevel.HIGH
    assert pai.shell_command_level("sudo true") == pai.PermissionLevel.CRITICAL
