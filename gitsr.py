#!/usr/bin/env python3
"""gitsr - Git Sync Remote: Sync git repositories between different remotes."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import fire

CONFIG_DIR = Path.home() / ".gitsr"
CONFIG_FILE = CONFIG_DIR / "config.json"
SECRETS_FILENAME = "secrets.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _load_config():
    if not CONFIG_FILE.exists():
        return {"projects": {}}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def _save_config(config):
    _ensure_config_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def _secrets_file(project_name):
    return CONFIG_DIR / project_name / SECRETS_FILENAME


def _load_secrets(project_name):
    sf = _secrets_file(project_name)
    if not sf.exists():
        return {}
    with open(sf, "r") as f:
        return json.load(f)


def _save_secrets(project_name, secrets):
    secrets_dir = CONFIG_DIR / project_name
    secrets_dir.mkdir(parents=True, exist_ok=True)
    sf = _secrets_file(project_name)
    with open(sf, "w") as f:
        json.dump(secrets, f, indent=2)
    os.chmod(sf, 0o600)


def _inject_token(url, token):
    """Inject *token* into an HTTPS remote *url* for authentication.

    Supports GitHub, Bitbucket and generic HTTPS hosts.
    """
    if not token or not url.startswith("https://"):
        return url
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if hostname == "github.com" or hostname.endswith(".github.com"):
        return re.sub(r"^https://", f"https://{token}@", url, count=1)
    if hostname == "bitbucket.org" or hostname.endswith(".bitbucket.org"):
        return re.sub(r"^https://", f"https://x-token-auth:{token}@", url, count=1)
    # Generic OAuth2 bearer style
    return re.sub(r"^https://", f"https://oauth2:{token}@", url, count=1)


def _auth_url(project_name, remote_name, url):
    """Return *url* with an embedded token when one is stored for *remote_name*."""
    secrets = _load_secrets(project_name)
    token = secrets.get(f"{remote_name}_token") or secrets.get("token")
    return _inject_token(url, token)


def _run_git(cmd):
    """Run *cmd* and stream its output; raise :class:`subprocess.CalledProcessError` on failure."""
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end="")
    return result


# ---------------------------------------------------------------------------
# CLI class
# ---------------------------------------------------------------------------


class GitSR:
    """gitsr - Git Sync Remote

    Keep two (or more) git remotes in sync with minimal effort.
    Typical use-case: mirror a GitHub repository to Bitbucket on a cron job.

    Quick start::

        gitsr setup myproject \\
            https://github.com/user/repo.git \\
            https://bitbucket.org/user/repo.git

        # store tokens for private repos
        gitsr set_token myproject ghp_xxxx --remote github
        gitsr set_token myproject APP_PASS --remote bitbucket

        # sync (add to cron for automation)
        gitsr sync myproject
    """

    # ------------------------------------------------------------------
    # Project management
    # ------------------------------------------------------------------

    def setup(
        self,
        name,
        source_url,
        target_url,
        source_name="source",
        target_name="target",
        branch="main",
    ):
        """Set up a new project with source and target remotes.

        Creates a project entry in the global config (~/.gitsr/config.json)
        that records the two remote URLs, their aliases and the default branch
        used for syncing.

        Args:
            name:        Unique project identifier.
            source_url:  HTTPS or SSH URL of the *source* repository.
            target_url:  HTTPS or SSH URL of the *target* repository.
            source_name: Alias for the source remote (default: ``source``).
            target_name: Alias for the target remote (default: ``target``).
            branch:      Default branch to sync (default: ``main``).

        Example::

            gitsr setup myproject \\
                https://github.com/user/repo.git \\
                https://bitbucket.org/user/repo.git \\
                --branch develop
        """
        config = _load_config()
        if name in config["projects"]:
            print(
                f"Project '{name}' already exists. "
                "Use 'add_remote' to add more remotes or 'remove' to delete it first."
            )
            return

        config["projects"][name] = {
            "remotes": {
                source_name: source_url,
                target_name: target_url,
            },
            "default_source": source_name,
            "default_target": target_name,
            "default_branch": branch,
        }
        _save_config(config)
        print(f"Project '{name}' configured.")
        print(f"  {source_name}: {source_url}")
        print(f"  {target_name}: {target_url}")
        print(f"  Default branch: {branch}")
        print(
            f"\nFor private repos store your token with:\n"
            f"  gitsr set_token {name} YOUR_TOKEN --remote {source_name}"
        )

    def add_remote(self, project, name, url, set_as_target=False):
        """Add an additional remote to an existing project.

        Args:
            project:       Project name.
            name:          Alias for the new remote.
            url:           HTTPS or SSH URL of the remote repository.
            set_as_target: Also set this remote as the default sync target
                           (default: ``False``).

        Example::

            gitsr add_remote myproject gitlab https://gitlab.com/user/repo.git
            gitsr add_remote myproject mirror https://mirror.example.com/repo.git --set_as_target
        """
        config = _load_config()
        if project not in config["projects"]:
            print(f"Project '{project}' not found. Run 'setup' first.")
            return

        config["projects"][project]["remotes"][name] = url
        if set_as_target:
            config["projects"][project]["default_target"] = name
        _save_config(config)
        print(f"Remote '{name}' added to '{project}': {url}")
        if set_as_target:
            print(f"  Set as default target.")

    def remove(self, project):
        """Remove a project and its cached clone (does not touch the actual repos).

        Args:
            project: Project name to remove.
        """
        config = _load_config()
        if project not in config["projects"]:
            print(f"Project '{project}' not found.")
            return

        del config["projects"][project]
        _save_config(config)

        cache_dir = CONFIG_DIR / "cache" / project
        if cache_dir.exists():
            shutil.rmtree(cache_dir)

        print(f"Project '{project}' removed.")

    def set_defaults(self, project, source=None, target=None):
        """Update the default source and/or target remote for a project.

        Args:
            project: Project name.
            source:  Remote alias to use as the default source.
            target:  Remote alias to use as the default target.

        Example::

            gitsr set_defaults myproject --source github --target bitbucket
        """
        config = _load_config()
        if project not in config["projects"]:
            print(f"Project '{project}' not found.")
            return

        proj = config["projects"][project]
        if source:
            if source not in proj["remotes"]:
                print(f"Remote '{source}' not configured in '{project}'.")
                return
            proj["default_source"] = source
        if target:
            if target not in proj["remotes"]:
                print(f"Remote '{target}' not configured in '{project}'.")
                return
            proj["default_target"] = target

        _save_config(config)
        if source:
            print(f"  Default source: {source}")
        if target:
            print(f"  Default target: {target}")

    def set_default_branch(self, project, branch):
        """Set the default sync branch for a project.

        Args:
            project: Project name.
            branch:  Branch name to use as the default.

        Example::

            gitsr set_default_branch myproject develop
        """
        config = _load_config()
        if project not in config["projects"]:
            print(f"Project '{project}' not found.")
            return

        config["projects"][project]["default_branch"] = branch
        _save_config(config)
        print(f"Default branch for '{project}' set to '{branch}'.")

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def set_token(self, project, token, remote=None):
        """Store an authentication token for a project's remote.

        Tokens are written to ``~/.gitsr/<project>/secrets.json`` with
        file permissions restricted to the current user (``0o600``).  They
        are injected automatically into remote URLs at sync time.

        Args:
            project: Project name.
            token:   Personal access token or app password.
            remote:  Remote alias the token belongs to.  When omitted the
                     token is stored as the generic fallback for the project.

        Example::

            gitsr set_token myproject ghp_xxxxxxxxxxxx --remote github
            gitsr set_token myproject APP_PASSWORD      --remote bitbucket
        """
        config = _load_config()
        if project not in config["projects"]:
            print(f"Project '{project}' not found. Run 'setup' first.")
            return

        secrets = _load_secrets(project)
        key = f"{remote}_token" if remote else "token"
        secrets[key] = token
        _save_secrets(project, secrets)
        print(f"Token stored for project '{project}' (key: '{key}').")

    # ------------------------------------------------------------------
    # Listing / info
    # ------------------------------------------------------------------

    def list(self):
        """List all configured projects with their default sync direction."""
        config = _load_config()
        if not config["projects"]:
            print("No projects configured.  Run 'setup' to add one.")
            return

        print("Configured projects:")
        for name, proj in config["projects"].items():
            src = proj["default_source"]
            tgt = proj["default_target"]
            branch = proj["default_branch"]
            src_url = proj["remotes"].get(src, "N/A")
            tgt_url = proj["remotes"].get(tgt, "N/A")
            print(f"\n  {name}")
            print(f"    branch : {branch}")
            print(f"    source : {src}  →  {src_url}")
            print(f"    target : {tgt}  →  {tgt_url}")
            extra = [k for k in proj["remotes"] if k not in (src, tgt)]
            if extra:
                print(f"    others : {', '.join(extra)}")

    def info(self, project):
        """Show full configuration details for a single project.

        Args:
            project: Project name.
        """
        config = _load_config()
        if project not in config["projects"]:
            print(f"Project '{project}' not found.")
            return

        proj = config["projects"][project]
        print(f"Project : {project}")
        print(f"  Default branch : {proj['default_branch']}")
        print(f"  Default source : {proj['default_source']}")
        print(f"  Default target : {proj['default_target']}")
        print("  Remotes:")
        for rname, rurl in proj["remotes"].items():
            print(f"    {rname}: {rurl}")

        secrets = _load_secrets(project)
        if secrets:
            print(f"  Stored token keys: {', '.join(secrets.keys())}")
        else:
            print("  Stored token keys: (none)")

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync(self, project, branch=None, source=None, target=None, force=False):
        """Sync a branch from the source remote to the target remote.

        Fetches *branch* from the source remote into a local bare-clone
        cache (``~/.gitsr/cache/<project>/``) and then pushes it to the
        target remote.  Authentication tokens are injected automatically
        when they have been stored with ``set_token``.

        The bare-clone cache is created on the first run and reused on
        subsequent runs, making repeated syncs (e.g. from a cron job) fast.

        Args:
            project: Project name to sync.
            branch:  Branch to sync (uses the project default when omitted).
            source:  Source remote alias (uses the project default when omitted).
            target:  Target remote alias (uses the project default when omitted).
            force:   Force-push to the target remote (default: ``False``).

        Example::

            gitsr sync myproject
            gitsr sync myproject --branch develop
            gitsr sync myproject --source github --target bitbucket --force
        """
        config = _load_config()
        if project not in config["projects"]:
            print(f"Project '{project}' not found. Run 'setup' first.")
            return

        proj = config["projects"][project]
        branch = branch or proj["default_branch"]
        source_name = source or proj["default_source"]
        target_name = target or proj["default_target"]

        if source_name not in proj["remotes"]:
            print(f"Remote '{source_name}' not configured in project '{project}'.")
            return
        if target_name not in proj["remotes"]:
            print(f"Remote '{target_name}' not configured in project '{project}'.")
            return

        source_url = _auth_url(project, source_name, proj["remotes"][source_name])
        target_url = _auth_url(project, target_name, proj["remotes"][target_name])

        cache_dir = CONFIG_DIR / "cache" / project

        print(
            f"Syncing '{project}': {source_name} → {target_name}  (branch: {branch})"
        )

        try:
            if not cache_dir.exists():
                print(f"  Initializing local cache…")
                _run_git(["git", "clone", "--bare", source_url, str(cache_dir)])
            else:
                print(f"  Fetching from {source_name}…")
                _run_git(
                    [
                        "git",
                        "-C",
                        str(cache_dir),
                        "fetch",
                        source_url,
                        f"refs/heads/{branch}:refs/heads/{branch}",
                    ]
                )

            print(f"  Pushing to {target_name}…")
            push_ref = f"refs/heads/{branch}:refs/heads/{branch}"
            push_cmd = [
                "git",
                "-C",
                str(cache_dir),
                "push",
                target_url,
                push_ref,
            ]
            if force:
                push_cmd.append("--force")
            _run_git(push_cmd)

            print(f"  ✓ Branch '{branch}' synced successfully.")
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else ""
            print(f"  ✗ git error: {stderr}", file=sys.stderr)
            sys.exit(1)


def main():
    fire.Fire(GitSR, name="gitsr")


if __name__ == "__main__":
    main()
