from app.services.parser import MAX_LINE_BYTES, parse_file, source_skip_reason

PY_SOURCE = b'''\
class Greeter:
    """A friendly greeter."""

    def say_hello(self, name: str) -> str:
        """Return a greeting."""
        return f"Hello, {name}!"

def add(a: int, b: int) -> int:
    return a + b
'''

JS_SOURCE = b'''\
/** Adds two numbers */
const add = (a, b) => {
  return a + b;
};

function greet(name) {
  return "hi " + name;
}
'''


def test_python_chunks():
    chunks = parse_file("example.py", PY_SOURCE)
    names = {c.symbol_name for c in chunks}
    assert names == {"Greeter", "say_hello", "add"}

    cls = next(c for c in chunks if c.symbol_name == "Greeter")
    assert cls.symbol_type == "class"
    assert cls.docstring == "A friendly greeter."

    method = next(c for c in chunks if c.symbol_name == "say_hello")
    assert method.symbol_type == "method"
    assert method.parent_class == "Greeter"

    func = next(c for c in chunks if c.symbol_name == "add")
    assert func.symbol_type == "function"
    assert func.parent_class is None


def test_javascript_chunks():
    chunks = parse_file("example.js", JS_SOURCE)
    names = {c.symbol_name for c in chunks}
    assert names == {"add", "greet"}

    arrow = next(c for c in chunks if c.symbol_name == "add")
    assert arrow.symbol_type == "function"
    assert arrow.docstring and "Adds two numbers" in arrow.docstring


def test_unsupported_extension_returns_empty():
    assert parse_file("data.csv", b"a,b,c") == []


def test_skipped_directory_returns_empty():
    assert parse_file("node_modules/lib/index.js", JS_SOURCE) == []


def test_nul_bytes_returns_empty():
    assert parse_file("evil.py", b"def evil():\n    return '\x00'\n") == []


def test_invalid_utf8_returns_empty():
    assert parse_file("evil.py", b"def evil():\n    return '\xff'\n") == []


def test_source_skip_reason():
    assert source_skip_reason(b"\xff\x00") == "binary (NUL bytes)"
    assert source_skip_reason(b"\xff") == "invalid UTF-8"
    assert source_skip_reason(b"def valid():\n    return True\n") is None


def _identities(chunks):
    return [(c.symbol_name, c.start_line) for c in chunks]


def test_one_line_redeclaration_yields_one_chunk():
    # Mirrors prettier's tests/format/js/error-recovery/duplicate-bindings.js.
    chunks = parse_file("duplicate-bindings.js", b"class A{}   class A{}\n")
    assert _identities(chunks) == [("A", 1)]


def test_one_line_repeated_names_have_unique_identities():
    source = b"function n(){}function n(){}class A{m(){}m(){}}function t(){}\n"
    identities = _identities(parse_file("bundle.js", source))
    assert len(identities) == len(set(identities))
    assert set(identities) == {("n", 1), ("A", 1), ("m", 1), ("t", 1)}


def test_nested_same_name_on_one_line_keeps_outer_chunk():
    source = b"function f(){ function f(){} }\n"
    chunks = parse_file("nested.js", source)
    assert _identities(chunks) == [("f", 1)]
    assert chunks[0].source_code == "function f(){ function f(){} }"


def test_one_line_arrow_redeclaration_yields_one_chunk():
    chunks = parse_file("arrows.js", b"const a = () => 1; const a = () => 2;\n")
    assert _identities(chunks) == [("a", 1)]


def test_same_name_on_different_lines_is_kept():
    chunks = parse_file("dupes.py", b"class A: pass\nclass A: pass\n")
    assert _identities(chunks) == [("A", 1), ("A", 2)]


def test_source_skip_reason_minified_name():
    source = b"function a(){}\n"
    for path in ("static/jquery-3.7.1.min.js", "rapidoc-min.js", "lib/x.MIN.JS", "a.min.ts"):
        assert source_skip_reason(source, path) == "minified (file name)", path
    for path in ("src/admin.js", "src/minify.js", "src/min.js", "tools/x-min.py"):
        assert source_skip_reason(source, path) is None, path
    assert source_skip_reason(source) is None


def test_source_skip_reason_long_line():
    at_limit = b"x" * MAX_LINE_BYTES + b"\nshort\n"
    over_limit = b"short\n" + b"x" * (MAX_LINE_BYTES + 1)
    assert source_skip_reason(at_limit, "data.js") is None
    assert source_skip_reason(over_limit, "data.js") == (
        f"minified (line over {MAX_LINE_BYTES} bytes)"
    )


def test_minified_files_return_empty():
    assert parse_file("static/jquery.min.js", JS_SOURCE) == []
    long_line = b"function a(){return " + b"1+" * MAX_LINE_BYTES + b"1}\n"
    assert parse_file("static/blob.js", long_line) == []
