import os
import re
from pathlib import Path

import requests
import typer
import yaml
from rich.console import Console

console = Console()
app = typer.Typer(no_args_is_help=True)

QUIPUCORDS_SERVER = "quipucords-server"


@app.command()
def update_remote_sources(downstream_path: Path):
    os.chdir(downstream_path)
    repo_regex = re.compile(r"([-\w]+)/([-\w]+).git")

    versions_path = Path("sources-version.yaml")
    container_path = Path("container.yaml")
    commitsh_map = yaml.safe_load(versions_path.open())
    container_data = yaml.safe_load(container_path.open())
    perform_update = False
    for source in container_data["remote_sources"]:
        user, repository = repo_regex.search(source["remote_source"]["repo"]).groups()
        commitsh = commitsh_map[source["name"]]
        commit_sha = _get_commit_sha(user, repository, commitsh)
        if commit_sha == source["remote_source"]["ref"]:
            console.print(f"\[{source['name']}] Nothing to update")
        else:
            console.print(f"\[{source['name']}] updating ref to '{commit_sha}'")
            _side_effects(source, commit_sha)
            source["remote_source"]["ref"] = commit_sha
            perform_update = True

    if perform_update:
        console.print("Updating container.yaml")
        console.print(container_data)
        yaml.dump(container_data, container_path.open("w"))


@app.command()
def update_quipucords_hash(downstream_path: Path):
    console.print("Forcibly updating QUIPUCORDS_COMMIT ARG on Dockerfile")
    os.chdir(downstream_path)
    container_path = Path("container.yaml")
    container_data = yaml.safe_load(container_path.open())
    for source in container_data["remote_sources"]:
        if source["name"] == QUIPUCORDS_SERVER:
            break
    _update_quipucords_sha(source["remote_source"]["ref"])


def _get_commit_sha(user, repository, commitsh):
    with console.status(f"Resolving commit sha for {repository}:{commitsh}..."):
        gh_url = f"https://api.github.com/repos/{user}/{repository}/commits/{commitsh}"
        gh_response = requests.get(gh_url)
    if gh_response.ok:
        return gh_response.json()["sha"]
    console.print(
        f"[red]Error retrieving data[/red] from [link={gh_url}]github api[/link]"
    )
    console.print("Status code:", gh_response.status_code)
    raise typer.Abort()


def _side_effects(source, new_commit):
    if not source["name"] == QUIPUCORDS_SERVER:
        return
    _update_quipucords_sha(new_commit)


def _update_quipucords_sha(new_commit):
    console.print("Updating Dockerfile ARG QUIPUCORDS_COMMIT")
    dockerfile = Path("Dockerfile")
    updated_dockerfile = re.sub(
        r"ARG QUIPUCORDS_COMMIT=.*",
        f'ARG QUIPUCORDS_COMMIT="{new_commit}"',
        dockerfile.read_text(),
    )
    dockerfile.write_text(updated_dockerfile)


if __name__ == "__main__":
    app()
