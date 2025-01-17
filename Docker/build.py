#!/bin/python

# Copyright 2022 Image Analysis Lab, German Center for Neurodegenerative Diseases(DZNE), Bonn
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Author: David Kuegler
# June 27th 2023

import argparse
import os
import subprocess
from itertools import chain
from pathlib import Path
from typing import Tuple, Literal, Sequence, Optional, Dict, get_args, cast, List, Callable, Union
import logging


logger = logging.getLogger(__name__)

Target = Literal['runtime', 'build_common', 'build_conda', 'build_freesurfer',
                 'build_base', 'runtime_cuda']
CacheType = Literal["inline", "registry", "local", "gha", "s3", "azblob"]
AllDeviceType = Literal["cpu", "cuda", "cu116", "cu117", "cu118", "rocm", "rocm5.1.1",
                        "rocm5.4.2"]
DeviceType = Literal["cpu", "cu116", "cu117", "cu118", "rocm5.1.1", "rocm5.4.2"]


__import_cache = {}


class DEFAULTS:
    # Here (and in the Literals at the top of the document), we need to update the cuda
    # and rocm versions, if pytorch comes with new versions.
    # torch 1.12.0 comes compiled with cu113, cu116, rocm5.0 and rocm5.1.1
    # torch 2.0.1 comes compiled with cu117, cu118, and rocm5.4.2
    MapDeviceType: Dict[AllDeviceType, DeviceType] = dict(
        ((d, d) for d in get_args(DeviceType)),
        rocm="rocm5.1.1",
        cuda="cu117",
    )
    BUILD_BASE_IMAGE = "ubuntu:22.04"
    RUNTIME_BASE_IMAGE = "ubuntu:22.04"
    FREESURFER_BUILD_IMAGE = "build_freesurfer"
    CONDA_BUILD_IMAGE = "build_conda"


def _import_calls(fasturfer_home: Path, token: str = "Popen") -> Callable:
    # import call and call_async without importing FastSurferCNN fully
    if token not in __import_cache:
        def __import(file: Path, name: str, *tokens: str, **rename_tokens: str):
            from importlib.util import spec_from_file_location, module_from_spec
            spec = spec_from_file_location(name, file)
            module = module_from_spec(spec)
            spec.loader.exec_module(module)
            for tok, name in chain(zip(tokens, tokens), rename_tokens.items()):
                __import_cache[tok] = getattr(module, name)

        if token in ("Popen", "PyPopen"):
            # import Popen and PyPopen from FastSurferCNN.utils.run_tools
            __import(fasturfer_home / "FastSurferCNN/utils/run_tools.py",
                     "run_tools", "Popen", "PyPopen")
        elif token in ("version", "parse_build_file", "has_git"):
            # import main as version from FastSurferCNN.version
            __import(fasturfer_home / "FastSurferCNN/version.py",
                     "version", "parse_build_file", "has_git",
                     version="main")

    if token in __import_cache:
        return __import_cache[token]
    else:
        raise ImportError(f"Invalid token {token}")


def docker_image(arg) -> str:
    """
    Returns a str with the image.

    Raises
    ======
    ArgumentTypeError
        if it is not a valid docker image.
    """
    from re import match
    # regex from https://stackoverflow.com/questions/39671641/regex-to-parse-docker-tag
    pattern = r"^(?:(?=[^:\/]{1,253})(?!-)[a-zA-Z0-9-]{1,63}(?<!-)" \
              r"(?:\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))*(?::[0-9]{1,5})?/)?" \
              r"((?![._-])(?:[a-z0-9._-]*)(?<![._-])(?:/(?![._-])[a-z0-9._-]*" \
              r"(?<![._-]))*)(?::(?![.-])[a-zA-Z0-9_.-]{1,128})?$"
    if match(pattern, arg):
        return arg
    else:
        raise argparse.ArgumentTypeError(
            f"The image '{arg}' does not look like a valid image name."
        )


def target(arg) -> Target:
    """Returns a tuple of the target (see `Target`) and a str with the docker image.

    Raises
    ======
    ArgumentTypeError
        if not valid."""
    if isinstance(arg, str) and arg in get_args(Target):
        return cast(Target, arg)
    else:
        raise argparse.ArgumentTypeError(
            f"target must be one of {', '.join(get_args(Target))}, but was {arg}."
        )


class CacheSpec:
    """Class to parse the cache specification."""

    _type: CacheType
    _params: dict

    CACHE_PARAMETERS: Dict[CacheType, Tuple[List[str], List[str]]] = {
        "inline": ([], []),
        "registry": (
            ["ref", "compression", "compression-level", "force-compression",
             "image-manifest", "oci-mediatypes", "ignore-error"],
            ["ref"]),
        "local": (
            ["dest", "tag", "compression", "compression-level", "force-compression",
             "image-manifest", "oci-mediatypes", "ignore-error"],
            ["src", "digest", "tag"]),
        "gha": (["scope", "ignore-error"], ["scope"]),
        "s3": (
            ["bucket", "region", "name", "prefix", "ignore-error"],
            ["bucket", "region", "name", "prefix", "blobs_prefix", "manifests_prefix"]),
        "azblob": (
            ["account_url", "name", "prefix", "ignore-error"],
            ["account_url", "name", "prefix", "blobs_prefix", "manifests_prefix"]),
    }

    def __init__(self, arg: str):
        self._params = {}
        for key_value_pair in arg.split(","):
            k, v = key_value_pair.split("=", 2)
            if k.strip().lower() == "type":
                self.type = v.strip()
            else:
                self._params[k.strip().lower()] = v.strip()
        if not hasattr(self, "_type"):
            raise ValueError("No type defined in arg.")

    @property
    def type(self) -> CacheType:
        return self._type

    @type.setter
    def type(self, _type: str):
        from typing import get_args, cast
        if _type.lower() in get_args(CacheType):
            self._type = cast(CacheType, _type)
        else:
            raise ValueError(f"{_type} is not a valid cache type of "
                             f"{', '.join(get_args(CacheType))}")

    def to_str(self, from_str_format: bool, **kwargs) -> str:
        valid_parameters = self.CACHE_PARAMETERS[self.type][int(from_str_format)]
        params = {k: v for k, v in self._params.items() if k in valid_parameters}
        params["type"] = self.type
        params.update(kwargs)
        return ",".join("=".join(i) for i in params.items())

    def format_cache_from(self) -> str:
        return self.to_str(True)

    def format_cache_to(self) -> str:
        return self.to_str(False, mode=self._params.get("mode", "min"))

    __repr__ = format_cache_from


def make_parser() -> argparse.ArgumentParser:
    try:
        from FastSurferCNN.segstats import HelpFormatter
    except ImportError:
        from argparse import HelpFormatter
    parser = argparse.ArgumentParser(
        description="Docker build script for FastSurfer",
        formatter_class=HelpFormatter,
    )

    parser.add_argument(
        "--device",
        choices=["cpu", "cuda", "cu117", "cu118", "rocm", "rocm5.4.2"],
        required=True,
        help="""selection of internal build stages to build for a specific platform.<br>
                - cuda: defaults to cu118, cuda 11.8<br>
                - cpu: only cpu support<br>
                - rocm: defaults to rocm5.4.2 (experimental)""",
    )
    parser.add_argument(
        "--tag",
        type=docker_image,
        dest="image_tag",
        metavar="image[:tag]",
        help="""tag build stage/target as <image>[:<tag>]""",
    )
    parser.add_argument(
        "--target",
        default="runtime",
        type=target,
        choices=get_args(Target),
        metavar="target",
        help=f"""target to build (from list of targets below, defaults to runtime):<br>
                 - build_conda: "finished" conda build image<br>
                 - build_freesurfer: "finished" freesurfer build image<br>
                 - runtime: final fastsurfer runtime image""",
    )
    parser.add_argument(
        "--rm",
        action="store_true",
        help="disables caching, i.e. removes all intermediate images.",
    )
    cache_kwargs = {}
    if "FASTSURFER_BUILD_CACHE" in os.environ:
        try:
            cache_kwargs = {
                "default": CacheSpec(os.environ["FASTSURFER_BUILD_CACHE"])
            }
        except ValueError as e:
            logger.warning(
                f"ERROR while parsing the environment variable 'FASTSURFER_BUILD_CACHE' "
                f"{os.environ['FASTSURFER_BUILD_CACHE']} (ignoring this environment "
                f"variable): {e.args[0]}"
            )
    parser.add_argument(
        "--cache",
        type=CacheSpec,
        help=f"""cache as defined in https://docs.docker.com/build/cache/backends/ 
                 (using --cache-to syntax, parameters are automatically filtered for use 
                 in --cache-to and --cache-from), e.g.: 
                 --cache type=registry,ref=server/fastbuild,mode=max.
                 Will default to the environment variable FASTSURFER_BUILD_CACHE: 
                 {cache_kwargs.get('default', 'N/A')}""",
        **cache_kwargs,
    )
    parser.add_argument(
        "--dry_run",
        "--print",
        action="store_true",
        help="Instead of starting processes, write the commands to stdout, so they can "
             "be dry_run with 'build.py ... --dry_run | bash'.",
    )
    parser.add_argument(
        "--tag_dev",
        action="store_true",
        help="Also tag the resulting image as 'fastsurfer:dev'.",
    )

    expert = parser.add_argument_group('Expert options')

    parser.add_argument(
        "--attest",
        action="store_true",
        help="add sbom and provenance attestation (requires docker-container buildkit "
             "builder created with 'docker buildx create')",
    )
    parser.add_argument(
        "--action",
        choices=("load", "push"),
        default="load",
        help="Which action to perform after building the image (if a docker-container "
             "is detected): "
             "'load' loads the image into the current docker context (default), "
             "'push' pushes the image to the registry (needs --tag <registry>/"
             "<name+maybe organization>:<tag>)",
    )
    expert.add_argument(
        "--freesurfer_build_image",
        type=docker_image,
        metavar="image[:tag]",
        help="""explicitly specifies an image to copy freesurfer binaries from.
                freesurfer binaries are expected to be in /opt/freesurfer in the image, 
                like the runtime image. By default, uses the "build_freesurfer" stage in 
                the Dockerfile (either by building it or from cache, see --cache).""")
    expert.add_argument(
        "--conda_build_image",
        type=docker_image,
        metavar="image[:tag]",
        help="""explicitly specifies an image to copy the python environment from.
                The environment is expected to be in /venv in the image, like the 
                runtime image. By default, uses the "build_conda" stage in the 
                Dockerfile (either by building it or from cache, see --cache).""")
    expert.add_argument(
        "--runtime_base_image",
        type=docker_image,
        metavar="image[:tag]",
        help="explicitly specifies the base image to build the runtime image from "
             "(default: ubuntu:22.04).")
    expert.add_argument(
        "--build_base_image",
        type=docker_image,
        metavar="image[:tag]",
        help="explicitly specifies the base image to build the build images from "
             "(default: ubuntu:22.04).")

    expert.add_argument(
        "--debug",
        action="store_true",
        help="enables the DEBUG build flag."
    )
    return parser


def red(skk):
    return "\033[91m {}\033[00m" .format(skk)


def get_builder(Popen, require_builder_type: str) -> tuple[bool, str]:
    """Get the builder to build the fastsurfer image."""
    from subprocess import PIPE
    from re import compile
    buildx_binfo = Popen(["docker", "buildx", "ls"], stdout=PIPE, stderr=PIPE).finish()
    header, *lines = buildx_binfo.out_str("utf-8").strip().split("\n")
    header_pattern = compile("\\S+\\s*")
    fields = {}
    pos = 0
    while pos < len(header) and (match := header_pattern.search(header, pos)):
        start, pos = match.span()
        fields[match.group().strip()] = slice(start, pos)
    builders = {line[fields["NAME/NODE"]]: line[fields["DRIVER/ENDPOINT"]]
                for line in lines if not line.startswith(" ")}
    builders = {key.strip(): value.strip() for key, value in builders.items()}
    default_builders = [name for name in builders.keys() if name.endswith("*")]
    if len(default_builders) != 1:
        raise RuntimeError("Could not find default builder of buildx")
    default_builder = default_builders[0][:-1].strip()
    builders[default_builder] = builders[default_builders[0]]
    del builders[default_builders[0]]
    cannot_use_default_builder = (
            require_builder_type and builders[default_builder] != require_builder_type
    )
    if cannot_use_default_builder:
        # if the default builder is a docker builder (which does not support
        for builder in builders.keys():
            if (builder.startswith("fastsurfer") and
                    builders[builder] == require_builder_type):
                default_builder = builder
                break
        if builders[default_builder] != require_builder_type:
            # did not find an appropriate builder
            raise RuntimeError(
                "Could not find an appropriate builder from the current builder "
                "(see docker buildx use) or builders named fastsurfer* (searching for "
                f"a builder of type {require_builder_type}, docker "
                "builders may not be supported with the selected export settings. "
                "Create builder with 'docker buildx create --name fastsurfer'."
            )
    return not cannot_use_default_builder, default_builder


def docker_build_image(
        image_name: str,
        dockerfile: Path,
        working_directory: Optional[Path] = None,
        context: Path | str = ".",
        dry_run: bool = False,
        attestation: bool = False,
        action: Literal["load", "push"] = "load",
        **kwargs) -> None:
    """
    Build a docker image.

    Parameters
    ----------
    image_name : str
        Name / target tag of the image.
    dockerfile : Path, str
        Path to the Dockerfile.
    working_directory : Path, str, optional
        Path o the working directory to perform the build operation (default: inherit).
    context : Path, str, optional
        Base path to the context folder to build the docker image from (default: '.').
    dry_run : bool, optional
        Whether to actually trigger the build, or just print the command to the console
        (default: False => actually build).
    cache_to : str, optional
        Forces usage of buildx over build, use docker build caching as in the --cache-to
        argument to docker buildx build.
    attestation : bool, default=False
        Whether to create sbom and provenance attestation
    action : "load", "push", default="load"
        The operation to perform after the image is built (only if a docker-container
        builder is detected).

    Additional kwargs add additional build flags to the build command in the following
    manner: "_" is replaced by "-" in the keyword name and each sequence entry is passed
    with its own flag, e.g. `docker_build_image(..., build_arg=["TEST=1", "VAL=2"])` is
    translated to `docker [buildx] build ... --build-arg TEST=1 --build-arg VAL=2`.
    """
    from itertools import chain, repeat
    from subprocess import PIPE
    logger.info("Building. This starts with sending the build context to the docker "
                "daemon, which may take a while...")
    extra_env = {"DOCKER_BUILDKIT": "1"}

    from shutil import which
    docker_cmd = which("docker")
    if docker_cmd is None:
        raise FileNotFoundError("Could not locate the docker executable")

    if action not in ("load", "push"):
        raise ValueError(f"Invalid Value for 'action' {action}, must be load or push.")

    def to_pair(key, values):
        if isinstance(values, Sequence) and isinstance(values, (str, bytes)):
            values = [values]
        key_dashed = key.replace("_", "-")
        # concatenate the --key_dashed value pairs
        return list(chain(*zip(repeat(f"--{key_dashed}"), values)))

    # needs buildx
    args = ["buildx", "build"]

    # always use/require buildx (required for sbom and provenance)
    Popen = _import_calls(working_directory)  # from fastsurfer dir
    if attestation or \
            any(kwargs.get(f"cache_{c}", "inline") != "inline" for c in ("to", "from")):
        buildx_test = Popen(
            [docker_cmd, "buildx", "version"],
            stdout=PIPE,
            stderr=PIPE,
        ).finish()
        if "'buildx' is not a docker command" in buildx_test.err_str("utf-8").strip():
            wget_cmd = (
                "wget -qO ~/.docker/cli-plugins/docker-buildx https://github.com/docker"
                "/buildx/releases/download/{0:s}/buildx-{0:s}.{1:s}"
            )
            raise RuntimeError(
                f"Using --cache or attestation requires docker buildx, install with "
                f"'{wget_cmd % ('<version>', '<platform>')}'\n"
                f"e.g. '{wget_cmd % ('v0.12.1', 'linux-amd64')}\n"
                f"You may need to 'chmod +x ~/.docker/cli-plugins/docker-buildx'\n"
                f"See also https://github.com/docker/buildx#manual-download"
            )

    default_builder_is_container, alternative_builder = get_builder(
        Popen,
        "docker-container",
    )
    args.append("--output")
    if not attestation:
        # tag image_name in local registry (simple standard case)
        if default_builder_is_container:
            args.extend([f"type=docker,name={image_name}", "--" + action])
        else:
            args.append(f"type=image,name={image_name}")
    else:
        # want to create sbom and provenance manifests, so needs to use a
        # docker-container builder
        image_type = "registry" if action == "push" else "docker"
        args.extend([f"type={image_type},name={image_name}", "--" + action])

        args.extend(["--attest", "type=sbom", "--provenance=true"])
        if not default_builder_is_container:
            args.extend(["--builder", alternative_builder])
    args.extend(("-t", image_name))
    params = [to_pair(*a) for a in kwargs.items()]
    args.extend(["-f", str(dockerfile)] + list(chain(*params)))
    args.append(str(context))
    if dry_run:
        extra_environment = [f"{k}={v}" for k, v in extra_env.items()]
        print(" ".join(extra_environment + ["docker"] + args))
    else:
        env = dict(os.environ)
        env.update(extra_env)
        with Popen([docker_cmd] + args + ["--progress=plain"],
                   cwd=working_directory, env=env, stdout=subprocess.PIPE) as proc:
            for msg in proc:
                if msg.out:
                    logger.info("stdout: " + msg.out.decode("utf-8"))
                if msg.err:
                    logger.info("stderr: " + red(msg.err.decode("utf-8")))


def main(
        device: DeviceType,
        cache: Optional[CacheSpec] = None,
        rm: bool = False,
        target: Target = "runtime",
        debug: bool = False,
        image_tag: Optional[str] = None,
        dry_run: bool = False,
        tag_dev: bool = True,
        **keywords
        ) -> int | str:
    this_script = Path(__file__)
    if not this_script.is_absolute():
        this_script = Path.cwd() / __file__
    fastsurfer_home = this_script.parent.parent
    version = _import_calls(fastsurfer_home, "version")
    has_git = _import_calls(fastsurfer_home, "has_git")
    parse_build_file = _import_calls(fastsurfer_home, "parse_build_file")

    kwargs: Dict[str, Union[str, List[str]]] = {}
    if cache is not None:
        if not isinstance(cache, CacheSpec):
            cache = CacheSpec(cache)
        logger.info(f"cache: {cache}")
        kwargs["cache_from"] = cache.format_cache_from()
        kwargs["cache_to"] = cache.format_cache_from()
    elif rm is True:
        kwargs["no-cache"] = None

    if target not in get_args(Target):
        raise ValueError(f"Invalid target: {target}")
    if device not in get_args(AllDeviceType):
        raise ValueError(f"Invalid device: {device}")
    if keywords.get("action", "load") == "push":
        kwargs["action"] = "push"
    # special case to add extra environment variables to better support AWS and ROCm
    if device.startswith("cu") and target == "runtime":
        target = "runtime_cuda"
    kwargs["target"] = target
    kwargs["build_arg"] = [f"DEVICE={DEFAULTS.MapDeviceType.get(device, 'cpu')}"]
    if debug:
        kwargs["build_arg"].append(f"DEBUG=true")
    for key in ["build_base_image", "runtime_base_image", "freesurfer_build_image",
                "conda_build_image"]:
        upper_key = key.upper()
        value = keywords.get(key) or getattr(DEFAULTS, upper_key)
        kwargs["build_arg"].append(f"{upper_key}={value}")
    #    kwargs["build_arg"] = " ".join(kwargs["build_arg"])

    build_filename = fastsurfer_home / "BUILD.info"
    if has_git():
        version_sections = "+git"
    else:
        # try creating the build file without git info
        version_sections = ""
        logger.warning(
            "Failed to create the git_status section in the BUILD.info file. "
            "The resulting build file will not have valid git information, so "
            "the version command of FastSurfer in the image will not complete."
        )

    with open(build_filename, "w") as build_file, \
            open(fastsurfer_home / "pyproject.toml") as project_file:
        ret_version = version(
            version_sections,
            project_file=project_file,
            file=build_file,
            build_cache=False,
        )
    if ret_version != 0:
        return f"Creating the version file failed with message: {ret_version}"

    with open(build_filename, "r") as build_file:
        build_info = parse_build_file(build_file)

    version_tag = build_info["version_tag"]
    image_suffix = ""
    if device != "cuda":
        image_suffix = f"-{device}"
    # image_tag is None or ""
    if not bool(image_tag):
        image_tag = f"fastsurfer:{version_tag}{image_suffix}".replace("+", "_")

    attestation = bool(keywords.get("attest"))
    if not attestation:
        # only attestation requires and actively changes to a docker-container driver
        if cache is not None and cache.type != "inline":
            Popen = _import_calls(fastsurfer_home, "Popen")
            try:
                can_default, _ = get_builder(Popen, "docker-container")
            except RuntimeError as e:
                return e.args[0]
            if not can_default:
                return ("The docker build interface only support caching inline, i.e. "
                        "--cache type=inline.")

    if tag_dev:
        kwargs["tag"] = f"fastsurfer:dev{image_suffix}"

    if not dry_run:
        logger.info("Version info added to the docker image:")
        logger.info(build_info["content"])

    dockerfile = fastsurfer_home / "Docker" / "Dockerfile"
    try:
        docker_build_image(
            image_tag,
            dockerfile,
            working_directory=fastsurfer_home,
            context=fastsurfer_home,
            dry_run=dry_run,
            attestation=attestation,
            **kwargs,
        )
    except RuntimeError as e:
        return e.args[0]
    return 0


if __name__ == "__main__":
    import sys
    logging.basicConfig(stream=sys.stdout)
    arguments = make_parser().parse_args()

    # make sure the code can run without FastSurfer being in PYTHONPATH
    if "FASTSURFER_HOME" in os.environ:
        fastsurfer_home = os.environ["FASTSURFER_HOME"]
    else:
        fastsurfer_home = str(Path(__file__).parent.parent)

    if fastsurfer_home not in sys.path:
        sys.path.append(fastsurfer_home)

    logger.setLevel(logging.WARN if arguments.dry_run else logging.INFO)
    sys.exit(main(**vars(arguments)))
