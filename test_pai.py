from pai import git_diff


def test_git_diff_returns_string():
    result = git_diff()
    assert isinstance(result, str)
