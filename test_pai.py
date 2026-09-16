from pai import read_project_file, git_diff, edit_source


def test_git_diff_returns_string():
    result = git_diff()
    assert isinstance(result, str)



def test_edit_source_missing_fragment():
    result = edit_source("# DEFINITELY_MISSING_FRAGMENT", "# replacement")
    assert "не найден" in result

