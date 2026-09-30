"""Minimal SOAP helpers built on the standard library.

Reads a WSDL (concrete or abstract), builds SOAP envelopes from dicts and
turns XML responses back into dicts. Nothing more.

Network access is limited to hosts in SOAP_ALLOWED_HOSTS
(comma separated, default "127.0.0.1,localhost"; "*" allows any host).
"""

import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

WSDL = "{http://schemas.xmlsoap.org/wsdl/}"
XSD = "{http://www.w3.org/2001/XMLSchema}"
SOAP_BINDING_NS = {
    "{http://schemas.xmlsoap.org/wsdl/soap/}": "1.1",
    "{http://schemas.xmlsoap.org/wsdl/soap12/}": "1.2",
}
ENVELOPE_NS = {
    "1.1": "http://schemas.xmlsoap.org/soap/envelope/",
    "1.2": "http://www.w3.org/2003/05/soap-envelope",
}
ET.register_namespace("soapenv", ENVELOPE_NS["1.1"])
ET.register_namespace("soap12", ENVELOPE_NS["1.2"])


class SoapError(Exception):
    pass


def local(tag_or_qname: str) -> str:
    """'{ns}name' or 'prefix:name' -> 'name'."""
    return tag_or_qname.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


# --------------------------------------------------------------------------- I/O

def check_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    allowed = {h.strip().lower() for h in os.environ.get("SOAP_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")}
    if parsed.scheme not in ("http", "https"):
        raise SoapError(f"Only http/https URLs are supported: {url}")
    if "*" not in allowed and (parsed.hostname or "").lower() not in allowed:
        raise SoapError(f"Host {parsed.hostname!r} is not in SOAP_ALLOWED_HOSTS {sorted(allowed)}")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirect)


def parse_xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data.upper():
        raise SoapError("XML with a DOCTYPE is rejected.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise SoapError(f"Invalid XML: {exc}") from exc


def _is_http(location: str) -> bool:
    return urllib.parse.urlparse(location).scheme in ("http", "https")


def read(location: str) -> bytes:
    """Read a WSDL/XSD from an http(s) URL or a local file path."""
    if _is_http(location):
        check_url(location)
        try:
            with _opener.open(location, timeout=30) as resp:
                return resp.read()
        except urllib.error.URLError as exc:
            raise SoapError(f"Cannot fetch {location}: {exc}") from exc
    try:
        return Path(location).read_bytes()
    except OSError as exc:
        raise SoapError(f"Cannot read {location}: {exc}") from exc


def post(endpoint: str, envelope: bytes, soap_action: str, soap_version: str) -> tuple[int, bytes]:
    check_url(endpoint)
    if soap_version == "1.2":
        headers = {"Content-Type": f'application/soap+xml; charset=utf-8; action="{soap_action}"'}
    else:
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{soap_action}"'}
    request = urllib.request.Request(endpoint, data=envelope, headers=headers, method="POST")
    try:
        with _opener.open(request, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # SOAP faults usually come back as HTTP 500
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise SoapError(f"Cannot reach {endpoint}: {exc.reason}") from exc


# --------------------------------------------------------------------------- XML <-> dict

def dict_to_xml(parent: ET.Element, data: dict, ns: str) -> None:
    for key, value in data.items():
        for item in value if isinstance(value, list) else [value]:
            child = ET.SubElement(parent, f"{{{ns}}}{key}" if ns else key)
            if isinstance(item, dict):
                dict_to_xml(child, item, ns)
            elif isinstance(item, bool):
                child.text = "true" if item else "false"
            elif item is not None:
                child.text = str(item)


def xml_to_dict(el: ET.Element):
    children = list(el)
    if not children:
        return (el.text or "").strip()
    result: dict = {}
    for child in children:
        key, value = local(child.tag), xml_to_dict(child)
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    return result


def build_envelope(body_element: ET.Element, soap_version: str) -> bytes:
    ns = ENVELOPE_NS[soap_version]
    envelope = ET.Element(f"{{{ns}}}Envelope")
    ET.SubElement(envelope, f"{{{ns}}}Header")
    ET.SubElement(envelope, f"{{{ns}}}Body").append(body_element)
    return ET.tostring(envelope, xml_declaration=True, encoding="utf-8")


def parse_envelope(data: bytes) -> dict:
    """Return the SOAP Body contents as a dict (a fault simply shows up as 'Fault')."""
    root = parse_xml(data)
    body = next((c for c in root if local(c.tag) == "Body"), None)
    if body is None:
        raise SoapError("Response has no SOAP Body.")
    return xml_to_dict(body) or {}


# --------------------------------------------------------------------------- WSDL

class Wsdl:
    """Index of a WSDL and everything it imports. Names are matched by local name."""

    def __init__(self, location: str):
        self.docs: list[ET.Element] = []
        self._load(location, set())

        self.elements = {}  # element name -> (node, target namespace, children qualified?)
        self.types = {}     # type name -> node
        for doc in self.docs:
            for schema in doc.iter(f"{XSD}schema"):
                tns = schema.get("targetNamespace", "")
                qualified = schema.get("elementFormDefault") == "qualified"
                for node in schema:
                    if node.tag == f"{XSD}element":
                        self.elements[node.get("name")] = (node, tns, qualified)
                    elif node.tag in (f"{XSD}complexType", f"{XSD}simpleType"):
                        self.types[node.get("name")] = node

        self.operations = self._index_operations()

    def _load(self, location: str, seen: set) -> None:
        if location in seen:
            return
        seen.add(location)
        root = parse_xml(read(location))
        self.docs.append(root)
        imports = [(node, "location") for node in root.iter(f"{WSDL}import")]
        imports += [(node, "schemaLocation") for tag in ("import", "include") for node in root.iter(f"{XSD}{tag}")]
        for node, attr in imports:
            target = node.get(attr)
            if not target:
                continue
            if _is_http(location):
                target = urllib.parse.urljoin(location, target)
            elif not _is_http(target) and not os.path.isabs(target):
                target = str(Path(location).parent / target)
            self._load(target, seen)

    def _index_operations(self) -> dict:
        messages, port_types, bindings, endpoints = {}, {}, {}, {}
        for doc in self.docs:
            tns = doc.get("targetNamespace", "")
            for msg in doc.iter(f"{WSDL}message"):
                messages[msg.get("name")] = [
                    {"name": p.get("name"), "element": p.get("element"), "type": p.get("type")}
                    for p in msg.iter(f"{WSDL}part")
                ]
            for pt in doc.iter(f"{WSDL}portType"):
                for op in pt.iter(f"{WSDL}operation"):
                    inp, out = op.find(f"{WSDL}input"), op.find(f"{WSDL}output")
                    port_types.setdefault(pt.get("name"), {})[op.get("name")] = {
                        "input": local(inp.get("message")) if inp is not None else None,
                        "output": local(out.get("message")) if out is not None else None,
                        "namespace": tns,
                    }
            for binding in doc.iter(f"{WSDL}binding"):
                prefix = next((p for p in SOAP_BINDING_NS if binding.find(f"{p}binding") is not None), None)
                if prefix is None:
                    continue  # HTTP/MIME binding, not SOAP
                soap = binding.find(f"{prefix}binding")
                ops = {}
                for op in binding.iter(f"{WSDL}operation"):
                    soap_op = op.find(f"{prefix}operation")
                    body = op.find(f"{WSDL}input/{prefix}body")
                    ops[op.get("name")] = {
                        "soap_action": soap_op.get("soapAction", "") if soap_op is not None else "",
                        "style": (soap_op.get("style") if soap_op is not None else None) or soap.get("style", "document"),
                        "namespace": body.get("namespace") if body is not None else None,
                    }
                bindings[binding.get("name")] = {
                    "port_type": local(binding.get("type")), "version": SOAP_BINDING_NS[prefix], "ops": ops,
                }
            for port in doc.iter(f"{WSDL}port"):
                address = next((c for c in port if local(c.tag) == "address"), None)
                if address is not None:
                    endpoints.setdefault(local(port.get("binding")), address.get("location"))

        # Concrete WSDL: operations come from SOAP bindings. Abstract WSDL: straight from portTypes.
        sources = [(b["port_type"], b, endpoints.get(name)) for name, b in bindings.items()]
        bound = {b["port_type"] for b in bindings.values()}
        sources += [(pt, None, None) for pt in port_types if pt not in bound]

        operations = {}
        for pt_name, binding, endpoint in sources:
            for op_name, op in port_types.get(pt_name, {}).items():
                existing = operations.get(op_name)
                if existing and (existing["endpoint"] or not endpoint):
                    continue  # keep the first port that has an endpoint
                soap = binding["ops"].get(op_name, {}) if binding else {}
                operations[op_name] = {
                    "operation": op_name,
                    "endpoint": endpoint,
                    "soap_action": soap.get("soap_action", ""),
                    "soap_version": binding["version"] if binding else "1.1",
                    "style": soap.get("style", "document"),
                    "rpc_namespace": soap.get("namespace") or op["namespace"],
                    "input_parts": messages.get(op["input"], []),
                    "output_parts": messages.get(op["output"], []),
                }
        return operations

    # ---- request/response shape

    def _shape(self, node: ET.Element, depth: int = 0):
        """Example value for an xsd:element / type node: a type name or a nested dict."""
        if node.get("ref"):
            ref = self.elements.get(local(node.get("ref")))
            return self._shape(ref[0], depth + 1) if ref and depth < 10 else "any"
        type_name = node.get("type")
        if type_name and local(type_name) in self.types and depth < 10:
            node = self.types[local(type_name)]
        elif type_name:
            return local(type_name)
        fields = {}
        for child in self._child_elements(node, depth):
            name = child.get("name") or local(child.get("ref", ""))
            value = self._shape(child, depth + 1)
            fields[name] = [value] if child.get("maxOccurs", "1") not in ("0", "1") else value
        if fields:
            return fields
        restriction = node.find(f".//{XSD}restriction")
        return local(restriction.get("base")) if restriction is not None else "string"

    def _child_elements(self, node: ET.Element, depth: int):
        containers = {f"{XSD}{t}" for t in ("complexType", "sequence", "all", "choice", "complexContent")}
        for child in node:
            if child.tag == f"{XSD}element":
                yield child
            elif child.tag == f"{XSD}extension":
                base = local(child.get("base", ""))
                if base in self.types and depth < 10:
                    yield from self._child_elements(self.types[base], depth + 1)
                yield from self._child_elements(child, depth)
            elif child.tag in containers:
                yield from self._child_elements(child, depth)

    def _part_shape(self, part: dict):
        if part["element"]:
            entry = self.elements.get(local(part["element"]))
            return self._shape(entry[0]) if entry else "any"
        return self._shape(ET.Element(f"{XSD}element", type=part["type"] or "string"))

    def get(self, op_name: str) -> dict:
        if op_name not in self.operations:
            raise SoapError(f"Unknown operation {op_name!r}. Available: {sorted(self.operations)}")
        return self.operations[op_name]

    def describe(self, op_name: str) -> dict:
        op = self.get(op_name)
        parts = op["input_parts"]
        if op["style"] == "document" and len(parts) == 1:
            arguments = self._part_shape(parts[0])  # fields of the single wrapper element
        else:
            arguments = {p["name"]: self._part_shape(p) for p in parts}
        return {
            "operation": op_name,
            "endpoint": op["endpoint"],
            "soap_action": op["soap_action"],
            "soap_version": op["soap_version"],
            "arguments_template": arguments,
            "response_template": {local(p["element"] or p["name"]): self._part_shape(p) for p in op["output_parts"]},
        }

    # ---- request building

    def build_body(self, op_name: str, arguments: dict) -> ET.Element:
        op = self.get(op_name)
        if op["style"] == "rpc":
            wrapper = ET.Element(f"{{{op['rpc_namespace']}}}{op_name}")
            dict_to_xml(wrapper, arguments, "")
            return wrapper
        parts = op["input_parts"]
        if len(parts) != 1 or not parts[0]["element"]:
            raise SoapError(f"{op_name}: only document/literal operations with one body part are supported.")
        name = local(parts[0]["element"])
        if name not in self.elements:
            raise SoapError(f"Element {name!r} is not defined in the WSDL schemas.")
        _, tns, qualified = self.elements[name]
        if set(arguments) == {name}:  # caller already wrapped the fields in the root element
            arguments = arguments[name]
        root = ET.Element(f"{{{tns}}}{name}" if tns else name)
        dict_to_xml(root, arguments, tns if qualified else "")
        return root
# Parse WSDL whether abrastract or concrete