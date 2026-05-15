"""Local search functions for DOM, console, network files."""
import os
import re

SCRATCH_DIR = "scratch"

def search_file(filepath: str, query: str) -> dict:
    """Search a file for lines matching a query. Supports regex and pipe-separated OR."""
    if not os.path.exists(filepath):
        return {"error": f"File not found: {filepath}", "matches": []}

    matches = []
    try:
        try:
            pattern = re.compile(query, re.IGNORECASE)
            use_regex = True
        except re.error:
            use_regex = False

        with open(filepath, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                hit = pattern.search(line) if use_regex else (query.lower() in line.lower())
                if hit:
                    matches.append({"line": i, "content": line.rstrip()})
    except Exception as e:
        return {"error": str(e), "matches": []}

    return {
        "query": query,
        "file": os.path.basename(filepath),
        "total_matches": len(matches),
        "matches": matches[:100],
        "truncated": len(matches) > 100,
    }

def search_dom(query: str) -> dict:
    return search_file(os.path.join(SCRATCH_DIR, "obs_dom.txt"), query)

def search_console(query: str) -> dict:
    return search_file(os.path.join(SCRATCH_DIR, "obs_console.log"), query)

def search_network(query: str) -> dict:
    return search_file(os.path.join(SCRATCH_DIR, "obs_network.log"), query)

def read_network_body(filename: str = "") -> dict:
    bodies_dir = os.path.join(SCRATCH_DIR, "obs_net_bodies")
    if not os.path.exists(bodies_dir):
        return {"available_files": [], "hint": "No network bodies captured yet."}

    available = os.listdir(bodies_dir)

    if filename and len(filename) >= 3:
        # Require at least 3 chars to prevent overly broad matches
        # Also read the URL line inside the file for better matching
        for f in available:
            filepath = os.path.join(bodies_dir, f)
            if filename.lower() in f.lower():
                try:
                    with open(filepath, "r", encoding="utf-8") as fh:
                        return {"file": f, "body": fh.read()[:10000]}
                except Exception:
                    continue
        # Second pass: check inside files for URL match
        for f in available:
            filepath = os.path.join(bodies_dir, f)
            try:
                with open(filepath, "r", encoding="utf-8") as fh:
                    content = fh.read()
                    first_line = content.split("\n")[0] if content else ""
                    if filename.lower() in first_line.lower():
                        return {"file": f, "body": content[:10000]}
            except Exception:
                continue

    return {
        "available_files": available,
        "hint": "Provide a filename or partial match (min 3 chars).",
    }
