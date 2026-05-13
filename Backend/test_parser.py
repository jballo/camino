from parser import parse_file

with open("main.py", "rb") as f:
    source = f.read()

chunks = parse_file("main.py", source)

for c in chunks:
    print(f"[{c.symbol_type:8}] {c.symbol_name}")
    print(f"           lines {c.start_line}-{c.end_line} | parent: {c.parent_class}")
    print(f"           sig: {c.signature[:80]}")
    print(f"           doc: {c.docstring[:60] if c.docstring else 'None'}")
    print()

print(f"Total chunks: {len(chunks)}")
