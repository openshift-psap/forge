# CLI Entrypoint

The CLI entrypoint provides interactive command-line interfaces for FORGE projects.

## Structure

The CLI files typically include:
- Click command groups for logical organization
- Interactive prompts and confirmations
- Development helper functions

## vs CI Entrypoint

| CLI | CI |
|-----|-----|
| Interactive use | Automated execution |
| Development tools | Testing phases |
| Flexible workflows | Standardized phases |

## Implementation

CLI entrypoints use the same Click framework as CI but with:
- More interactive features
- Development-focused commands
- Less standardized structure
- Project-specific workflows
