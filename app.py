import os
import re
from pathlib import Path

import requests
import toml
import typer
import yaml
from rich.console import Console

console = Console()
app = typer.Typer(no_args_is_help=True)

QUIPUCORDS_REQUIREMENTS_URL = "https://raw.githubusercontent.com/%s/%s/requirements.txt"
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
    source = _get_quipucords_source()
    _update_quipucords_sha(source["remote_source"]["ref"])


def _get_quipucords_source():
    container_path = Path("container.yaml")
    container_data = yaml.safe_load(container_path.open())
    for source in container_data["remote_sources"]:
        if source["name"] == QUIPUCORDS_SERVER:
            return source
    assert False


@app.command()
def update_cryptography(downstream_path: Path, cryptography_version: str = None):
    os.chdir(downstream_path)
    if not cryptography_version:
        console.print("cryptography version not specified.")
        console.print(f"Using the one from {QUIPUCORDS_SERVER} source.")
        source = _get_quipucords_source()
        quipucords_repo = _get_repo_from_source(source)
        cryptography_version = _get_cryptography_version(
            quipucords_repo,
            source["remote_source"]["ref"],
        )
    _update_cryptography(cryptography_version)


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
    _handle_cryptography(source, new_commit)


def _handle_cryptography(source, new_commit):
    console.print(
        "Checking if quipucords subdependency 'cryptography' needs to be updated."
    )
    quipucords_repo = _get_repo_from_source(source)
    old_commit = source["remote_source"]["ref"]
    old_version = _get_cryptography_version(quipucords_repo, old_commit)
    new_version = _get_cryptography_version(quipucords_repo, new_commit)
    if old_version != new_version:
        console.print(f"Updating cryptography lib ({old_version} -> {new_version}).")
        _update_cryptography(new_version)
    else:
        console.print(f"cryptography version remains the same ({old_version}).")


def _update_quipucords_sha(new_commit):
    console.print("Updating Dockerfile ARG QUIPUCORDS_COMMIT")
    dockerfile = Path("Dockerfile")
    updated_dockerfile = re.sub(
        r"ARG QUIPUCORDS_COMMIT=.*",
        f'ARG QUIPUCORDS_COMMIT="{new_commit}"',
        dockerfile.read_text(),
    )
    dockerfile.write_text(updated_dockerfile)


def _get_repo_from_source(source):
    return source["remote_source"]["repo"].split("/", 3)[-1].strip(".git")


def _get_cryptography_version(quipucords_repo, quipucords_sha):
    requirements_url = QUIPUCORDS_REQUIREMENTS_URL % (quipucords_repo, quipucords_sha)
    requirements_content = requests.get(requirements_url).content.decode()
    match = re.search(r"cryptography==([\d\.]+)", requirements_content)
    return match.group(1)


def _update_cryptography(cryptography_version):
    console.print(f"Updating rust dependencies for cryptography={cryptography_version}")
    CRATES_IO_DOWNLOAD_URL = "https://crates.io/api/v1/crates/%s/%s/download"
    CRYPTOGRAPHY_CARGO_LOCK_URL = (
        "https://raw.githubusercontent.com/pyca/cryptography/%s/src/rust/Cargo.lock"
    )
    # because cachito doesn't support rust, reference rust deps in a different method
    # https://osbs.readthedocs.io/en/latest/users.html#fetch-artifacts-url-yaml
    fetch_artifacts_yaml = Path("fetch-artifacts-url.yaml")
    cryptography_rust_url = CRYPTOGRAPHY_CARGO_LOCK_URL % cryptography_version
    cryptography_cargo_lock = requests.get(cryptography_rust_url).content.decode()
    cargo_lock_dict = toml.loads(cryptography_cargo_lock)
    artifacts_url = []
    for dep_dict in cargo_lock_dict["package"]:
        if dep_dict["name"] == "cryptography-rust":
            continue
        _check_crates_io_source(dep_dict)
        pkg_name = dep_dict["name"]
        version = dep_dict["version"]
        pkg_url = CRATES_IO_DOWNLOAD_URL % (pkg_name.lower(), version)
        entry = {
            "url": pkg_url,
            "source-url": pkg_url,
            "sha256": dep_dict["checksum"],
            "source-sha256": dep_dict["checksum"],
            "target": f"rust/{pkg_name}-{version}.crate",
        }
        artifacts_url.append(entry)
    yaml.dump(artifacts_url, fetch_artifacts_yaml.open("w"))


def _check_crates_io_source(dep_dict):
    CRATES_IO_SOURCE = "registry+https://github.com/rust-lang/crates.io-index"
    CHASKI_URL = "https://github.com/quipucords/chaski"
    if dep_dict.get("source") != CRATES_IO_SOURCE:
        console.print(
            "[bold red]ERROR:[/bold red] chaski don't know how to handle rust"
            " dependencies that are not from crates.io!"
        )
        console.print(
            f"It's probably time for YOU to [link={CHASKI_URL}]hack it[/link]."
        )
        console.print("Offending rust dependency:")
        console.print_json(data=dep_dict)
        raise typer.Abort()


if __name__ == "__main__":
    app()
