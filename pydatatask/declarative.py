"""This module contains parsing methods for transforming various dict and list schemas into Repository, Task, and
other kinds of pydatatask classes."""
from typing import Any, Callable, Dict, List, Mapping, Optional, Type, TypeVar
from datetime import timedelta
from enum import Enum
import base64
import gc
import json
import os
import socket
import sys
import traceback

from importlib_metadata import entry_points
from motor.core import AgnosticClient
import aiobotocore.session
import asyncssh
import docker_registry_client_async
import motor.motor_asyncio

from pydatatask.executor import Executor
from pydatatask.executor.container_manager import DockerContainerManager
from pydatatask.executor.pod_manager import PodManager, kube_connect
from pydatatask.executor.proc_manager import (
    InProcessLocalLinuxManager,
    LocalLinuxManager,
    SSHLinuxManager,
)
from pydatatask.host import Host, HostOS
from pydatatask.quota import Quota, QuotaManager
from pydatatask.repository import (
    DirectoryRepository,
    DockerRepository,
    FileRepository,
    InProcessBlobRepository,
    InProcessMetadataRepository,
    MongoMetadataRepository,
    Repository,
    S3BucketRepository,
    YamlMetadataFileRepository,
    YamlMetadataS3Repository,
)
from pydatatask.session import Ephemeral
from pydatatask.task import ContainerTask, KubeTask, LinkKind, ProcessTask, Task
import pydatatask

_T = TypeVar("_T")


def parse_bool(thing: Any) -> bool:
    """Parse a string, int, or bool into a bool."""
    if isinstance(thing, bool):
        return thing
    if isinstance(thing, int):
        return bool(thing)
    if isinstance(thing, str):
        if thing.lower() in ("yes", "y", "1", "true"):
            return True
        if thing.lower() in ("no", "n", "0", "false"):
            return False
        raise ValueError(f"Invalid bool value {thing}")
    raise ValueError(f"{type(thing)} is not valid as a bool")


_E = TypeVar("_E", bound=Enum)


def make_enum_constructor(cls: Type[_E]) -> Callable[[Any], Optional[_E]]:
    """Parse a string into an enum."""

    def inner(thing):
        if thing is None:
            return None
        if not isinstance(thing, str):
            raise ValueError(f"{cls} must be instantiated by a string")
        return getattr(cls, thing)

    return inner


def make_constructor(name: str, constructor: Callable[..., _T], schema: Dict[str, Any]) -> Callable[[Any], _T]:
    """Generate a constructor function, or a function which will take a dict of parameters, validate them, and call
    a function with them as keywords."""
    tdc = make_typeddict_constructor(name, schema)

    def inner(thing):
        return constructor(**tdc(thing))

    return inner


def make_typeddict_constructor(name: str, schema: Dict[str, Any]) -> Callable[[Any], Dict[str, Any]]:
    """Generate a dict constructor function, or a function which will take a dict of parameters, validate and
    transform them according to a schema, and return that dict."""

    def inner(thing):
        if not isinstance(thing, dict):
            raise ValueError(f"{name} must be followed by a mapping")

        kwargs = {}
        for k, v in thing.items():
            if k not in schema:
                raise ValueError(f"Invalid argument to {name}: {k}")
            kwargs[k] = schema[k](v)
        return kwargs

    return inner


def make_dispatcher(name: str, mapping: Dict[str, Callable[[Any], _T]]) -> Callable[[Any], _T]:
    """Generate a dispatcher function, or a function which accepts a mapping of two keys: cls and args.

    cls should be one keys in the provided mapping, and args are the arguments to the function pulled out of mapping.
    Should be used for situations where you need to pick from one of many implementations of something.
    """

    def inner(thing):
        if not isinstance(thing, dict):
            raise ValueError(f"{name} must be a mapping")
        if "cls" not in thing:
            raise ValueError(f"You must provide the cls name for {name}")
        key = thing["cls"]
        value = thing.get("args", {})
        constructor = mapping.get(key, None)
        if constructor is None:
            raise ValueError(f"{key} is not a valid member of {name}")
        return constructor(value)

    return inner


def make_dict_parser(
    name: str, key_parser: Callable[[str], str], value_parser: Callable[[Any], _T]
) -> Callable[[Any], Dict[str, _T]]:
    """Generate a dict parser function, or a function which validates and transforms the keys and values of a dict
    into another dict."""

    def inner(thing):
        if not isinstance(thing, dict):
            raise ValueError(f"{name} must be a dict")
        return {key_parser(key): value_parser(value) for key, value in thing.items()}

    return inner


def make_list_parser(name: str, value_parser: Callable[[Any], _T]) -> Callable[[Any], List[_T]]:
    """Generate a list parser function, or a function which validates and transforms the members of a list into
    another list."""

    def inner(thing):
        if not isinstance(thing, list):
            raise ValueError(f"{name} must be a list")
        return [value_parser(value) for value in thing]

    return inner


def make_picker(name: str, options: Mapping[str, _T]) -> Callable[[Any], Optional[_T]]:
    """Generate a picker function, or a function which takes a string and returns one of the members of the provided
    options dict."""

    def inner(thing):
        if thing is None:
            return None
        if not options:
            raise ValueError(f"Must provide at least one {name}")
        if not isinstance(thing, str):
            raise ValueError(f"When picking a {name}, must provide a str")
        if thing not in options:
            raise ValueError(f"{thing} is not a valid option for {options}, you want e.g. {next(iter(options))}")
        return options[thing]

    return inner


def _build_s3_connection(endpoint: str, username: str, password: str):
    async def minio():
        minio_session = aiobotocore.session.get_session()
        async with minio_session.create_client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=username,
            aws_secret_access_key=password,
        ) as client:
            yield client

    return minio


def _build_docker_connection(
    domain: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    config_file: Optional[str] = None,
    default_config_file: bool = False,
):
    if default_config_file:
        config_file = os.path.expanduser("~/.docker/config.json")
    if config_file is not None:
        with open(config_file, "r", encoding="utf-8") as fp:
            docker_config = json.load(fp)
        username, password = base64.b64decode(docker_config["auths"][domain]["auth"]).decode().split(":")
    else:
        if username is None or password is None:
            raise ValueError("Must provide username and password or a config file for DockerRegistry")

    async def docker():
        registry = docker_registry_client_async.DockerRegistryClientAsync(
            client_session_kwargs={"connector_owner": True},
            tcp_connector_kwargs={"family": socket.AF_INET},
            ssl=True,
        )
        await registry.add_credentials(
            credentials=base64.b64encode(f"{username}:{password}".encode()).decode(),
            endpoint=domain,
        )
        yield registry
        await registry.close()
        gc.collect()

    return docker


def _build_mongo_connection(url: str, database: str):
    async def mongo():
        client: "AgnosticClient[Any]" = motor.motor_asyncio.AsyncIOMotorClient(url)
        collection = client.get_database(database)
        yield collection

    return mongo


def _build_ssh_connection(
    hostname: str, username: str, password: Optional[str] = None, key: Optional[str] = None, port: int = 22
):
    async def ssh():
        async with asyncssh.connect(
            hostname,
            port=port,
            username=username,
            password=password,
            known_hosts=None,
            client_keys=asyncssh.load_keypairs(key) if key is not None else None,
        ) as s:
            yield s

    return ssh


quota_constructor = make_constructor("quota", Quota.parse, {"cpu": str, "mem": str, "launches": str})
timedelta_constructor = make_constructor(
    "timedelta",
    timedelta,
    {"days": int, "seconds": int, "microseconds": int, "milliseconds": int, "minutes": int, "hours": int, "weeks": int},
)


def make_annotated_constructor(
    name: str, constructor: Callable[..., _T], schema: Dict[str, Any]
) -> Callable[[Any], _T]:
    """Generate a constructor which allows the passing of an "annotations" key even if the constructor does not take
    one."""

    def inner_constructor(**kwargs):
        annotations = kwargs.pop("annotations", {})
        result = constructor(**kwargs)
        result.annotations.update(annotations)  # type: ignore
        return result

    schema["annotations"] = lambda x: x
    return make_constructor(name, inner_constructor, schema)


def build_repository_picker(ephemerals: Dict[str, Callable[[], Any]]) -> Callable[[Any], Repository]:
    """Generate a function which will dispatch a dict into all known repository constructors.

    This function can be extended through the ``pydatatask.repository_constructors`` entrypoint.
    """
    kinds: Dict[str, Callable[[Any], Repository]] = {
        "InProcessMetadata": make_annotated_constructor(
            "InProcessMetadataRepository",
            InProcessMetadataRepository,
            {},
        ),
        "InProcessBlob": make_annotated_constructor(
            "InProcessBlobRepository",
            InProcessBlobRepository,
            {},
        ),
        "File": make_annotated_constructor(
            "FileRepository",
            FileRepository,
            {
                "basedir": str,
                "extension": str,
                "case_insensitive": parse_bool,
            },
        ),
        "Directory": make_annotated_constructor(
            "DirectoryRepository",
            DirectoryRepository,
            {
                "basedir": str,
                "extension": str,
                "case_insensitive": parse_bool,
                "discard_empty": parse_bool,
            },
        ),
        "YamlFile": make_annotated_constructor(
            "YamlMetadataFileRepository",
            YamlMetadataFileRepository,
            {
                "basedir": str,
                "extension": str,
                "case_insensitive": parse_bool,
            },
        ),
        "S3Bucket": make_annotated_constructor(
            "S3BucketRepository",
            S3BucketRepository,
            {
                "client": make_picker("S3Connection", ephemerals),
                "bucket": str,
                "prefix": str,
                "suffix": str,
                "mimetype": str,
                "incluster_endpoint": str,
            },
        ),
        "YamlMetadataS3Bucket": make_annotated_constructor(
            "YamlMetadataS3Repository",
            YamlMetadataS3Repository,
            {
                "client": make_picker("S3Connection", ephemerals),
                "bucket": str,
                "prefix": str,
                "suffix": str,
                "mimetype": str,
                "incluster_endpoint": str,
            },
        ),
        "DockerRegistry": make_annotated_constructor(
            "DockerRepository",
            DockerRepository,
            {
                "registry": make_picker("DockerRegistry", ephemerals),
                "domain": str,
                "repository": str,
            },
        ),
        "MongoMetadata": make_annotated_constructor(
            "MongoMetadataRepository",
            MongoMetadataRepository,
            {
                "database": make_picker("MongoDatabase", ephemerals),
                "collection": str,
            },
        ),
    }
    for ep in entry_points(group="pydatatask.repository_constructors"):
        maker = ep.load()
        try:
            kinds.update(maker(ephemerals))
        except TypeError:
            traceback.print_exc(file=sys.stderr)
    return make_dispatcher("Repository", kinds)


def build_executor_picker(hosts: Dict[str, Host], ephemerals: Dict[str, Ephemeral[Any]]) -> Callable[[Any], Executor]:
    """Generate a function which will dispatch a dict into all known executor constructors.

    This function can be extended through the ``pydatatask.executor_constructors`` entrypoint.
    """
    kinds: Dict[str, Callable[[Any], Executor]] = {
        "TempLinux": make_constructor(
            "InProcessLocalLinuxManager",
            InProcessLocalLinuxManager,
            {
                "app": str,
                "local_path": str,
            },
        ),
        "LocalLinux": make_constructor(
            "LocalLinuxManager",
            LocalLinuxManager,
            {
                "app": str,
                "local_path": str,
            },
        ),
        "SSHLinux": make_constructor(
            "SSHLinuxManager",
            SSHLinuxManager,
            {
                "host": make_picker("Host", hosts),
                "app": str,
                "remote_path": str,
                "ssh": make_picker("SSHConnection", ephemerals),
            },
        ),
        "Kubernetes": make_constructor(
            "PodManager",
            PodManager,
            {
                "host": make_picker("Host", hosts),
                "app": str,
                "namespace": str,
                "connection": make_picker("KubeConnection", ephemerals),
            },
        ),
        "Docker": make_constructor(
            "DockerContainerManager",
            DockerContainerManager,
            {
                "host": make_picker("Host", hosts),
                "app": str,
                "url": str,
            },
        ),
    }
    for ep in entry_points(group="pydatatask.executor_constructors"):
        maker = ep.load()
        try:
            kinds.update(maker(ephemerals))
        except TypeError:
            traceback.print_exc(file=sys.stderr)
    return make_dispatcher("Executor", kinds)


host_constructor = make_constructor(
    "Host",
    Host,
    {
        "name": str,
        "os": make_enum_constructor(HostOS),
    },
)


def build_ephemeral_picker() -> Callable[[Any], Ephemeral[Any]]:
    """Generate a function which will dispatch a dict into all known ephemeral constructors.

    This function can be extended through the ``pydatatask.ephemeral_constructors`` entrypoint.
    """
    kinds = {
        "S3Connection": make_constructor(
            "S3Connection",
            _build_s3_connection,
            {
                "endpoint": str,
                "username": str,
                "password": str,
            },
        ),
        "DockerRegistry": make_constructor(
            "DockerRegistry",
            _build_docker_connection,
            {
                "domain": str,
                "username": str,
                "password": str,
                "config_file": str,
                "default_config_file": parse_bool,
            },
        ),
        "MongoDatabase": make_constructor(
            "MongoDatabase",
            _build_mongo_connection,
            {
                "url": str,
                "database": str,
            },
        ),
        "SSHConnection": make_constructor(
            "SSHConnection",
            _build_ssh_connection,
            {
                "hostname": str,
                "username": str,
                "password": str,
                "key": str,
                "port": int,
            },
        ),
        "KubeConnection": make_constructor(
            "KubeConnection",
            kube_connect,
            {
                "config_file": str,
                "context": str,
            },
        ),
    }
    for ep in entry_points(group="pydatatask.ephemeral_constructors"):
        maker = ep.load()
        try:
            kinds.update(maker())
        except TypeError:
            traceback.print_exc(file=sys.stderr)
    return make_dispatcher("Ephemeral", kinds)


link_kind_constructor = make_enum_constructor(LinkKind)


def build_task_picker(
    repos: Dict[str, Repository],
    executors: Dict[str, Executor],
    quotas: Dict[str, QuotaManager],
    ephemerals: Dict[str, Callable[[], Any]],
) -> Callable[[str, Any], Task]:
    """Generate a function which will dispatch a dict into all known task constructors.

    This function can be extended through the ``pydatatask.task_constructors`` entrypoint.
    """
    link_constructor = make_typeddict_constructor(
        "Link",
        {
            "repo": make_picker("Repository", repos),
            "kind": link_kind_constructor,
            "key": lambda thing: None if thing is None else str(thing),
            "multi_meta": lambda thing: None if thing is None else str(thing),
            "is_input": parse_bool,
            "is_output": parse_bool,
            "is_status": parse_bool,
            "inhibits_start": parse_bool,
            "required_for_start": parse_bool,
            "inhibits_output": parse_bool,
            "required_for_output": parse_bool,
        },
    )
    links_constructor = make_dict_parser("links", str, link_constructor)
    kinds = {
        "Process": make_annotated_constructor(
            "ProcessTask",
            ProcessTask,
            {
                "name": str,
                "template": str,
                "executor": make_picker("Executor", executors),
                "quota_manager": make_picker("QuotaManager", quotas),
                "job_quota": quota_constructor,
                "pids": make_picker("Repository", repos),
                "window": timedelta_constructor,
                "timeout": timedelta_constructor,
                "environ": make_dict_parser("environ", str, str),
                "long_running": parse_bool,
                "done": make_picker("Repository", repos),
                "stdin": make_picker("Repository", repos),
                "stdout": make_picker("Repository", repos),
                "stderr": lambda thing: pydatatask.task.STDOUT
                if thing == "STDOUT"
                else make_picker("Repository", repos)(thing),
                "ready": make_picker("Repository", repos),
                "links": links_constructor,
            },
        ),
        "Kubernetes": make_annotated_constructor(
            "KubeTask",
            KubeTask,
            {
                "name": str,
                "executor": make_picker("Executor", executors),
                "quota_manager": make_picker("QuotaManager", quotas),
                "template": str,
                "logs": make_picker("Repository", repos),
                "done": make_picker("Repository", repos),
                "window": timedelta_constructor,
                "timeout": timedelta_constructor,
                "env": make_dict_parser("environ", str, str),
                "ready": make_picker("Repository", repos),
                "links": links_constructor,
                "long_running": parse_bool,
            },
        ),
        "Container": make_annotated_constructor(
            "ContainerTask",
            ContainerTask,
            {
                "name": str,
                "image": str,
                "template": str,
                "executor": make_picker("Executor", executors),
                "entrypoint": make_list_parser("entrypoint", str),
                "quota_manager": make_picker("QuotaManager", quotas),
                "job_quota": quota_constructor,
                "window": timedelta_constructor,
                "timeout": timedelta_constructor,
                "environ": make_dict_parser("environ", str, str),
                "logs": make_picker("Repository", repos),
                "done": make_picker("Repository", repos),
                "ready": make_picker("Repository", repos),
                "links": links_constructor,
                "privileged": parse_bool,
                "tty": parse_bool,
                "long_running": parse_bool,
            },
        ),
    }
    for ep in entry_points(group="pydatatask.task_constructors"):
        maker = ep.load()
        try:
            kinds.update(maker(repos, quotas, ephemerals))
        except TypeError:
            traceback.print_exc(file=sys.stderr)
    dispatcher = make_dispatcher("Task", kinds)

    def constructor(name, thing):
        executable = thing.pop("executable")
        executable["args"].update(thing)
        executable["args"]["name"] = name
        links = links_constructor(executable["args"].pop("links", {}) or {})
        task = dispatcher(executable)
        for linkname, link in links.items():
            task.link(linkname, **link)
        return task

    return constructor
