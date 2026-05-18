from app.services.parser import parse_file

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
