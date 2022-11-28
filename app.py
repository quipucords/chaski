import io
import json
import os
import re
import tarfile
from pathlib import Path

import requests
import toml
import typer
import yaml
from rich.console import Console

console = Console()
app = typer.Typer(no_args_is_help=True)

CARGO_LOCAL_CACHE = ".cargo/registry/cache/github.com-1ecc6299db9ec823"
CARGO_PKG_PREFIX = "rust-crate-"
CONTAINER_YAML = "container.yaml"
QUIPUCORDS_REQUIREMENTS_URL = "https://raw.githubusercontent.com/%s/%s/requirements.txt"
QUIPUCORDS_SERVER = "quipucords-server"
SOURCES_VERSION_YAML = "sources-version.yaml"

distgit_path_arg = typer.Argument(
    ...,
    help="path to folder where discovery-server is cloned",
    metavar="distgit-path",
    show_default=False,
)


cryptography_version_arg = typer.Argument(
    None,
    help="cryptography version (format: X.Y.Z).",
    metavar="cryptography-version",
    show_default=False,
)


@app.command()
def update_remote_sources(distgit_path: Path = distgit_path_arg):
    """
    Update remote-sources on 'container.yaml' based on 'sources-version.yaml'.

    If changes are detected to quipucords-server, than this will also invoke
    the following subcommands:

    - update-quipucords-sha

    - update-cryptography dependencies when a version change is detected.

    Check --help method of these subcommands for more info.
    """
    distgit_path = distgit_path.absolute()
    os.chdir(distgit_path)
    repo_regex = re.compile(r"([-\w]+)/([-\w\.]+).git")

    versions_path = Path(SOURCES_VERSION_YAML)
    container_path = Path(CONTAINER_YAML)
    commitsh_map = yaml.safe_load(versions_path.open())
    container_data = yaml.safe_load(container_path.open())
    perform_update = False
    for source in container_data["remote_sources"]:
        try:
            commitsh = commitsh_map[source["name"]]
        except KeyError:
            continue
        user, repository = repo_regex.search(source["remote_source"]["repo"]).groups()
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
        yaml.dump(container_data, container_path.open("w"))
        _print_downstream_instructions(distgit_path)
    else:
        console.print("Nothing to update. Go treat yourself with some coffee :coffee:")


def _print_downstream_instructions(distgit_path: Path):
    """Remind the user commands for downstream building."""
    RHPKG_COMMAND = "rhpkg container-build --target=<target-build>"
    RHPKG_EXAMPLE = (
        "rhpkg container-build --target=discovery-1.1-rhel-8-containers-candidate"
    )
    SCRATCH_OPTION = "--scratch if this is still in development"
    console.print("You are almost ready for a downstream build! :ship:")
    console.print(
        f"Check the changes on [green]{distgit_path}[/green], commit"
        " and push :rocket:"
    )
    console.print(f"Then run [green]{RHPKG_COMMAND}[/green] \[{SCRATCH_OPTION}]")
    console.print(f"Example: [green]{RHPKG_EXAMPLE}[/green] :coffee:")


@app.command()
def update_quipucords_hash(distgit_path: Path = distgit_path_arg):
    """Update QUIPUCORDS_COMMIT ARG on Dockerfile."""
    console.print("Forcibly updating QUIPUCORDS_COMMIT ARG on Dockerfile")
    distgit_path = distgit_path.absolute()
    os.chdir(distgit_path)
    source = _get_quipucords_source()
    _update_quipucords_sha(source["remote_source"]["ref"])


def _get_quipucords_source():
    container_path = Path(CONTAINER_YAML)
    container_data = yaml.safe_load(container_path.open())
    for source in container_data["remote_sources"]:
        if source["name"] == QUIPUCORDS_SERVER:
            return source
    assert False


@app.command()
def update_cryptography(
    distgit_path: Path = distgit_path_arg,
    cryptography_version: str = cryptography_version_arg,
):
    """Update python-cryptography lib.

    Defaults to the version defined on current quipucords-server.
    """
    distgit_path = distgit_path.absolute()
    os.chdir(distgit_path)
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
    cargo_sources = []
    if tuple(cryptography_version.split(".")) < ("3", "4", "0"):
        console.print(
            f"cryptographt version {cryptography_version} don't have rust dependencies"
        )
    else:
        cryptography_cargo_lock_url = (
            "https://raw.githubusercontent.com/pyca/cryptography/%s/src/rust/Cargo.lock"
        )
        console.print(
            f"Updating rust dependencies for cryptography={cryptography_version}"
        )
        cryptography_rust_url = cryptography_cargo_lock_url % cryptography_version
        cryptography_cargo_lock = requests.get(cryptography_rust_url).content.decode()
        cargo_lock_dict = toml.loads(cryptography_cargo_lock)
        ignored_cargo = [
            "cryptography-rust",
            "winapi-i686-pc-windows-gnu",
            "winapi-x86_64-pc-windows-gnu",
        ]
        for dep_dict in cargo_lock_dict["package"]:
            pkg_name = dep_dict["name"]
            version = dep_dict["version"]
            if pkg_name in ignored_cargo:
                continue
            _check_crates_io_source(dep_dict)
            repo, git_sha = _get_crate_repo_and_sha(pkg_name, version)
            cargo_sources.append(
                {
                    "name": f"rust-crate-{pkg_name}-{version.replace('.', '_')}",
                    "remote_source": {"pkg_managers": [], "ref": git_sha, "repo": repo},
                }
            )
    container_path = Path(CONTAINER_YAML)
    container_data = yaml.safe_load(container_path.open())
    sources = [
        source
        for source in container_data["remote_sources"]
        if not source["name"].startswith("rust-crate-")
    ]
    container_data["remote_sources"] = sources + cargo_sources
    yaml.dump(container_data, container_path.open("w"))


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


def _get_crate_repo_and_sha(crate_name, version):
    CRATES_IO_PACKAGE_URL = "https://crates.io/api/v1/crates/%s"
    response = requests.get(CRATES_IO_PACKAGE_URL % crate_name)
    if not response.ok:
        console.print(f"Failed to find crate {crate_name} in 'crates.io'", style="red")
        raise typer.Abort()
    data = response.json()
    repository = data["crate"]["repository"]
    if not repository.endswith(".git"):
        repository += ".git"
    crate = _get_crate(crate_name, version)
    with tarfile.open(fileobj=crate.open("rb")) as tarball:
        vcs_info_file = f"{crate_name}-{version}/.cargo_vcs_info.json"
        try:
            vcs_info = json.loads(tarball.extractfile(vcs_info_file).read())
            git_sha = vcs_info["git"]["sha1"]
        except KeyError:
            console.print(f"Unable to dectect git sha for {crate_name}")
            git_sha = "unknown"
    return repository, git_sha


def _get_crate(crate_name, version) -> Path:
    CRATES_IO_DOWNLOAD_URL = "https://crates.io/api/v1/crates/%s/%s/download"
    cargo_cache_dir = Path.home() / CARGO_LOCAL_CACHE
    cargo_cache_dir.mkdir(exist_ok=True)
    crate = cargo_cache_dir / f"{crate_name}-{version}.crate"
    pretty_crate = f"[magenta]{crate_name}[/magenta]=[green]{version}[/green]"
    if crate.exists():
        console.print(f"Using cached version for crate {pretty_crate}")
        return crate
    console.print(f"Downloading crate {pretty_crate}")
    dl_resp = requests.get(CRATES_IO_DOWNLOAD_URL % (crate_name, version))
    if not dl_resp.ok:
        console.print(f"Failed to find crate {crate_name} in 'crates.io'", style="red")
        raise typer.Abort()
    crate.write_bytes(dl_resp.content)
    return crate


if __name__ == "__main__":
    app()
