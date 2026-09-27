import os
import subprocess
import urllib.request


def test_isolated_hermes_home_points_to_tmp(isolated_hermes_home):
    assert isolated_hermes_home.exists()
    assert os.environ["HERMES_HOME"] == str(isolated_hermes_home)
    assert (isolated_hermes_home / "config.yaml").exists()


def test_hermetic_git_repo_has_three_commits(hermetic_git_repo):
    log = subprocess.check_output(
        ["git", "-C", str(hermetic_git_repo.repo), "log", "--oneline"]
    ).decode()
    assert len(log.strip().splitlines()) == 3


def test_fake_gateway_responds(fake_gateway):
    with urllib.request.urlopen(f"http://127.0.0.1:{fake_gateway.port}/api/status") as r:
        assert r.status == 200
        body = r.read().decode()
        assert '"ok": true' in body