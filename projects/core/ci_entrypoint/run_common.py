#!/usr/bin/env python3
"""
Common functionality for FORGE CI/CLI orchestration entrypoints
"""

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

from projects.core.ci_entrypoint import prepare_ci


def setup_logging():
    """Set up logging configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


logger = logging.getLogger(__name__)

FORGE_HOME = Path(__file__).resolve().parent.parent.parent.parent

EXTRA_PACKAGES = ["nooa"]

# Global reference to child process for signal forwarding
_child_process = None


def signal_handler_sigint(sig, frame):
    """Handle SIGINT (Ctrl+C) gracefully."""
    logger.info("🚫 Received SIGINT (Ctrl+C) - Interrupting operation...")

    # Forward signal to child process first
    if _child_process and _child_process.poll() is None:  # Child is still running
        logger.info("📡 Forwarding SIGINT to child process...")
        try:
            _child_process.send_signal(signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass  # Child may have already terminated

    # Emergency cleanup of dual output
    prepare_ci.shutdown_dual_output()

    sys.exit(130)  # Standard exit code for SIGINT


def signal_handler_sigterm(sig, frame):
    """Handle SIGTERM gracefully."""
    logger.info("🛑 Received SIGTERM - Terminating operation...")

    # Forward signal to child process first
    if _child_process and _child_process.poll() is None:  # Child is still running
        logger.info("📡 Forwarding SIGTERM to child process...")
        try:
            _child_process.send_signal(signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass  # Child may have already terminated

    # Emergency cleanup of dual output
    prepare_ci.shutdown_dual_output()

    sys.exit(143)  # Standard exit code for SIGTERM


def setup_signal_handlers():
    """Set up signal handlers for graceful interruption."""
    try:
        signal.signal(signal.SIGINT, signal_handler_sigint)
        signal.signal(signal.SIGTERM, signal_handler_sigterm)
        # SIGPIPE handling for broken pipes
        if hasattr(signal, "SIGPIPE"):
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except Exception:
        # Signal handling might not be available on all platforms
        pass


def install_extra_packages(packages):
    if not packages:
        return

    print(f"📦 Installing {'/'.join(packages)} packages...")

    # Try uv first with no-cache to avoid permission issues
    try:
        subprocess.run(
            ["uv", "pip", "install", "--no-cache", *packages],
            check=True,
            capture_output=True,
        )
        print(f"✅ {'/'.join(packages)} packages installed successfully with uv")
        print()
    except (FileNotFoundError, subprocess.CalledProcessError):
        # Fallback to pip with user installation
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--user",
                    "--no-cache-dir",
                    *packages,
                ],
                check=True,
                capture_output=True,
            )
            print(f"✅ {'/'.join(packages)} packages installed successfully with pip")
            print()
        except subprocess.CalledProcessError as pip_error:
            print(f"❌ Failed to install {'/'.join(packages)}: {pip_error}")
            raise RuntimeError("failed to install the extra packages") from pip_error

    # Ensure user site-packages is in path
    import site

    user_site = site.getusersitepackages()
    if user_site not in sys.path:
        sys.path.insert(0, user_site)

    # Also check for common install locations
    import os

    possible_paths = [
        os.path.expanduser("~/.local/lib/python3.11/site-packages"),
        os.path.expanduser("~/.local/lib/python3.12/site-packages"),
        os.path.expanduser("~/.local/lib/python3.13/site-packages"),
        user_site,
    ]

    for path in possible_paths:
        if os.path.exists(path) and path not in sys.path:
            sys.path.insert(0, path)

    # Clear import cache and try again
    import importlib

    importlib.invalidate_caches()


def prepare():
    setup_logging()

    # Install click package using uv (as non-root user)
    install_extra_packages(EXTRA_PACKAGES)

    # Set up ARTIFACT_DIR
    prepare_ci.precheck_artifact_dir()

    if os.isatty(sys.stdin.fileno()):
        # not working very well from a TTY ...
        logger.info("Running from a TTY, not enabling the dual output")
    elif (Path(os.environ["ARTIFACT_DIR"]) / "run.log").exists():
        logger.info("run.log file already exists, not enabling the dual output")
    else:
        # Set up dual output as early as possible
        prepare_ci.setup_dual_output()


def find_project_directory(project_name: str) -> Path | None:
    """
    Find the directory for the specified project.

    Args:
        project_name: Name of the project to find

    Returns:
        Path to project directory if found, None otherwise
    """
    # Look in the projects directory
    projects_dir = FORGE_HOME / "projects"
    project_dir = projects_dir / project_name

    if project_dir.exists() and project_dir.is_dir():
        return project_dir

    return None


def find_script(project_dir: Path, operation: str, *, use_cli: bool = False) -> Path | None:
    """
    Find the appropriate script for the operation.

    Args:
        project_dir: Project directory path
        operation: Operation to perform (e.g., 'ci')
        use_cli: If True, look for cli.py; if False, look for {operation}.py

    Returns:
        Path to script if found, None otherwise
    """
    if use_cli:
        # Check possible locations for CLI scripts
        possible_locations = [
            project_dir / "orchestration" / "cli.py",
            project_dir / "cli.py",
        ]
    else:
        # Check possible locations for operation scripts
        possible_locations = [
            project_dir / "orchestration" / f"{operation}.py",
        ]

    for script_path in possible_locations:
        if script_path.exists() and os.access(script_path, os.X_OK):
            return script_path

    return None


def get_available_projects(*, use_cli: bool = False) -> list[str]:
    """Get list of available projects."""

    projects_dir = FORGE_HOME / "projects"

    if not projects_dir.exists():
        return []

    projects = []
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        if proj_dir.name.startswith("."):
            continue

        if use_cli:
            # Only include projects that have a cli.py file
            cli_script = find_script(proj_dir, "", use_cli=True)
            if cli_script:
                projects.append(proj_dir.name)
        else:
            # Only include projects that have an orchestration directory
            orchestration_dir = proj_dir / "orchestration"
            if orchestration_dir.exists() and orchestration_dir.is_dir():
                projects.append(proj_dir.name)

    return sorted(projects)


def parse_cli_help(help_output: str) -> list[str]:
    """Parse CLI help output to extract available commands."""
    operations = []
    in_commands_section = False

    lines = help_output.split("\n")
    for line in lines:
        line = line.strip()

        # Look for "Commands:" section
        if line.lower().startswith("commands:"):
            in_commands_section = True
            continue

        # Stop when we hit another section
        if in_commands_section and line and not line.startswith(" "):
            break

        # Extract command names
        if in_commands_section and line.startswith(" "):
            # Format is typically: "  command_name  Description"
            parts = line.split()
            if parts:
                command_name = parts[0].strip()
                # Skip common help/utility commands
                if command_name not in ["--help", "--version", "-h", "-v"]:
                    operations.append(command_name)

    return operations


def execute_project_operation(
    project: str,
    operation: str,
    args: tuple,
    verbose: bool = False,
    dry_run: bool = False,
    do_prepare_ci: bool = True,
    use_cli: bool = False,
):
    """Execute a project operation."""

    if not (isinstance(args, list) or isinstance(args, tuple)):
        raise ValueError(f"Args={args} must be a list, not {args.__class__.__name__}")

    mode_name = "CLI" if use_cli else "CI"

    if verbose:
        click.echo("")
        click.echo(f"🚀 FORGE {mode_name} Orchestration")
        click.echo(f"Project: {project}")
        click.echo(f"Operation: {operation}")
        click.echo(f"Arguments: {' '.join(args)}")
        click.echo("")

    # Execute CI preparation tasks
    if do_prepare_ci:
        try:
            prepare_ci.prepare(
                verbose=verbose,
                project=project,
                operation=operation,
                args=list(args),
            )
        except:
            click.echo(click.style(f"❌ ERROR: {mode_name} preparation failed", fg="red"), err=True)
            raise
    else:
        logger.warning(f"{mode_name} preparation not enabled, skipping preparation")

    # Find project directory
    project_dir = find_project_directory(project)
    if not project_dir:
        click.echo(click.style(f"❌ ERROR: Project '{project}' not found.", fg="red"), err=True)

        available_projects = get_available_projects(use_cli=use_cli)
        if available_projects:
            click.echo("\n📂 Available projects:")
            for proj in available_projects:
                click.echo(f"   • {proj}")
        else:
            click.echo("📂 No projects found in projects/ directory")

        sys.exit(1)

    # Find script
    script = find_script(project_dir, operation, use_cli=use_cli)
    if not script:
        if use_cli:
            click.echo(
                click.style(
                    f"❌ ERROR: No CLI script found for project '{project}'.",
                    fg="red",
                ),
                err=True,
            )
            click.echo(f"🔍 Expected: {project_dir}/orchestration/cli.py or {project_dir}/cli.py")
        else:
            script_path = project_dir.relative_to(FORGE_HOME) / "orchestration" / f"{operation}.py"

            if script_path.exists():
                click.echo(
                    click.style(
                        f"❌ ERROR: CI script exists but is not executable for project '{project}' operation '{operation}'.",
                        fg="red",
                    ),
                    err=True,
                )
                click.echo(f"💡 Fix with: chmod +x {script_path}")
            else:
                click.echo(
                    click.style(
                        f"❌ ERROR: No CI script '{script_path}' found for project '{project}' operation '{operation}'.",
                        fg="red",
                    ),
                    err=True,
                )
                click.echo(f"🔍 Expected: {script_path}")
        sys.exit(1)

    # Prepare command
    if use_cli:
        # CLI mode: pass operation as argument
        cmd = [sys.executable, str(script), operation] + list(args)
    else:
        # CI mode: don't pass operation as it's just the script name
        cmd = [sys.executable, str(script)] + list(args)

    if verbose or dry_run:
        click.echo("\n🔧 Execution Details:")
        click.echo(f"   Command: {' '.join(cmd)}")
        click.echo(f"   Working Directory: {Path.cwd()}")
        click.echo(f"   Script: {script}")

    if dry_run:
        click.echo("\n🧪 DRY RUN: Would execute the above command")
        click.echo("✨ Use --verbose to see execution details without --dry-run")
        return

    # Execute the command
    click.echo("")
    click.echo(f"▶️  Executing {project} {operation} {' '.join(args)} | {' '.join(cmd)}")
    click.echo("")

    try:
        # Track start time for duration calculation
        start_time = time.time()

        # Set up signal handlers for graceful interruption (safe to call multiple times)
        setup_signal_handlers()

        # Use Popen to get access to process object for signal forwarding
        global _child_process
        _child_process = subprocess.Popen(
            cmd,
            stdin=None,  # Inherit stdin for pdb/debugging
            stdout=None,  # Inherit stdout for pdb/debugging
            stderr=None,  # Inherit stderr for pdb/debugging
        )

        # Wait for process to complete
        result_code = _child_process.wait()

        # Create result object similar to subprocess.run()
        class Result:
            def __init__(self, returncode):
                self.returncode = returncode

        result = Result(result_code)
        click.echo()
        click.echo(
            f"▶️  Execution of {project} {operation} {' '.join(args)} returned {result.returncode}"
        )
        click.echo()

        finish_reason = (
            prepare_ci.FinishReason.SUCCESS
            if result.returncode == 0
            else prepare_ci.FinishReason.ERROR
        )

        success = finish_reason == prepare_ci.FinishReason.SUCCESS
        # Post-execution checks and status reporting
        status_message = prepare_ci.postchecks(
            project, operation, start_time, finish_reason, list(args)
        )
        msg = click.style(status_message, fg="green" if success else "red")

        click.echo()
        click.echo(msg, err=not success)

        # Properly shutdown dual output to flush all buffers and terminate daemon
        prepare_ci.shutdown_dual_output()

        sys.exit(result.returncode)

    except Exception as e:
        logger.exception(f"Unexpected exception {e.__class__.__name__}: {e}")

        # Emergency cleanup of dual output to prevent hanging
        try:
            prepare_ci.shutdown_dual_output()
        except Exception:
            pass  # Don't let cleanup errors mask the original error

        click.echo(
            click.style("❌ ERROR: Unexpected error during execution", fg="red"),
            err=True,
        )
        sys.exit(1)


def list_projects(*, use_cli: bool = False):
    """List all available projects."""
    projects = get_available_projects(use_cli=use_cli)

    if not projects:
        mode_name = "CLI scripts" if use_cli else "projects"
        click.echo(f"📂 No {mode_name} found")
        return

    click.echo("📂 Available projects:")
    for project in projects:
        project_dir = find_project_directory(project)
        if use_cli:
            script = find_script(project_dir, "", use_cli=True)
        else:
            script = find_script(project_dir, "ci", use_cli=False)
        status = "✅" if script else "⚠️"

        click.echo(f"   {status} {project}")

    click.echo()
    click.echo("Usage:")
    if use_cli:
        click.echo("   run_cli <project> <operation>  # Execute project CLI operation")
        click.echo("   run_cli projects               # List projects explicitly")
    else:
        click.echo("   run <project> <operation>  # Execute project operation")
        click.echo("   run projects               # List projects explicitly")
    click.echo()
    click.echo("Examples:")
    script_name = "run_cli" if use_cli else "run"
    click.echo(f"   {script_name} {projects[0]} prepare")
    click.echo(f"   {script_name} {projects[0]} test")
    if len(projects) > 1:
        click.echo(f"   {script_name} {projects[1]} validate")


def show_project_operations(project: str, *, use_cli: bool = False):
    """Show available operations for a project."""
    click.echo(f"🔧 Available operations for project '{project}':")

    # Find project directory
    project_dir = find_project_directory(project)
    if not project_dir:
        click.echo(click.style(f"❌ ERROR: Project '{project}' not found.", fg="red"), err=True)
        return

    if use_cli:
        # CLI mode: query cli.py --help
        cli_script = find_script(project_dir, "", use_cli=True)
        if not cli_script:
            click.echo(
                click.style(f"❌ ERROR: No CLI script found for project '{project}'.", fg="red"),
                err=True,
            )
            return

        # Query CLI script for available commands
        try:
            import subprocess

            cmd = [sys.executable, str(cli_script), "--help"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

            if result.returncode == 0:
                operations = parse_cli_help(result.stdout)
                if operations:
                    click.echo()
                    click.echo("📄 Available operations:")
                    for operation in operations:
                        click.echo(f"   📝 {operation}")
                else:
                    click.echo("⚠️  No operations found in CLI help output")
                    click.echo("Raw help output:")
                    click.echo(result.stdout)
            else:
                click.echo(f"❌ Failed to get help from CLI script: {result.stderr}")

        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            click.echo(f"❌ Error querying CLI script: {e}")

        click.echo(f"Usage: run_cli {project} <operation>")
        click.echo()
        click.echo("Examples:")
        click.echo(f"   run_cli {project} --help")
        click.echo(f"   run_cli {project} prepare")

    else:
        # CI mode: list Python files in orchestration directory
        python_files = []

        def add_python_file(file_path):
            if not file_path.is_file():
                return
            # Skip files that are not executable
            if not os.access(file_path, os.X_OK):
                return

            operation_name = file_path.stem  # filename without .py extension
            python_files.append((operation_name, file_path))

        # List Python files in the orchestration directory
        for file_path in (project_dir / "orchestration").glob("*.py"):
            add_python_file(file_path)

        if not python_files:
            click.echo("⚠️  No Python files found in project directory")
            click.echo(f"📁 Project directory: {project_dir}")
            return

        click.echo()
        click.echo("📄 Available Python files:")

        operation_files = []

        for operation_name, file_path in sorted(python_files):
            operation_files.append((operation_name, file_path))

        for operation_name, _file_path in operation_files:
            click.echo(f"   📝 {operation_name}.py")

        click.echo(f"Usage: run {project} <filename_without_py>")
        click.echo()
        click.echo("Examples:")
        for operation_name, _ in python_files[:3]:
            click.echo(f"   run {project} {operation_name}")


def main_orchestrator(*, use_cli: bool = False):
    """Main orchestration function that handles both CI and CLI modes."""
    import click

    @click.command()
    @click.argument("project", required=False)
    @click.argument("operation", required=False)
    @click.argument("args", nargs=-1)
    @click.option("--verbose", "-v", is_flag=True, help="Enable verbose output", default=True)
    @click.option("--dry-run", is_flag=True, help="Show what would be executed without running it")
    def main(project, operation, args, verbose, dry_run):
        """
        FORGE Orchestration Entrypoint.

        \\b
        Usage:
            run_cli                           # List available projects
            run_cli <project> <operation>     # Execute project operation
            run_cli projects                  # Explicit project listing

        \\b
        Examples:
            run_cli llm_d prepare
            run_cli llm_d test
            run_cli skeleton validate
        """

        prepare()

        # No arguments - list projects
        if not project:
            list_projects(use_cli=use_cli)
            return

        # Special case: explicit "projects" command
        if project == "projects":
            list_projects(use_cli=use_cli)
            return

        # Need operation for project execution - show available operations
        if not operation:
            show_project_operations(project, use_cli=use_cli)
            sys.exit(1)

        # Execute project operation
        execute_project_operation(project, operation, args, verbose, dry_run, use_cli=use_cli)

    return main
