import os
import re
from pathlib import Path

import requests
import typer
import yaml
from rich.console import Console

console = Console()
app = typer.Typer()


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
            source["remote_source"]["ref"] = commit_sha
            perform_update = True

    if perform_update:
        console.print("Updating container.yaml")
        console.print(container_data)
        yaml.dump(container_data, container_path.open("w"))


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


if __name__ == "__main__":
    app()
