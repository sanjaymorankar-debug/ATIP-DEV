"""
`python -m ops scan`: the two fixed test-fixture passwords are not findings, and nothing
else is let through (TEST_SPEC.md S6.2, D11).

ops/scan.py exempts an exact (file, value) pair from the "secret_assignment" pattern. The
same value in another file, another value in those files, and every other pattern must
still be reported.
"""

import shutil
import subprocess

import pytest

from ops import scan

VALUE = "Correct-Horse-Battery-9!"
OTHER = "Another-Horse-Battery-8!"          # (written so that this file itself scans clean)


@pytest.fixture
def repo(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "ops").mkdir()

    def add(path, text):
        (tmp_path / path).write_text(text, encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", path], check=True)
    return tmp_path, add


def test_the_allowlist_is_exactly_the_two_fixture_passwords_in_tests():
    assert scan.FIXTURE_VALUES == {("tests/test_w39_api_contract.py", VALUE),
                                   ("tests/test_w39_exec_data_tests.py", VALUE)}
    assert all(f.startswith("tests/") for f, _ in scan.FIXTURE_VALUES)


def test_the_fixture_passwords_are_not_findings(repo):
    root, add = repo
    add("tests/test_w39_api_contract.py", f'PASSWORD = "{VALUE}"\n')
    add("tests/test_w39_exec_data_tests.py", f'\nPASSWORD = "{VALUE}"\n')
    assert scan.scan_secrets(str(root)) == []


def test_anything_else_is_still_found(repo):
    root, add = repo
    add("tests/test_w39_api_contract.py",
        f'PASSWORD = "{VALUE}"\npassword = "{OTHER}"\n')                      # another value, same file
    add("tests/test_other.py", f'PASSWORD = "{VALUE}"\n')                    # same value, other file
    add("ops/settings.py", f'password = "{VALUE}"\n')
    add("tests/test_w39_exec_data_tests.py",
        f'PASSWORD = "{VALUE}"\nTOKEN = "sk-ant-' + "a" * 30 + '"\n')        # another pattern
    got = sorted((x["file"], x["line"], x["pattern"]) for x in scan.scan_secrets(str(root)))
    assert got == [("ops/settings.py", 1, "secret_assignment"),
                   ("tests/test_other.py", 1, "secret_assignment"),
                   ("tests/test_w39_api_contract.py", 2, "secret_assignment"),
                   ("tests/test_w39_exec_data_tests.py", 2, "anthropic_key")]
