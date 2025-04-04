"""Chaski CLI - a helper for downstream builds of quipucords and qpc."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory

import requests
import typer
import yaml
from rich.console import Console

console = Console()
app = typer.Typer(no_args_is_help=True)

CONTAINER_YAML = "container.yaml"
QUIPUCORDS_FILE_URL = "https://raw.githubusercontent.com/%s/%s/%s"
QUIPUCORDS_SERVER = "quipucords-server"
QPC = "qpc"
SOURCES_VERSION_YAML = "sources-version.yaml"
DEPENDENCIES_FOLDER = "dependencies"
RUST_SOURCE_URL = {
    "cryptography": "https://github.com/pyca/cryptography/archive/refs/tags/%s.tar.gz",
    "bcrypt": "https://github.com/pyca/bcrypt/archive/refs/tags/%s.tar.gz",
    "maturin": "https://github.com/PyO3/maturin/archive/refs/tags/v%s.tar.gz",
    "rpds-py": "https://github.com/crate-py/rpds/archive/refs/tags/v%s.tar.gz",
}
RUST_CARGO_PATH = {
    "cryptography": "src/rust/Cargo.toml",
    "bcrypt": "src/_bcrypt/Cargo.toml",
    "maturin": "Cargo.toml",
    "rpds-py": "Cargo.toml",
}
VENDOR_FILE = "cargo_vendor.tar.gz"

distgit_path_arg = typer.Argument(
    ...,
    help="path to folder where discovery-server is cloned",
    metavar="distgit-path",
    show_default=False,
)


package_version_arg = typer.Argument(
    None,
    help="package version (format: X.Y.Z).",
    show_default=False,
)


@app.command()
def update_remote_sources(distgit_path: Path = distgit_path_arg):
    """
    Update remote-sources on 'container.yaml' based on 'sources-version.yaml'.

    If changes are detected to commit refs, then this will also invoke
    the following subcommands:

    - update_dockerfile
    - update_rust_deps dependencies when a version change is detected.

    Check --help method of these subcommands for more info.
    """
    distgit_path = distgit_path.absolute()
    os.chdir(distgit_path)
    repo_regex = re.compile(r"([-\w]+)/([-\w\.]+).git")

    versions_path = Path(SOURCES_VERSION_YAML)
    container_path = Path(CONTAINER_YAML)
    committish_map = yaml.safe_load(versions_path.open())
    container_data = yaml.safe_load(container_path.open())
    perform_update = False
    for source in container_data["remote_sources"]:
        try:
            committish = committish_map[source["name"]]
        except KeyError:
            continue
        user, repository = repo_regex.search(source["remote_source"]["repo"]).groups()
        commit_sha = _get_commit_sha(user, repository, committish)
        if commit_sha == source["remote_source"]["ref"]:
            console.print(f"\\[{source['name']}] Nothing to update")
        else:
            console.print(f"\\[{source['name']}] updating ref to '{commit_sha}'")
            original_commit_sha = source["remote_source"]["ref"]
            source["remote_source"]["ref"] = commit_sha
            _side_effects(source, committish, original_commit_sha)
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
        "rhpkg container-build --target=discovery-1-rhel-9-containers-candidate"
    )
    SCRATCH_OPTION = "--scratch if this is still in development"
    console.print("You are almost ready for a downstream build! :ship:")
    console.print(
        f"Check the changes on [green]{distgit_path}[/green], commit"
        " and push :rocket:"
    )
    console.print(f"Then run [green]{RHPKG_COMMAND}[/green] \\[{SCRATCH_OPTION}]")
    console.print(f"Example: [green]{RHPKG_EXAMPLE}[/green] :coffee:")


@app.command()
def update_dockerfile(distgit_path: Path = distgit_path_arg):
    """Update discovery-server Dockerfile."""
    console.print("Forcibly updating Dockerfile")
    distgit_path = distgit_path.absolute()
    os.chdir(distgit_path)
    source = _get_source_by_name(QUIPUCORDS_SERVER)
    quipucords_version = _get_quipucords_version()
    _update_dockerfile_quipucords(source["remote_source"]["ref"], quipucords_version)
    source = _get_source_by_name(QPC)
    _update_dockerfile_qpc(source["remote_source"]["ref"])


def _get_quipucords_version():
    committish_map = yaml.safe_load(Path(SOURCES_VERSION_YAML).open())
    quipucords_version = committish_map["quipucords-server"]
    return quipucords_version


def _get_source_by_name(name):
    container_path = Path(CONTAINER_YAML)
    container_data = yaml.safe_load(container_path.open())
    for source in container_data["remote_sources"]:
        if source["name"] == name:
            return source
    assert False


@app.command()
def update_rust_deps(distgit_path: Path = distgit_path_arg):
    """Update rust dependencies."""
    distgit_path = distgit_path.absolute()
    os.chdir(distgit_path)
    source = _get_source_by_name(QUIPUCORDS_SERVER)
    quipucords_repo = _get_repo_from_source(source)
    versions = _get_rust_deps_versions(
        quipucords_repo,
        source["remote_source"]["ref"],
    )
    console.print(f"Using the following libs: {versions}")
    _update_rust_deps(versions)


def _get_commit_sha(user, repository, committish):
    with console.status(f"Resolving commit sha for {repository}:{committish}..."):
        gh_url = (
            f"https://api.github.com/repos/{user}/{repository}/commits/{committish}"
        )
        gh_response = requests.get(gh_url)
    if gh_response.ok:
        return gh_response.json()["sha"]
    console.print(
        f"[red]Error retrieving data[/red] from [link={gh_url}]github api[/link]"
    )
    console.print("Status code:", gh_response.status_code)
    raise typer.Abort()


def _side_effects(
    source: dict, committish: str | None = None, original_commit_sha: str | None = None
):
    """
    Side effects for quipucords-server and qpc.

    :param source: dict representing a "source" from container.yaml.
    :param committish: commit-ish (using git jargon [1]), IoW, a commit, tag, branch name,
        etc. Preferably it should should be a tag formatted following semantic versioning
        (X.Y.Z).
    :param: original_commit_sha: commit sha of the original version of given source; for now this is
        only mandatory for quipucords.

    [1]: https://git-scm.com/docs/gitglossary#Documentation/gitglossary.txt-aiddefcommit-ishacommit-ishalsocommittish
    """  # noqa: E501
    if source["name"] == QPC:
        new_commit = source["remote_source"]["ref"]
        _update_dockerfile_qpc(new_commit)
    elif source["name"] == QUIPUCORDS_SERVER:
        new_commit = source["remote_source"]["ref"]
        _update_dockerfile_quipucords(new_commit, committish)
        _update_rust_deps_if_required(source, new_commit, original_commit_sha)


def _update_rust_deps_if_required(source, new_commit, old_commit):
    console.print("Checking if rust :crab: dependencies are updated.")
    quipucords_repo = _get_repo_from_source(source)
    old_versions = _get_rust_deps_versions(
        quipucords_repo, old_commit, ignore_missing_deps=True
    )
    new_versions = _get_rust_deps_versions(quipucords_repo, new_commit)
    if old_versions != new_versions:
        console.print(
            f"Updating rust :crab: dependencies ({old_versions} -> {new_versions})."
        )
        _update_rust_deps(new_versions)
    else:
        console.print(f"rust :crab: libraries remain the same ({old_versions}).")


def _update_dockerfile_quipucords(new_commit, committish):
    dockerfile = Path("Dockerfile")
    console.print("Updating Dockerfile ARG 'QUIPUCORDS_COMMIT'")
    updated_dockerfile = re.sub(
        r"ARG QUIPUCORDS_COMMIT=.*",
        f'ARG QUIPUCORDS_COMMIT="{new_commit}"',
        dockerfile.read_text(),
    )
    if re.match(r"\d+\.\d+\.\d+", committish):
        console.print(f"Updating Dockerfile ARG 'DISCOVERY_VERSION' to '{committish}'")
        updated_dockerfile = re.sub(
            r"ARG DISCOVERY_VERSION=.*",
            f'ARG DISCOVERY_VERSION="{committish}"',
            updated_dockerfile,
        )
    else:
        console.print(
            f":warning: {committish=} is not formatted as a version :warning:"
        )
        console.print(":warning: 'DISCOVERY_VERSION' ARG won't be updated :warning:")
    dockerfile.write_text(updated_dockerfile)


def _update_dockerfile_qpc(new_commit):
    dockerfile = Path("Dockerfile")
    console.print("Updating Dockerfile ARG 'QPC_COMMIT'")
    updated_dockerfile = re.sub(
        r"ARG QPC_COMMIT=.*", f'ARG QPC_COMMIT="{new_commit}"', dockerfile.read_text()
    )
    dockerfile.write_text(updated_dockerfile)


def _get_repo_from_source(source):
    return source["remote_source"]["repo"].split("/", 3)[-1].strip(".git")


def _get_rust_deps_versions(quipucords_repo, quipucords_sha, ignore_missing_deps=False):
    requirements_content = ""
    for file in ["lockfiles/requirements.txt", "lockfiles/requirements-build.txt"]:
        requirements_url = QUIPUCORDS_FILE_URL % (quipucords_repo, quipucords_sha, file)
        requirements_content += requests.get(requirements_url).content.decode()
    versions = {}
    for dependency in RUST_CARGO_PATH.keys():
        match = re.search(rf"{dependency}==([\d\.]+)", requirements_content)
        if not match:
            if not ignore_missing_deps:
                message = f"couldn't find rust dependency '{dependency}'"
                console.print(f":warning: [red]{message}[/red] :warning:")
                console.print(
                    "if you are not building an older version of discovery, "
                    "please check quipucords dependencies and update chaski",
                    style="red",
                )
            continue
        versions[dependency] = match.group(1)
    return versions


def cargo(cmd, manifest, *extra):
    """Execute rust package manager (cargo)."""
    args = [shutil.which("cargo"), cmd, f"--manifest-path={manifest}"]
    args.extend(str(e) for e in extra)
    console.print(" ".join(args))
    return subprocess.check_call(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def rhpkg(cmd, *args):
    """Execute rhpkg CLI."""
    args = [shutil.which("rhpkg"), cmd, *[str(a) for a in args]]
    console.print(" ".join(args))
    return subprocess.check_call(args, stdout=sys.stderr, stderr=sys.stderr)


def _update_rust_deps(versions: dict):
    cargo_manifests = []
    for dep, version in versions.items():
        dependency_path = _get_dependency(dep, version)
        manifest_path = dependency_path / RUST_CARGO_PATH[dep]
        cargo_manifests.append(manifest_path)

    vendor_path = Path(DEPENDENCIES_FOLDER) / "vendor"
    shutil.rmtree(vendor_path, ignore_errors=True)
    vendor_tarball = Path(DEPENDENCIES_FOLDER) / VENDOR_FILE
    vendor_tarball.unlink(missing_ok=True)

    if len(cargo_manifests) == 0:
        console.print("Nothing to update.")
        return None
    with console.status("Vendoring rust dependencies..."):
        if len(cargo_manifests) == 1:
            cargo("vendor", cargo_manifests[0], vendor_path)
        else:
            extra_args = [f"-s={manifest}" for manifest in cargo_manifests[1:]]
            extra_args.append(vendor_path)
            cargo("vendor", cargo_manifests[0], *extra_args)

    console.print("Generating a tarball with vendored dependencies")
    with tarfile.open(vendor_tarball, "w:gz") as tar:
        tar.add(vendor_path, arcname=vendor_path.name)

    console.print("Preparing vendored dependencies for lookaside cache")
    rhpkg("new-sources", vendor_tarball)


def _get_dependency(dependency_name: str, version: str) -> Path:
    dependencies_path = Path(DEPENDENCIES_FOLDER)
    dependencies_path.mkdir(exist_ok=True)
    archive = dependencies_path / f"{dependency_name}-{version}"
    if archive.exists():
        console.print(f"Using cached achive for {dependency_name}")
        return archive
    url = RUST_SOURCE_URL[dependency_name] % version
    console.print(f"Downloading {dependency_name} achive from {url}")
    dl_resp = requests.get(url)
    if not dl_resp.ok:
        console.print(f"Failed to download {url}", style="red")
        raise typer.Abort()
    with (
        tarfile.open(fileobj=io.BytesIO(dl_resp.content)) as tarball,
        TemporaryDirectory() as tmp_dir,
    ):
        tarball.extractall(tmp_dir)
        extracted_files = list(Path(tmp_dir).iterdir())
        if len(extracted_files) != 1:
            console.print(
                f"github tarball for {dependency_name}-{dependency_name} contains more "
                "than a single 'toplevel' file. Please update chaski to handle this.",
                style="red",
            )
            typer.Abort()
        if not extracted_files[0].is_dir():
            console.print(
                f"github tarball for {dependency_name}-{dependency_name} doesn't "
                "contain a directory. Please update chaski to handle this.",
                style="red",
            )
            typer.Abort()
        shutil.move(extracted_files[0], archive)
    return archive


if __name__ == "__main__":
    app()
