"""MCP server that lets a model call SOAP APIs registered in services.json.

Run over stdio (started by mcp_client.py):  python mcp_server.py
Environment:
  SOAP_SERVICES_FILE   service registry (default: services.json next to this file)
  SOAP_ALLOWED_HOSTS   hosts the server may contact (default 127.0.0.1,localhost)

services.json maps a service name to its WSDL (concrete or abstract):
  {"addition": {"wsdl": "http://.../AdditionService?wsdl",
                "description": "Adds two integers",
                "endpoint": "optional; required when the WSDL is abstract"}}
"""

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

import soap_xml

SERVICES_FILE = Path(os.environ.get("SOAP_SERVICES_FILE", Path(__file__).with_name("services.json")))

mcp = MCPServer(
    "soap-gateway",
    instructions=(
        "Gateway to SOAP web services. Call list_soap_operations for a service to see its "
        "operations and argument templates, then call_soap_operation with arguments shaped "
        "exactly like the template."
    ),
)

_wsdl_cache: dict[str, soap_xml.Wsdl] = {}


def _services() -> dict[str, dict[str, Any]]:
    # Re-read on every call so new services are picked up without a restart.
    return json.loads(SERVICES_FILE.read_text())


def _service(name: str) -> dict[str, Any]:
    services = _services()
    if name not in services:
        raise ValueError(f"Unknown service {name!r}. Available: {sorted(services)}")
    return services[name]


def _load_wsdl(name: str, refresh: bool = False) -> soap_xml.Wsdl:
    location = _service(name)["wsdl"]
    if refresh or location not in _wsdl_cache:
        _wsdl_cache[location] = soap_xml.Wsdl(location)
    return _wsdl_cache[location]


@mcp.tool()
def list_soap_services() -> dict[str, str]:
    """List the registered SOAP services and what each one does."""
    return {name: cfg.get("description", "") for name, cfg in _services().items()}


@mcp.tool()
def list_soap_operations(service: str, refresh: bool = False) -> dict[str, Any]:
    """List a service's operations with their endpoint, SOAPAction and argument template.

    Args:
        service: Service name from list_soap_services.
        refresh: Re-read the WSDL instead of using the cached copy.
    """
    doc = _load_wsdl(service, refresh)
    return {"service": service, "operations": [doc.describe(name) for name in doc.operations]}


@mcp.tool()
def call_soap_operation(service: str, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a SOAP operation and return the parsed response body.

    Args:
        service: Service name from list_soap_services.
        operation: Operation name from list_soap_operations.
        arguments: Request fields shaped like the operation's arguments_template.
    """
    doc = _load_wsdl(service)
    op = doc.get(operation)
    endpoint = _service(service).get("endpoint") or op["endpoint"]
    if not endpoint:
        raise ValueError(f"No endpoint for {service!r}: the WSDL is abstract, add \"endpoint\" to services.json.")
    envelope = soap_xml.build_envelope(doc.build_body(operation, arguments), op["soap_version"])
    status, body = soap_xml.post(endpoint, envelope, op["soap_action"], op["soap_version"])
    return {"http_status": status, "response": soap_xml.parse_envelope(body)}


@mcp.tool()
def send_raw_soap_request(
    service: str,
    envelope_xml: str,
    soap_action: str = "",
    soap_version: str = "1.1",
) -> dict[str, Any]:
    """Send a hand-written SOAP envelope to a service's endpoint and return the parsed response body.

    Args:
        service: Service name from list_soap_services.
        envelope_xml: Complete SOAP Envelope XML.
        soap_action: SOAPAction value.
        soap_version: "1.1" or "1.2".
    """
    endpoint = _service(service).get("endpoint")
    if not endpoint:
        endpoint = next((op["endpoint"] for op in _load_wsdl(service).operations.values() if op["endpoint"]), None)
    if not endpoint:
        raise ValueError(f"No endpoint known for {service!r}; add \"endpoint\" to services.json.")
    soap_xml.parse_xml(envelope_xml.encode())  # reject malformed XML before sending
    status, body = soap_xml.post(endpoint, envelope_xml.encode(), soap_action, soap_version)
    return {"http_status": status, "response": soap_xml.parse_envelope(body)}


if __name__ == "__main__":
    mcp.run()
