from pai import git_diff


def test_git_diff_clean():
    result = git_diff()
    assert result == "(изменений в pai.py нет)"
