#!/usr/bin/env python3
"""
Skeleton Example Project CI Operations

This is a skeleton/template project that demonstrates how to create a new project
within the FORGE test harness framework. Use this as a starting point for
building your own projects.
"""

import types

import click
import foreign_testing

from projects.core.library import ci as ci_lib


@click.group(cls=ci_lib.HelpfulGroup)
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx):
    """Foreign Testing Project CI Operations for FORGE."""
    ctx.ensure_object(types.SimpleNamespace)
    foreign_testing.init()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def submit(ctx):
    """Launch a foreign test."""
    project_path = foreign_testing.prepare()
    if not project_path.exists():
        raise ValueError(f"Received a project path that doesn't exist: {project_path}")
    return foreign_testing.submit(project_path)


if __name__ == "__main__":
    main()
