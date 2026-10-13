#!/usr/bin/env python3
"""
Unified entrypoint for all FORGE toolbox commands.

This script dynamically discovers toolbox commands across all projects
and creates a hierarchical CLI structure:

    forge-toolbox <project> <command> [args...]

Examples:
    forge-toolbox cluster cluster-deploy-operator redis llm-operators redis llm operators
    forge-toolbox llm_d deploy-llmisvc my-model llm-namespace
    forge-toolbox jump_ci take-lock my-lock
"""

import importlib.util
import os
import sys
from pathlib import Path


def get_forge_home() -> Path:
    """Get the FORGE_HOME directory."""
    forge_home = os.environ.get("FORGE_HOME")
    if forge_home:
        return Path(forge_home)

    # Try to find FORGE_HOME by looking for the projects directory
    current = Path(__file__).parent
    while current != current.parent:
        if (current / "projects").is_dir():
            return current
        current = current.parent

    raise RuntimeError(
        "Could not determine FORGE_HOME. Please set the FORGE_HOME environment variable."
    )


def discover_projects() -> dict[str, Path]:
    """
    Discover all projects that have toolbox directories.

    Returns:
        Dict mapping project names to their paths
    """
    forge_home = get_forge_home()
    projects_dir = forge_home / "projects"

    projects = {}
    for project_path in projects_dir.iterdir():
        if project_path.is_dir() and project_path.name != "__pycache__":
            toolbox_dir = project_path / "toolbox"
            if toolbox_dir.is_dir():
                projects[project_path.name] = project_path

    return projects


def discover_commands(project_path: Path) -> list[str]:
    """
    Discover all toolbox commands in a project.

    Args:
        project_path: Path to the project directory

    Returns:
        List of command names
    """
    toolbox_dir = project_path / "toolbox"
    commands = []

    for command_path in toolbox_dir.iterdir():
        if (
            command_path.is_dir()
            and command_path.name != "__pycache__"
            and (command_path / "main.py").exists()
        ):
            commands.append(command_path.name)

    commands.sort()
    return commands


def get_command_module_path(project_name: str, command_name: str) -> str:
    """
    Get the Python module path for a toolbox command.

    Args:
        project_name: Name of the project
        command_name: Name of the command

    Returns:
        Python module path (e.g., "projects.cluster.toolbox.build_image.main")
    """
    return f"projects.{project_name}.toolbox.{command_name}.main"


def execute_toolbox_command(project_name: str, command_name: str, args: list[str]):
    """
    Execute a toolbox command by importing its module and using the proper toolbox framework.

    Args:
        project_name: Name of the project
        command_name: Name of the command
        args: Command line arguments to pass to the command
    """
    module_path = get_command_module_path(project_name, command_name)

    try:
        # Import the command module
        module = importlib.import_module(module_path)
    except ImportError as e:
        print(f"Error: Could not import command module {module_path}: {e}", file=sys.stderr)
        sys.exit(1)

    # Check if the module has a run function (the actual toolbox command)
    if not hasattr(module, "run"):
        print(f"Error: {module_path} doesn't have a 'run' method", file=sys.stderr)
        sys.exit(1)

    # Use the proper toolbox framework flow for type-safe argument parsing
    try:
        # Import the toolbox framework
        from projects.core.dsl.toolbox import run_toolbox_command

        # Set up sys.argv to match what the command expects
        original_argv = sys.argv
        try:
            sys.argv = [f"{project_name}-{command_name}"] + args
            run_toolbox_command(module.run)
        finally:
            sys.argv = original_argv

    except SystemExit:
        # Let the command handle its own exit codes
        raise
    except Exception as e:
        print(f"Error executing command: {e}", file=sys.stderr)
        sys.exit(1)


def show_usage():
    """Show usage information for the unified toolbox."""
    print("FORGE Toolbox - Unified interface for all toolbox commands")
    print()
    print("Usage:")
    print("  forge-toolbox <project> <command> [args...]")
    print("  forge-toolbox list                          # List all available commands")
    print("  forge-toolbox --help                        # Show this help")
    print()
    print("Examples:")
    print("  forge-toolbox cluster cluster-deploy-operator redis llm-operators redis")
    print("  forge-toolbox llm_d deploy-llmisvc my-model llm-namespace")
    print("  forge-toolbox list")


def show_project_usage(project_name: str, commands: list[str]):
    """Show usage for a specific project."""
    print(f"{project_name.title().replace('_', ' ')} project toolbox commands")
    print()
    print("Available commands:")
    for command in commands:
        description = get_command_description(project_name, command)
        print(f"  {command.replace('_', '-'):30} - {description}")
    print()
    print(f"Usage: run_toolbox.py {project_name} <command> [args...]")
    print(f"Example: run_toolbox.py {project_name} {commands[0].replace('_', '-')} --help")


def get_command_description(project_name: str, command_name: str) -> str:
    """
    Extract the description from a toolbox command's main function docstring.

    Args:
        project_name: Name of the project
        command_name: Name of the command

    Returns:
        First line of the docstring or a default message
    """
    try:
        # Temporarily add FORGE_HOME to path for imports
        forge_home = get_forge_home()
        path_added = False
        if str(forge_home) not in sys.path:
            sys.path.insert(0, str(forge_home))
            path_added = True

        try:
            module_path = get_command_module_path(project_name, command_name)
            module = importlib.import_module(module_path)

            # Look for the run function (standard entrypoint)
            if hasattr(module, "run") and hasattr(module.run, "__doc__") and module.run.__doc__:
                # Get the first line of the docstring, stripped
                first_line = module.run.__doc__.strip().split("\n")[0].strip()
                return first_line if first_line else "No description available"

            return "No description available"
        finally:
            # Clean up path if we added it
            if path_added and str(forge_home) in sys.path:
                sys.path.remove(str(forge_home))

    except Exception:
        return "No description available"


def get_project_description(project_name: str) -> str:
    """
    Extract the description from a project's toolbox __init__.py docstring.

    Args:
        project_name: Name of the project

    Returns:
        First line of the module docstring or empty string
    """
    try:
        # Temporarily add FORGE_HOME to path for imports
        forge_home = get_forge_home()
        path_added = False
        if str(forge_home) not in sys.path:
            sys.path.insert(0, str(forge_home))
            path_added = True

        try:
            module_path = f"projects.{project_name}.toolbox"
            module = importlib.import_module(module_path)

            # Get the module docstring
            if hasattr(module, "__doc__") and module.__doc__:
                # Get the first line of the docstring, stripped
                first_line = module.__doc__.strip().split("\n")[0].strip()
                return first_line if first_line else ""

            return ""
        finally:
            # Clean up path if we added it
            if path_added and str(forge_home) in sys.path:
                sys.path.remove(str(forge_home))

    except Exception:
        return ""


def list_commands():
    """List all available projects and their toolbox commands."""
    print("Available toolbox commands:")
    print()

    projects = discover_projects()
    for project_name, project_path in sorted(projects.items()):
        commands = discover_commands(project_path)
        if commands:
            project_desc = get_project_description(project_name)
            if project_desc:
                print(f"  {project_name}: {project_desc}")
            else:
                print(f"  {project_name}:")
            for command in commands:
                description = get_command_description(project_name, command)
                print(f"    {command.replace('_', '-'):30} - {description}")
            print()


def main():
    """Main entry point for the unified toolbox CLI."""
    try:
        # Parse command line arguments manually
        args = sys.argv[1:]

        if not args:
            list_commands()
            return

        if args[0] in ["--help", "-h", "help"]:
            show_usage()
            return

        if args[0] == "list":
            list_commands()
            return

        # Discover available projects and commands
        projects = discover_projects()
        project_name = args[0]

        # Check if project exists (allow dash/underscore and prefix abbreviations)
        if project_name not in projects:
            input_segs = project_name.replace("-", "_").split("_")
            matches = [
                proj
                for proj in projects
                if len(proj.replace("-", "_").split("_")) == len(input_segs)
                and all(
                    ps.startswith(is_)
                    for is_, ps in zip(input_segs, proj.replace("-", "_").split("_"), strict=True)
                )
            ]
            if len(matches) == 1:
                project_name = matches[0]
            elif len(matches) > 1:
                print(
                    f"Error: Project '{project_name}' is ambiguous: {', '.join(sorted(matches))}",
                    file=sys.stderr,
                )
                sys.exit(1)

        if project_name not in projects:
            print(f"Error: Project '{project_name}' not found", file=sys.stderr)
            print()
            print("Available projects:")
            for proj in sorted(projects.keys()):
                commands = discover_commands(projects[proj])
                if commands:
                    print(f"  {proj}")
            sys.exit(1)

        commands = discover_commands(projects[project_name])

        if not commands:
            print(f"Error: No toolbox commands found in project '{project_name}'", file=sys.stderr)
            sys.exit(1)

        # If only project name is provided, show commands for that project
        if len(args) == 1:
            print(f"{project_name.title().replace('_', ' ')} project toolbox commands:")
            print()
            for command in commands:
                description = get_command_description(project_name, command)
                print(f"  {command.replace('_', '-'):30} - {description}")
            print()
            print(f"Usage: run_toolbox.py {project_name} <command> [args...]")
            return

        if len(args) < 2:
            print("Error: Both project and command must be specified", file=sys.stderr)
            print()
            show_usage()
            sys.exit(1)

        command_name = args[1].replace("-", "_")  # Convert from kebab-case to snake_case
        command_args = args[2:]

        if command_name not in commands:
            print(
                f"Error: Command '{args[1]}' not found in project '{project_name}'", file=sys.stderr
            )
            print()
            show_project_usage(project_name, commands)
            sys.exit(1)

        # Execute the toolbox command
        execute_toolbox_command(project_name, command_name, command_args)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
