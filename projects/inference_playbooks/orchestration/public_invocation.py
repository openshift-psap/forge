"""Approved workload facts from the actual generated benchmark Job."""

import hashlib
import json
import re
import shlex

import yaml


def project_invocation(raw, harness):
    try:
        job = yaml.safe_load(raw)
    except yaml.YAMLError:
        raise ValueError("invalid private benchmark Job YAML") from None
    if job.get("kind") != "Job" or harness not in ("guidellm", "aiperf"):
        raise ValueError("invalid private benchmark invocation")
    commands = []
    for container in job["spec"]["template"]["spec"]["containers"]:
        command, args = container.get("command", []), container.get("args", [])
        if command and command[0].rsplit("/", 1)[-1] == harness:
            commands.append([*command, *args])
        elif command and command[0] in ("/bin/sh", "/bin/bash", "sh", "bash"):
            for script in args:
                for line in script.splitlines():
                    if re.match(r"^\s*(?:/[^\s]+/)?" + harness + r"\s+(?:run|profile)\s", line):
                        commands.append(shlex.split(line))
    if len(commands) != 1:
        raise ValueError("private benchmark command is missing or ambiguous")
    argv = commands[0]
    if any(arg == "--config" or arg.startswith("--config=") for arg in argv):
        raise ValueError("private benchmark config-file override is unsupported")
    flag = "--profile" if harness == "guidellm" else "--concurrency"
    values = []
    for index, arg in enumerate(argv):
        if arg.startswith(flag + "="):
            values.append(arg.partition("=")[2])
        elif arg in ({flag, "-c"} if harness == "aiperf" else {flag}) and index + 1 < len(argv):
            values.append(argv[index + 1])
    if len(values) != 1:
        raise ValueError("private benchmark concurrency is missing or ambiguous")
    value = values[0]
    if harness == "aiperf":
        if not re.fullmatch("[0-9]+", value):
            raise ValueError("invalid private AIPerf concurrency")
        levels = [int(value)]
    else:
        if value.startswith("{"):
            profile = json.loads(value)
        else:
            pairs = [part.split("=", 1) for part in value.split(",")]
            if any(len(pair) != 2 for pair in pairs) or len({pair[0] for pair in pairs}) != len(
                pairs
            ):
                raise ValueError("ambiguous private GuideLLM profile")
            profile = dict(pairs)
            if re.fullmatch("[0-9]+", profile.get("streams", "")):
                profile["streams"] = int(profile["streams"])
        if profile.get("kind") != "concurrent":
            raise ValueError("private GuideLLM requires a concurrency profile")
        value = profile.get("streams")
        levels = value if isinstance(value, list) else [value]
    if (
        not levels
        or any(type(value) is not int or value < 1 for value in levels)
        or len(set(levels)) != len(levels)
    ):
        raise ValueError("invalid private benchmark concurrency levels")
    return {
        "contract": "private-invocation-v1",
        "harness": harness,
        "source_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "concurrencies": sorted(levels),
    }
