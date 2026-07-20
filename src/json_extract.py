def extract_balanced_json(text: str, open_index: int) -> str:
    """Given text and the index of an opening '{', return the substring
    from open_index through the matching closing '}', treating braces
    inside double-quoted string literals as inert."""
    depth = 0
    in_string = False
    escape = False
    for i in range(open_index, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[open_index : i + 1]
    raise ValueError("No matching closing brace found")
