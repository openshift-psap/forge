"""
Base CI app builder for agentic testing projects.

Provides a class with overridable hooks so each project can customize
initialization, vault resolution, and command registration while
sharing the common wiring logic.
"""

from __future__ import annotations

import functools
import importlib
import logging
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import click

from projects.core.ci_entrypoint.fournos_resolve import create_fournos_resolve_entrypoint
from projects.core.library import ci as ci_lib
from projects.core.library import config, env, run, vault
from projects.core.library.export import caliper_export_entrypoint
from projects.core.library.replot import caliper_replot_entrypoint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhaseSpec:
    """Typed specification for a CI phase command."""

    func: str
    help: str | None = None


def _import_function(dotted_path: str) -> Callable:
    """Import a function from a dotted path like 'projects.foo.bar:my_func'."""
    module_path, func_name = dotted_path.rsplit(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


class CIApp:
    """Extensible CI app builder for FORGE projects.

    Subclass and override any hook to customize behaviour.  The default
    implementation covers the common case (init env/run/config, resolve
    vaults from config YAML, register phase commands).

    Minimal usage (no subclassing needed)::

        main = CIApp(
            project_name="mcp_gateway",
            description="MCP Gateway CI.",
            config_dir=Path(__file__).parent,
            phases={"test": PhaseSpec(func="my.mod:run", help="Run tests")},
        ).build()

    Override hooks for advanced cases::

        class MyCIApp(CIApp):
            def init_vaults(self, phase: str) -> None:
                # preset-aware vault selection
                ...

            def register_extra_commands(self, group: click.Group) -> None:
                # add project-specific commands
                ...

        main = MyCIApp(...).build()
    """

    def __init__(
        self,
        *,
        project_name: str,
        description: str,
        config_dir: Path,
        phases: dict[str, PhaseSpec],
        hardware_resolver_func: Callable[[dict], dict] | None = None,
    ) -> None:
        self.project_name = project_name
        self.description = description
        self.config_dir = config_dir
        self.phases = phases
        self.hardware_resolver_func = hardware_resolver_func

    # ------------------------------------------------------------------
    # Overridable hooks
    # ------------------------------------------------------------------

    def init(self, phase: str | None = None) -> None:
        """Bootstrap env, run, and config.  Override to add custom init."""
        env.init()
        run.init()
        config.init(self.config_dir)

    def init_vaults(self, phase: str) -> None:
        """Resolve and initialize vaults for *phase*.

        Override to implement preset-aware vault selection or any other
        custom logic (e.g. different vaults per preset).
        """
        global_mandatory = self._get_vaults_for_key("all")
        phase_mandatory = self._get_vaults_for_key(phase)
        mandatory_vaults = global_mandatory + phase_mandatory

        global_optional = self._get_vaults_for_key("all-optional")
        phase_optional = self._get_vaults_for_key(f"{phase}-optional")
        optional_vaults = global_optional + phase_optional

        if not mandatory_vaults and not optional_vaults:
            logger.info("No vaults to initialize for phase '%s'", phase)
            return

        vault.init(mandatory_vaults=mandatory_vaults, optional_vaults=optional_vaults)

    def list_vaults(self) -> list[str]:
        """Return a flat, deduplicated list of all vault names in config.

        Used by the fournos-resolve entrypoint.  Override if vault
        enumeration needs custom logic.
        """
        vault_config = config.project.get_config("vaults")
        if isinstance(vault_config, list):
            return vault_config
        all_vaults: list[str] = []
        for _category, vaults in vault_config.items():
            if isinstance(vaults, list):
                all_vaults.extend(vaults)
        return list(dict.fromkeys(all_vaults))

    def should_init_vaults(self, phase: str) -> bool:
        """Return False to skip vault init for certain phases."""
        return phase != "resolve-fournos-config"

    def register_extra_commands(self, group: click.Group) -> None:
        """Hook to register additional click commands on the group.

        Override to add project-specific commands (e.g. decorators,
        agent-review commands, etc.) without modifying the base class.
        """

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_vaults_for_key(self, key: str) -> list[str]:
        return config.project.get_config(f"vaults.{key}", [])

    def _resolve_notification_provider(self):
        """Auto-discover notification provider from config.

        Reads ``notifications.slack.provider_module`` (a fully-qualified
        class path like ``projects.foo.notifications.MyProvider``) and
        instantiates it.  Returns None if not configured.
        """
        provider_path = config.project.get_config(
            "notifications.slack.provider_module", None, print=False, warn=False
        )
        if not provider_path:
            return None

        try:
            module_path, class_name = provider_path.rsplit(".", 1)
            mod = importlib.import_module(module_path)
            provider_cls = getattr(mod, class_name)
            return provider_cls()
        except Exception as e:
            logger.warning("Failed to resolve notification provider '%s': %s", provider_path, e)
            return None

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self) -> click.Group:
        """Assemble and return the fully-wired click.Group."""
        app = self

        @click.group()
        @click.pass_context
        @ci_lib.safe_ci_function
        def main(ctx):
            ctx.ensure_object(types.SimpleNamespace)
            app.init(phase=ctx.invoked_subcommand)

            if not app.should_init_vaults(ctx.invoked_subcommand):
                logger.info("Skipping vault init for '%s'", ctx.invoked_subcommand)
                return

            app.init_vaults(ctx.invoked_subcommand)

            ctx.obj.notification_provider = app._resolve_notification_provider()

        main.help = self.description

        for cmd_name, spec in self.phases.items():
            _register_phase_command(main, cmd_name, spec)

        main.add_command(
            create_fournos_resolve_entrypoint(
                vault_list_func=self.list_vaults,
                hardware_resolver_func=self.hardware_resolver_func,
            )
        )
        main.add_command(caliper_export_entrypoint)
        main.add_command(caliper_replot_entrypoint)

        self.register_extra_commands(main)

        return main


def _register_phase_command(
    group: click.Group,
    cmd_name: str,
    spec: PhaseSpec,
) -> None:
    """Register a lazily-imported phase function as a click command."""

    @click.pass_context
    @ci_lib.safe_ci_entrypoint
    @functools.wraps(_import_function)
    def command(ctx, _spec=spec):
        fn = _import_function(_spec.func)
        return fn()

    command.__name__ = cmd_name.replace("-", "_")
    if spec.help:
        command.__doc__ = spec.help

    group.command(name=cmd_name)(command)
