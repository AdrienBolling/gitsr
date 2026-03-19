"""Tests for gitsr.py"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers to patch the config/secrets directory so tests don't touch $HOME
# ---------------------------------------------------------------------------

import gitsr  # the module under test


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Redirect CONFIG_DIR and CONFIG_FILE to a temporary directory."""
    fake_config_dir = tmp_path / ".gitsr"
    fake_config_file = fake_config_dir / "config.json"
    monkeypatch.setattr(gitsr, "CONFIG_DIR", fake_config_dir)
    monkeypatch.setattr(gitsr, "CONFIG_FILE", fake_config_file)
    yield fake_config_dir


# ---------------------------------------------------------------------------
# _inject_token
# ---------------------------------------------------------------------------


class TestInjectToken:
    def test_github_url(self):
        url = "https://github.com/user/repo.git"
        result = gitsr._inject_token(url, "ghp_TOKEN")
        assert result == "https://ghp_TOKEN@github.com/user/repo.git"

    def test_bitbucket_url(self):
        url = "https://bitbucket.org/user/repo.git"
        result = gitsr._inject_token(url, "APPPASS")
        assert result == "https://x-token-auth:APPPASS@bitbucket.org/user/repo.git"

    def test_generic_https_url(self):
        url = "https://gitlab.example.com/user/repo.git"
        result = gitsr._inject_token(url, "TOKEN")
        assert result == "https://oauth2:TOKEN@gitlab.example.com/user/repo.git"

    def test_no_token_returns_original(self):
        url = "https://github.com/user/repo.git"
        assert gitsr._inject_token(url, None) == url
        assert gitsr._inject_token(url, "") == url

    def test_ssh_url_unchanged(self):
        url = "git@github.com:user/repo.git"
        assert gitsr._inject_token(url, "TOKEN") == url


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


class TestConfigHelpers:
    def test_load_config_empty(self):
        cfg = gitsr._load_config()
        assert cfg == {"projects": {}}

    def test_save_and_load_config(self):
        cfg = {"projects": {"myproject": {"remotes": {}}}}
        gitsr._save_config(cfg)
        assert gitsr._load_config() == cfg

    def test_load_secrets_missing(self):
        assert gitsr._load_secrets("nonexistent") == {}

    def test_save_and_load_secrets(self, isolated_config):
        gitsr._save_secrets("myproject", {"token": "abc123"})
        loaded = gitsr._load_secrets("myproject")
        assert loaded == {"token": "abc123"}

    def test_secrets_file_permissions(self, isolated_config):
        gitsr._save_secrets("myproject", {"token": "abc"})
        sf = gitsr._secrets_file("myproject")
        mode = oct(sf.stat().st_mode)[-3:]
        assert mode == "600"


# ---------------------------------------------------------------------------
# GitSR.setup
# ---------------------------------------------------------------------------


class TestSetup:
    def test_basic_setup(self, capsys):
        cli = gitsr.GitSR()
        cli.setup(
            "proj",
            "https://github.com/u/r.git",
            "https://bitbucket.org/u/r.git",
        )
        config = gitsr._load_config()
        proj = config["projects"]["proj"]
        assert proj["remotes"]["source"] == "https://github.com/u/r.git"
        assert proj["remotes"]["target"] == "https://bitbucket.org/u/r.git"
        assert proj["default_branch"] == "main"
        assert proj["default_source"] == "source"
        assert proj["default_target"] == "target"
        out = capsys.readouterr().out
        assert "proj" in out

    def test_custom_names_and_branch(self):
        cli = gitsr.GitSR()
        cli.setup(
            "p2",
            "https://github.com/u/r.git",
            "https://bitbucket.org/u/r.git",
            source_name="github",
            target_name="bitbucket",
            branch="develop",
        )
        proj = gitsr._load_config()["projects"]["p2"]
        assert "github" in proj["remotes"]
        assert "bitbucket" in proj["remotes"]
        assert proj["default_branch"] == "develop"

    def test_duplicate_setup_warns(self, capsys):
        cli = gitsr.GitSR()
        cli.setup("p", "https://github.com/u/r.git", "https://b.org/u/r.git")
        cli.setup("p", "https://github.com/u/r.git", "https://b.org/u/r.git")
        out = capsys.readouterr().out
        assert "already exists" in out


# ---------------------------------------------------------------------------
# GitSR.set_token
# ---------------------------------------------------------------------------


class TestSetToken:
    def _setup_project(self):
        cli = gitsr.GitSR()
        cli.setup("proj", "https://github.com/u/r.git", "https://bitbucket.org/u/r.git")
        return cli

    def test_generic_token(self):
        cli = self._setup_project()
        cli.set_token("proj", "mytoken")
        secrets = gitsr._load_secrets("proj")
        assert secrets["token"] == "mytoken"

    def test_remote_specific_token(self):
        cli = self._setup_project()
        cli.set_token("proj", "ghp_abc", remote="github")
        secrets = gitsr._load_secrets("proj")
        assert secrets["github_token"] == "ghp_abc"

    def test_unknown_project(self, capsys):
        cli = gitsr.GitSR()
        cli.set_token("nonexistent", "tok")
        out = capsys.readouterr().out
        assert "not found" in out


# ---------------------------------------------------------------------------
# GitSR.add_remote
# ---------------------------------------------------------------------------


class TestAddRemote:
    def test_add_remote(self):
        cli = gitsr.GitSR()
        cli.setup("p", "https://github.com/u/r.git", "https://b.org/u/r.git")
        cli.add_remote("p", "gitlab", "https://gitlab.com/u/r.git")
        proj = gitsr._load_config()["projects"]["p"]
        assert proj["remotes"]["gitlab"] == "https://gitlab.com/u/r.git"

    def test_add_remote_set_as_target(self):
        cli = gitsr.GitSR()
        cli.setup("p", "https://github.com/u/r.git", "https://b.org/u/r.git")
        cli.add_remote("p", "mirror", "https://mirror.example.com/r.git", set_as_target=True)
        proj = gitsr._load_config()["projects"]["p"]
        assert proj["default_target"] == "mirror"

    def test_add_remote_unknown_project(self, capsys):
        cli = gitsr.GitSR()
        cli.add_remote("nope", "r", "https://example.com/r.git")
        out = capsys.readouterr().out
        assert "not found" in out


# ---------------------------------------------------------------------------
# GitSR.remove
# ---------------------------------------------------------------------------


class TestRemove:
    def test_remove_project(self):
        cli = gitsr.GitSR()
        cli.setup("p", "https://github.com/u/r.git", "https://b.org/u/r.git")
        cli.remove("p")
        assert "p" not in gitsr._load_config()["projects"]

    def test_remove_nonexistent(self, capsys):
        cli = gitsr.GitSR()
        cli.remove("ghost")
        out = capsys.readouterr().out
        assert "not found" in out


# ---------------------------------------------------------------------------
# GitSR.set_defaults / set_default_branch
# ---------------------------------------------------------------------------


class TestSetDefaults:
    def _setup(self):
        cli = gitsr.GitSR()
        cli.setup("p", "https://github.com/u/r.git", "https://b.org/u/r.git",
                  source_name="github", target_name="bitbucket")
        cli.add_remote("p", "gitlab", "https://gitlab.com/u/r.git")
        return cli

    def test_set_default_branch(self):
        cli = self._setup()
        cli.set_default_branch("p", "develop")
        assert gitsr._load_config()["projects"]["p"]["default_branch"] == "develop"

    def test_set_defaults_target(self):
        cli = self._setup()
        cli.set_defaults("p", target="gitlab")
        assert gitsr._load_config()["projects"]["p"]["default_target"] == "gitlab"

    def test_set_defaults_source(self):
        cli = self._setup()
        cli.set_defaults("p", source="bitbucket")
        assert gitsr._load_config()["projects"]["p"]["default_source"] == "bitbucket"

    def test_set_defaults_unknown_remote(self, capsys):
        cli = self._setup()
        cli.set_defaults("p", target="nonexistent")
        out = capsys.readouterr().out
        assert "not configured" in out


# ---------------------------------------------------------------------------
# GitSR.list / info
# ---------------------------------------------------------------------------


class TestListAndInfo:
    def test_list_empty(self, capsys):
        cli = gitsr.GitSR()
        cli.list()
        out = capsys.readouterr().out
        assert "No projects" in out

    def test_list_projects(self, capsys):
        cli = gitsr.GitSR()
        cli.setup("alpha", "https://github.com/u/a.git", "https://b.org/u/a.git")
        cli.setup("beta", "https://github.com/u/b.git", "https://b.org/u/b.git")
        cli.list()
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "beta" in out

    def test_info_known_project(self, capsys):
        cli = gitsr.GitSR()
        cli.setup("p", "https://github.com/u/r.git", "https://b.org/u/r.git")
        cli.info("p")
        out = capsys.readouterr().out
        assert "https://github.com/u/r.git" in out

    def test_info_unknown_project(self, capsys):
        cli = gitsr.GitSR()
        cli.info("ghost")
        out = capsys.readouterr().out
        assert "not found" in out

    def test_info_shows_token_keys(self, capsys):
        cli = gitsr.GitSR()
        cli.setup("p", "https://github.com/u/r.git", "https://b.org/u/r.git")
        cli.set_token("p", "ghp_abc", remote="source")
        cli.info("p")
        out = capsys.readouterr().out
        assert "source_token" in out


# ---------------------------------------------------------------------------
# GitSR.sync
# ---------------------------------------------------------------------------


class TestSync:
    def _setup_project(self, cli):
        cli.setup(
            "proj",
            "https://github.com/u/r.git",
            "https://bitbucket.org/u/r.git",
            source_name="github",
            target_name="bitbucket",
            branch="main",
        )

    @patch("gitsr._run_git")
    def test_sync_initial_clone(self, mock_run, capsys):
        """First sync should clone the source as a bare repo."""
        cli = gitsr.GitSR()
        self._setup_project(cli)
        cli.sync("proj")
        # First call must be a bare clone
        first_call_args = mock_run.call_args_list[0][0][0]
        assert "clone" in first_call_args
        assert "--bare" in first_call_args

    @patch("gitsr._run_git")
    def test_sync_subsequent_fetch_push(self, mock_run, isolated_config, capsys):
        """Subsequent syncs should fetch then push (no clone)."""
        cli = gitsr.GitSR()
        self._setup_project(cli)
        # Simulate cache already exists
        cache_dir = isolated_config / "cache" / "proj"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cli.sync("proj")
        calls = [c[0][0] for c in mock_run.call_args_list]
        assert any("fetch" in c for c in calls)
        assert any("push" in c for c in calls)

    @patch("gitsr._run_git")
    def test_sync_with_token(self, mock_run, capsys):
        """Token should be injected into the remote URLs passed to git."""
        cli = gitsr.GitSR()
        self._setup_project(cli)
        cli.set_token("proj", "ghp_SECRET", remote="github")
        cli.sync("proj")
        clone_cmd = mock_run.call_args_list[0][0][0]
        # The source URL passed to git must contain the token
        assert any("ghp_SECRET" in str(arg) for arg in clone_cmd)

    @patch("gitsr._run_git")
    def test_sync_force_flag(self, mock_run, isolated_config, capsys):
        """--force should append --force to the push command."""
        cli = gitsr.GitSR()
        self._setup_project(cli)
        cache_dir = isolated_config / "cache" / "proj"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cli.sync("proj", force=True)
        push_cmd = mock_run.call_args_list[-1][0][0]
        assert "--force" in push_cmd

    @patch("gitsr._run_git")
    def test_sync_custom_branch(self, mock_run, isolated_config, capsys):
        """Explicitly supplied branch overrides the project default."""
        cli = gitsr.GitSR()
        self._setup_project(cli)
        cache_dir = isolated_config / "cache" / "proj"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cli.sync("proj", branch="develop")
        calls_flat = [arg for c in mock_run.call_args_list for arg in c[0][0]]
        assert any("develop" in a for a in calls_flat)

    def test_sync_unknown_project(self, capsys):
        cli = gitsr.GitSR()
        cli.sync("ghost")
        out = capsys.readouterr().out
        assert "not found" in out

    def test_sync_missing_remote(self, capsys):
        cli = gitsr.GitSR()
        cli.setup("p", "https://github.com/u/r.git", "https://b.org/u/r.git")
        cli.sync("p", target="nonexistent")
        out = capsys.readouterr().out
        assert "not configured" in out

    @patch("gitsr._run_git", side_effect=subprocess.CalledProcessError(1, "git", stderr="auth error"))
    def test_sync_git_error_exits(self, mock_run):
        cli = gitsr.GitSR()
        cli.setup("p", "https://github.com/u/r.git", "https://b.org/u/r.git")
        with pytest.raises(SystemExit):
            cli.sync("p")
