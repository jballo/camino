import logging
import os
from dataclasses import dataclass

import tree_sitter_javascript as tsjavascript
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

logger = logging.getLogger(__name__)

LANGUAGES = {
    ".py": Language(tspython.language()),
    ".js": Language(tsjavascript.language()),
    ".ts": Language(tstypescript.language_typescript()),
    ".tsx": Language(tstypescript.language_tsx()),
}

TARGET_NODES = {
    ".py":  {"function_definition", "class_definition"},
    ".js":  {"function_declaration", "class_declaration", "method_definition", "lexical_declaration"},
    ".ts":  {"function_declaration", "class_declaration", "method_definition", "lexical_declaration"},
    ".tsx": {"function_declaration", "class_declaration", "method_definition", "lexical_declaration"},
}

# These names conventionally contain vendored or generated code. Repositories
# that keep first-party source under them intentionally trade coverage for a
# smaller, more relevant index.
SKIP_DIRS = {
    "node_modules",
    ".git",
    ".repos",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    "vendor",
    "third_party",
    "dist",
    "build",
    ".next",
}
MAX_FILE_BYTES = 500_000
# Minified or bundled code packs a whole library onto a few very long lines.
# It is costly to embed, irrelevant to search, and repeats short names on one
# line. Hand-written files with a line this long (embedded data) are skipped too.
MAX_LINE_BYTES = 5_000
_MINIFIED_NAME_EXTENSIONS = {".js", ".ts", ".tsx"}
_MINIFIED_NAME_SUFFIXES = (".min", "-min")


@dataclass
class CodeChunk:
    file_path: str
    symbol_name: str
    symbol_type: str            # "function" | "class" | "method"
    language: str
    start_line: int             # 1 indexed
    end_line: int               # 1 indexed
    source_code: str            # raw source of chunk
    signature: str              # def/class lines before the body
    docstring: str | None
    parent_class: str | None    # for methods


def _is_minified_name(file_path: str) -> bool:
    stem, ext = os.path.splitext(os.path.basename(file_path).lower())
    return ext in _MINIFIED_NAME_EXTENSIONS and stem.endswith(_MINIFIED_NAME_SUFFIXES)


def _longest_line_bytes(source_bytes: bytes) -> int:
    longest = 0
    start = 0
    while True:
        end = source_bytes.find(b"\n", start)
        if end == -1:
            return max(longest, len(source_bytes) - start)
        longest = max(longest, end - start)
        start = end + 1


def source_skip_reason(source_bytes: bytes, file_path: str | None = None) -> str | None:
    if b"\x00" in source_bytes:
        return "binary (NUL bytes)"
    try:
        source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return "invalid UTF-8"
    if file_path is not None and _is_minified_name(file_path):
        return "minified (file name)"
    if _longest_line_bytes(source_bytes) > MAX_LINE_BYTES:
        return f"minified (line over {MAX_LINE_BYTES} bytes)"
    return None


def get_symbol_type(node: Node) -> str:
    if node.type in ("class_definition", "class_declaration"):
        return "class"

    if node.type == "method_definition":
        return "method"

    if node.type == "function_definition":
        parent = node.parent
        if parent and parent.type == "decorated_definition":
            parent = parent.parent
        if parent and parent.type == "block":
            grandparent = parent.parent
            if grandparent and grandparent.type == "class_definition":
                return "method"

    return "function"


def get_parent_class(node: Node) -> str | None:
    current = node.parent
    while current:
        if current.type == "class_definition" or current.type == "class_declaration":
            name_node = current.child_by_field_name("name")
            if name_node:
                return name_node.text.decode("utf-8")
            return None
        current = current.parent
    return None


def extract_signature(node: Node, source_bytes: bytes) -> str:
    body_node = node.child_by_field_name("body")
    if body_node:
        sig = source_bytes[node.start_byte:body_node.start_byte].decode("utf-8").rstrip()
        return sig
    # No body aka a stub or declaration — use the first line
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8").split("\n")[0]


def extract_docstring(node: Node, source_bytes: bytes) -> str | None:
    body_node = node.child_by_field_name("body")
    if not body_node or body_node.child_count == 0:
        return None

    first_stmt = body_node.children[0]

    # Python: docstring is an expression_statement containing a string
    if first_stmt.type == "expression_statement" and first_stmt.child_count > 0:
        string_node = first_stmt.children[0]
        if string_node.type == "string":
            raw = string_node.text.decode("utf-8")
            # Strip the triple quotes
            for quote in ('"""', "'''", '"', "'"):
                if raw.startswith(quote) and raw.endswith(quote):
                    return raw[len(quote):-len(quote)].strip()
            return raw

    # JS/TS: check for a JSDoc comment before the function (previous sibling)
    prev = node.prev_sibling
    if prev and prev.type == "comment":
        text = prev.text.decode("utf-8")
        if text.startswith("/**"):
            return text

    return None


def extract_chunks(source_bytes: bytes, file_path: str) -> list[CodeChunk]:
    ext = os.path.splitext(file_path)[1]
    if ext not in LANGUAGES:
        return []
    # Skip files in excluded directories
    path_parts = file_path.replace("\\", "/").split("/")
    if any(part in SKIP_DIRS for part in path_parts):
        return []
    if len(source_bytes) > MAX_FILE_BYTES:
        return []
    if source_skip_reason(source_bytes, file_path) is not None:
        return []
    language = LANGUAGES[ext]
    parser = Parser(language)
    tree = parser.parse(source_bytes)
    target_types = TARGET_NODES[ext]

    def collect_nodes(root: Node) -> list[Node]:
        results = []
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type in target_types:
                results.append(node)
            stack.extend(reversed(node.children))
        return results

    nodes = collect_nodes(tree.root_node)
    chunks: list[CodeChunk] = []
    # code_chunks is unique on (file_path, symbol_name, start_line) per
    # generation. One-line code (minified bundles, fixtures that redeclare a
    # name, nested same-name functions) can produce several nodes with the same
    # identity. Nodes are visited in pre-order, so the first, outermost one wins.
    seen: set[tuple[str, int]] = set()
    dropped = 0

    def add(chunk: CodeChunk) -> None:
        nonlocal dropped
        identity = (chunk.symbol_name, chunk.start_line)
        if identity in seen:
            dropped += 1
            return
        seen.add(identity)
        chunks.append(chunk)

    for node in nodes:
        # Arrow functions: lexical_declaration → variable_declarator → arrow_function
        if node.type == "lexical_declaration":
            for declarator in node.children:
                if declarator.type != "variable_declarator":
                    continue
                value = declarator.child_by_field_name("value")
                if not value or value.type != "arrow_function":
                    continue
                name_node = declarator.child_by_field_name("name")
                if not name_node:
                    continue
                arrow_body = value.child_by_field_name("body")
                sig = (
                    source_bytes[node.start_byte:arrow_body.start_byte].decode("utf-8").rstrip()
                    if arrow_body
                    else source_bytes[node.start_byte:node.end_byte].decode("utf-8").split("\n")[0]
                )
                docstring = None
                prev = node.prev_sibling
                if prev and prev.type == "comment":
                    text = prev.text.decode("utf-8")
                    if text.startswith("/**"):
                        docstring = text
                add(CodeChunk(
                    file_path=file_path,
                    symbol_name=name_node.text.decode("utf-8"),
                    symbol_type="function",
                    language=ext.lstrip("."),
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    source_code=source_bytes[node.start_byte:node.end_byte].decode("utf-8"),
                    signature=sig,
                    docstring=docstring,
                    parent_class=get_parent_class(node),
                ))
            continue

        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        # Use the decorator's range if this node is wrapped in one
        source_node = node
        if node.parent and node.parent.type == "decorated_definition":
            source_node = node.parent
        chunk = CodeChunk(
            file_path=file_path,
            symbol_name=name_node.text.decode("utf-8"),
            symbol_type=get_symbol_type(node),
            language=ext.lstrip("."),
            start_line=source_node.start_point[0] + 1,
            end_line=source_node.end_point[0] + 1,
            source_code=source_bytes[source_node.start_byte:source_node.end_byte].decode("utf-8"),
            signature=extract_signature(node, source_bytes),
            docstring=extract_docstring(node, source_bytes),
            parent_class=get_parent_class(node),
        )
        add(chunk)
    if dropped:
        logger.info("parse dedupe | file=%s dropped=%d", file_path, dropped)
    return chunks


def parse_file(file_path: str, source: bytes) -> list[CodeChunk]:
    try:
        return extract_chunks(source, file_path)
    except Exception as e:
        logger.warning("parse failed | file=%s error=%s", file_path, e)
        return []
