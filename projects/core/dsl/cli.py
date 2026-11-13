"""
CLI argument parsing utilities for the DSL framework
"""

import argparse
import inspect
import json
import re
from typing import get_origin


def _parse_docstring_args(docstring: str) -> dict:
    """Parse function docstring to extract argument descriptions"""
    if not docstring:
        return {}

    # Look for Args: section
    args_match = re.search(r"Args:\s*\n(.*?)(?:\n\s*\n|\n\s*[A-Z]|\Z)", docstring, re.DOTALL)
    if not args_match:
        return {}

    args_section = args_match.group(1)
    arg_descriptions = {}

    # Parse each argument line
    for line in args_section.split("\n"):
        line = line.strip()
        if ":" not in line:
            continue

        # Extract "param_name: description" format
        param_match = re.match(r"(\w+):\s*(.*)", line)
        if not param_match:
            continue

        param_name = param_match.group(1)
        description = param_match.group(2)
        arg_descriptions[param_name] = description

    return arg_descriptions


def _parse_list_value(value_str: str) -> list:
    """Parse list from command line argument (JSON or comma-separated)"""
    if not value_str:
        return []

    # Try JSON first
    try:
        parsed = json.loads(value_str)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # Fall back to comma-separated values
    return [item.strip() for item in value_str.split(",") if item.strip()]


def _parse_dict_value(value_str: str) -> dict:
    """Parse dict from command line argument (JSON or key=value pairs)"""
    if not value_str:
        return {}

    # Try JSON first
    try:
        parsed = json.loads(value_str)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # Fall back to key=value pairs separated by commas
    result = {}
    for pair in value_str.split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            result[key.strip()] = value.strip()
        else:
            raise ValueError(f"Invalid key=value pair: {pair}")
    return result


def create_dynamic_parser(func, positional_args=None) -> argparse.ArgumentParser:
    """
    Create argparse parser dynamically from function signature and docstring

    Args:
        func: Function to generate CLI for
        positional_args: List of parameter names to make positional (default: auto-detect)
    """

    # Get function signature and docstring
    sig = inspect.signature(func)
    docstring = inspect.getdoc(func)

    # Parse descriptions from docstring
    arg_descriptions = _parse_docstring_args(docstring)

    # Auto-detect positional args if not specified
    if positional_args is None:
        # Make first 2 parameters positional if they're commonly required
        param_names = [name for name in sig.parameters.keys() if name not in ("self", "cls")]
        positional_args = param_names[:2]  # First two parameters

    # Get main description from docstring
    main_description = docstring.split("\n")[0] if docstring else f"CLI for {func.__name__}"

    # Create parser
    parser = argparse.ArgumentParser(
        description=main_description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Helper function to resolve annotations
    def resolve_annotation(annotation):
        """Resolve string annotations to actual types"""
        if isinstance(annotation, str):
            # Handle string annotations from __future__ import annotations
            type_map = {
                "int": int,
                "str": str,
                "float": float,
                "bool": bool,
                "list": list,
                "dict": dict,
            }

            # Handle generic types like list[str], dict[str, str]
            if "[" in annotation:
                base_type = annotation.split("[")[0]
                return type_map.get(base_type, str)

            return type_map.get(annotation, str)

        # Handle actual type objects (when not using string annotations)
        origin = get_origin(annotation)
        if origin is list:
            return list
        elif origin is dict:
            return dict

        return annotation

    # Add arguments based on function signature
    for param_name, param in sig.parameters.items():
        # Skip self/cls parameters
        if param_name in ("self", "cls"):
            continue

        # Determine if required (no default value)
        has_default = param.default != inspect.Parameter.empty

        # Get description from docstring
        help_text = arg_descriptions.get(param_name, f"{param_name} parameter")
        if has_default and param.default is not None:
            help_text += f" (default: {param.default})"

        # Determine argument type from annotation
        if param.annotation != inspect.Parameter.empty:
            resolved_annotation = resolve_annotation(param.annotation)

            # Add format hints for complex types to help text
            if resolved_annotation is list:
                help_text += " (format: JSON array or comma-separated values)"
            elif resolved_annotation is dict:
                help_text += " (format: JSON object or key=value,key2=value2)"

            if resolved_annotation is bool:
                # Boolean args come in a --flag / --no-flag pair. Omitting both keeps
                # the function's declared default — a plain store_true flag would
                # clobber a `param: bool = True` default to False when absent, and
                # would make False impossible to request explicitly.
                cli_name = f"--{param_name.replace('_', '-')}"
                parser.add_argument(
                    cli_name,
                    action=argparse.BooleanOptionalAction,
                    dest=param_name,
                    default=param.default if has_default else None,
                    required=not has_default,
                    help=help_text,
                )
                continue
            elif resolved_annotation is int:
                arg_type = int
            elif resolved_annotation is float:
                arg_type = float
            elif resolved_annotation is str:
                arg_type = str
            elif resolved_annotation is list:
                arg_type = _parse_list_value
            elif resolved_annotation is dict:
                arg_type = _parse_dict_value
            else:
                arg_type = str  # Default to string
        else:
            arg_type = str  # Default to string

        # Add as positional or optional argument (or both!)
        if param_name in positional_args:
            # Add positional argument
            nargs = "?" if has_default else None  # Optional if has default
            default = param.default if has_default else None

            parser.add_argument(
                param_name,
                type=arg_type,
                nargs=nargs,
                default=default,
                help=f"{help_text} (positional)",
            )

            # ALSO add named version for flexibility
            cli_name = f"--{param_name.replace('_', '-')}"
            parser.add_argument(
                cli_name,
                type=arg_type,
                dest=param_name,  # Same destination as positional
                help=f"{help_text} (named alternative to positional)",
            )
        else:
            # Optional argument with --flag only
            cli_name = f"--{param_name.replace('_', '-')}"

            parser.add_argument(
                cli_name,
                type=arg_type,
                required=not has_default,
                default=param.default if has_default else None,
                dest=param_name,
                help=help_text,
            )

    return parser
