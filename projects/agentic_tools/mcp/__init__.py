"""
MCP Server Testing Toolbox

Shared infrastructure for:
- Deploying mock MCP servers on Kubernetes (batch deployment)
- Deploying tokenizer-accurate MCP servers (deterministic tool responses)
- MCP protocol client for load testing (JSON-RPC over HTTP)

Submodules:
    clients/         - MCP protocol client (MCPClient, MCPResponse)
    toolbox/
      deploy_mock_servers/          - Deploy 1..N mock MCP servers programmatically
      deploy_tokenized_mcp_server/  - Deploy tokenizer-accurate MCP server
"""
