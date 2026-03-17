#!/usr/bin/env python3
"""
Railway MCP Server
Lets Claude monitor and manage Railway services — check status,
view logs, trigger redeploys, and manage environment variables.
"""

import json
import os
from typing import Optional
import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

# ── Config ────────────────────────────────────────────────────────────────────
RAILWAY_TOKEN   = os.environ.get("RAILWAY_TOKEN", "")
RAILWAY_GQL     = "https://backboard.railway.app/graphql/v2"
REQUEST_TIMEOUT = 30.0

mcp = FastMCP("railway_mcp")

# ── Shared GQL client ─────────────────────────────────────────────────────────

async def _gql(query: str, variables: dict = None) -> dict:
    """Execute a Railway GraphQL query."""
    if not RAILWAY_TOKEN:
        raise ValueError("RAILWAY_TOKEN env var not set.")
    headers = {
        "Authorization": f"Bearer {RAILWAY_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(RAILWAY_GQL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise ValueError(f"GraphQL error: {data['errors'][0]['message']}")
        return data.get("data", {})

def _handle_error(e: Exception) -> str:
    if isinstance(e, ValueError):
        return f"Error: {str(e)}"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401: return "Error: Invalid Railway token. Check RAILWAY_TOKEN env var."
        if code == 429: return "Error: Railway rate limited. Wait and retry."
        return f"Error: Railway API returned HTTP {code}"
    if isinstance(e, httpx.TimeoutException):
        return "Error: Railway API timed out. Try again."
    return f"Error: {type(e).__name__}: {str(e)}"

# ── Input Models ──────────────────────────────────────────────────────────────

class ProjectIdInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    project_id: str = Field(..., description="Railway project ID (UUID)", min_length=1)

class ServiceInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    project_id: str = Field(..., description="Railway project ID", min_length=1)
    service_id: str = Field(..., description="Railway service ID", min_length=1)

class LogsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    deployment_id: str = Field(..., description="Deployment ID (from railway_get_deployments)", min_length=1)
    limit: Optional[int] = Field(default=50, description="Number of log lines to fetch", ge=1, le=500)

class SetVariableInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    project_id: str = Field(..., description="Railway project ID", min_length=1)
    service_id: str = Field(..., description="Railway service ID", min_length=1)
    environment_id: str = Field(..., description="Environment ID (from railway_list_services)", min_length=1)
    name: str = Field(..., description="Variable name (e.g. 'API_KEY')", min_length=1)
    value: str = Field(..., description="Variable value", min_length=0)

class RedeployInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    service_id: str = Field(..., description="Railway service ID to redeploy", min_length=1)
    environment_id: str = Field(..., description="Environment ID for the redeploy", min_length=1)

# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="railway_list_projects",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def railway_list_projects() -> str:
    """List all Railway projects in your account.

    Returns:
        str: JSON list of projects with id, name, description
    """
    query = """
    query {
      me {
        projects {
          edges {
            node {
              id
              name
              description
              createdAt
              updatedAt
            }
          }
        }
      }
    }
    """
    try:
        data = await _gql(query)
        edges = data.get("me", {}).get("projects", {}).get("edges", [])
        projects = [e["node"] for e in edges]
        return json.dumps({"count": len(projects), "projects": projects}, indent=2, default=str)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="railway_list_services",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def railway_list_services(params: ProjectIdInput) -> str:
    """List all services in a Railway project, including their environment IDs.

    Args:
        params (ProjectIdInput):
            - project_id (str): Railway project ID

    Returns:
        str: JSON list of services with id, name, and environment info
    """
    query = """
    query($projectId: String!) {
      project(id: $projectId) {
        name
        services {
          edges {
            node {
              id
              name
              createdAt
              serviceInstances {
                edges {
                  node {
                    environmentId
                    startCommand
                    domains {
                      serviceDomains {
                        domain
                      }
                    }
                  }
                }
              }
            }
          }
        }
        environments {
          edges {
            node {
              id
              name
            }
          }
        }
      }
    }
    """
    try:
        data = await _gql(query, {"projectId": params.project_id})
        project = data.get("project", {})
        service_edges = project.get("services", {}).get("edges", [])
        env_edges = project.get("environments", {}).get("edges", [])

        services = []
        for e in service_edges:
            svc = e["node"]
            instances = svc.get("serviceInstances", {}).get("edges", [])
            domains = []
            env_ids = []
            for inst in instances:
                n = inst["node"]
                env_ids.append(n.get("environmentId"))
                for d in n.get("domains", {}).get("serviceDomains", []):
                    domains.append(d.get("domain"))
            services.append({
                "id": svc["id"],
                "name": svc["name"],
                "environment_ids": env_ids,
                "domains": domains,
                "created_at": svc.get("createdAt")
            })

        environments = [e["node"] for e in env_edges]

        return json.dumps({
            "project_name": project.get("name"),
            "environments": environments,
            "services": services
        }, indent=2, default=str)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="railway_get_deployments",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def railway_get_deployments(params: ServiceInput) -> str:
    """Get recent deployments for a Railway service. Use deployment IDs for logs.

    Args:
        params (ServiceInput):
            - project_id (str): Railway project ID
            - service_id (str): Railway service ID

    Returns:
        str: JSON list of recent deployments with status, created time, deployment ID
    """
    query = """
    query($serviceId: String!, $projectId: String!) {
      deployments(
        input: { serviceId: $serviceId, projectId: $projectId }
        last: 5
      ) {
        edges {
          node {
            id
            status
            createdAt
            updatedAt
            meta
            url
          }
        }
      }
    }
    """
    try:
        data = await _gql(query, {
            "serviceId": params.service_id,
            "projectId": params.project_id
        })
        edges = data.get("deployments", {}).get("edges", [])
        deployments = [e["node"] for e in edges]
        return json.dumps({
            "count": len(deployments),
            "deployments": deployments
        }, indent=2, default=str)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="railway_get_logs",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def railway_get_logs(params: LogsInput) -> str:
    """Get logs for a Railway deployment.

    Args:
        params (LogsInput):
            - deployment_id (str): Deployment ID (get from railway_get_deployments)
            - limit (int): Number of log lines (default 50, max 500)

    Returns:
        str: Recent log lines as formatted text
    """
    query = """
    query($deploymentId: String!) {
      deploymentLogs(deploymentId: $deploymentId) {
        message
        severity
        timestamp
      }
    }
    """
    try:
        data = await _gql(query, {"deploymentId": params.deployment_id})
        logs = data.get("deploymentLogs", [])[-params.limit:]
        if not logs:
            return "No logs found for this deployment."
        lines = []
        for log in logs:
            ts = str(log.get("timestamp", ""))[:19]
            sev = log.get("severity", "INFO")
            msg = log.get("message", "")
            lines.append(f"[{ts}] {sev}: {msg}")
        return "\n".join(lines)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="railway_redeploy",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
)
async def railway_redeploy(params: RedeployInput) -> str:
    """Trigger a redeploy of a Railway service.

    Args:
        params (RedeployInput):
            - service_id (str): Railway service ID
            - environment_id (str): Environment ID (get from railway_list_services)

    Returns:
        str: Success message with new deployment ID, or error
    """
    mutation = """
    mutation($serviceId: String!, $environmentId: String!) {
      serviceInstanceRedeploy(
        serviceId: $serviceId
        environmentId: $environmentId
      )
    }
    """
    try:
        await _gql(mutation, {
            "serviceId": params.service_id,
            "environmentId": params.environment_id
        })
        return f"✅ Redeploy triggered for service '{params.service_id}'. Check railway_get_deployments for status."
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="railway_list_variables",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def railway_list_variables(params: ServiceInput) -> str:
    """List environment variables for a Railway service.

    Args:
        params (ServiceInput):
            - project_id (str): Railway project ID
            - service_id (str): Railway service ID

    Returns:
        str: JSON dict of variable names and values
    """
    query = """
    query($projectId: String!, $serviceId: String!, $environmentId: String) {
      variables(
        projectId: $projectId
        serviceId: $serviceId
        environmentId: $environmentId
      )
    }
    """
    try:
        # Get environment ID first
        svc_data_raw = await _gql("""
        query($projectId: String!) {
          project(id: $projectId) {
            environments { edges { node { id name } } }
          }
        }
        """, {"projectId": params.project_id})
        envs = svc_data_raw.get("project", {}).get("environments", {}).get("edges", [])
        env_id = envs[0]["node"]["id"] if envs else None

        data = await _gql(query, {
            "projectId": params.project_id,
            "serviceId": params.service_id,
            "environmentId": env_id
        })
        variables = data.get("variables", {})
        # Mask sensitive values
        masked = {}
        sensitive_keys = {"key", "secret", "password", "token", "api", "auth", "pass"}
        for k, v in variables.items():
            is_sensitive = any(s in k.lower() for s in sensitive_keys)
            masked[k] = f"***{v[-4:]}" if is_sensitive and len(str(v)) > 4 else v
        return json.dumps({"count": len(masked), "variables": masked}, indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="railway_set_variable",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
)
async def railway_set_variable(params: SetVariableInput) -> str:
    """Set or update an environment variable for a Railway service.

    Args:
        params (SetVariableInput):
            - project_id, service_id, environment_id (str): Railway IDs
            - name (str): Variable name (e.g. 'API_KEY')
            - value (str): Variable value

    Returns:
        str: Success or error message
    """
    mutation = """
    mutation($input: VariableUpsertInput!) {
      variableUpsert(input: $input)
    }
    """
    try:
        await _gql(mutation, {"input": {
            "projectId": params.project_id,
            "serviceId": params.service_id,
            "environmentId": params.environment_id,
            "name": params.name,
            "value": params.value
        }})
        return f"✅ Variable '{params.name}' set successfully. Redeploy the service for changes to take effect."
    except Exception as e:
        return _handle_error(e)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    print(f"🚂 Railway MCP Server → http://0.0.0.0:{port}")
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="0.0.0.0", port=port)
