from pai import git_diff


def test_git_diff_returns_string():
    result = git_diff()
    assert isinstance(result, str)


from pai import edit_source


def test_edit_source_missing_fragment():
    result = edit_source("# DEFINITELY_MISSING_FRAGMENT", "# replacement")
    assert "не найден" in result
