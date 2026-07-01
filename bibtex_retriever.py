#!/usr/bin/env python3
"""Search Google Scholar or Libris (libris.kb.se) for books, let the user
pick a result, and append its BibTeX entry to a .bib file with a normalized
cite key (firstAuthor + year)."""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

from scholarly import scholarly


SCHOLAR_RESULTS_LIMIT = 10

LIBRIS_XSEARCH_URL = "https://libris.kb.se/xsearch"
LIBRIS_CITE_URL = "https://libris.kb.se/api/sv/cite"
LIBRIS_REQUEST_TIMEOUT = 30


def fetch_results_scholar(query, limit=SCHOLAR_RESULTS_LIMIT):
    """Return up to `limit` publication dicts from Google Scholar."""
    results = []
    try:
        generator = scholarly.search_pubs(query)
        for _ in range(limit):
            try:
                results.append(next(generator))
            except StopIteration:
                break
    except Exception as exc:
        print(f"Error querying Google Scholar: {exc}", file=sys.stderr)
        print(
            "Scholar may be rate-limiting or showing a CAPTCHA. "
            "Try again later, reduce usage, or route through a proxy/Tor.",
            file=sys.stderr,
        )
        sys.exit(1)
    return results


def fetch_bibtex_scholar(pub):
    """Return the raw BibTeX string for a Scholar publication, or None."""
    try:
        return scholarly.bibtex(pub)
    except Exception as exc:
        print(f"Could not fetch BibTeX from Scholar: {exc}", file=sys.stderr)
        return None


LIBRIS_USER_AGENT = "bibtex_retriever/1.0 (+https://libris.kb.se)"


def _libris_request(url):
    """Perform a GET request and return the decoded body, exiting on error."""
    req = urllib.request.Request(url, headers={"User-Agent": LIBRIS_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=LIBRIS_REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"Error querying Libris: {exc}", file=sys.stderr)
        sys.exit(1)


def fetch_results_libris(query, limit=SCHOLAR_RESULTS_LIMIT):
    """Return up to `limit` publication-like dicts from Libris (libris.kb.se).

    Each result is normalized to the same ``{"bib": {...}}`` shape used by the
    Scholar path so the rest of the pipeline (display, key derivation, etc.)
    can treat both providers uniformly. A ``"libris_identifier"`` field carries
    the record URI used later to fetch the BibTeX entry.
    """
    params = urllib.parse.urlencode({"query": query, "n": limit, "format": "json"})
    body = _libris_request(f"{LIBRIS_XSEARCH_URL}?{params}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        print(f"Could not parse Libris response: {exc}", file=sys.stderr)
        sys.exit(1)

    items = data.get("xsearch", {}).get("list", [])
    if not items:
        return []

    results = []
    for item in items:
        identifier = item.get("identifier", "")
        # Remote returns http:// URIs, but the cite endpoint requires https://.
        if identifier.startswith("http://"):
            identifier = "https://" + identifier[len("http://"):]
        bib = {
            "title": item.get("title", "(no title)"),
            "author": _clean_libris_creator(item.get("creator", "")) or "(no author)",
            "pub_year": item.get("date") or "?",
            "publisher": item.get("publisher", ""),
            "isbn": item.get("isbn", ""),
            "type": item.get("type", ""),
        }
        results.append({"bib": bib, "libris_identifier": identifier})
    return results


def _clean_libris_creator(creator):
    """Normalize a Libris 'creator' string into a BibTeX-style author.

    Libris xsearch exposes a single creator as 'Surname, Forename[, Dates]'.
    Drop any trailing date component so the value formats cleanly both for
    display and as a BibTeX author field.
    """
    if not creator:
        return ""
    parts = [p.strip() for p in creator.split(",")]
    parts = [p for p in parts if not re.fullmatch(r"\d{4}(-\d{4})?", p)]
    return ", ".join(parts)


def _build_libris_bibtex(pub):
    """Construct a minimal ``@book`` entry from normalized Libris metadata.

    Used as a fallback when the unAPI cite endpoint cannot produce an entry
    (e.g. for legacy numeric bib identifiers that return HTTP 500).
    """
    bib = pub.get("bib", {})
    title = bib.get("title", "")
    author = bib.get("author", "")
    year = bib.get("pub_year", "")
    publisher = bib.get("publisher", "")
    isbn = bib.get("isbn", "")
    lines = ["@book{librisplaceholder,"]
    if author:
        lines.append(f"\tauthor = {{{author}}},")
    if title:
        lines.append(f"\ttitle = {{{title}}},")
    if year:
        lines.append(f"\tyear = {{{year}}},")
    if publisher:
        lines.append(f"\tpublisher = {{{publisher}}},")
    if isbn:
        lines.append(f"\tisbn = {{{isbn}}},")
    # Drop the trailing comma on the last field.
    if lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines.append("}")
    return "\n".join(lines) + "\n"


def _libris_resolve_identifier(identifier):
    """Resolve a (possibly legacy numeric) Libris bib URI to its modern IRI.

    The unAPI cite endpoint serves BibTeX for modern IRIs such as
    ``https://libris.kb.se/<token>`` but returns HTTP 500 for the legacy
    numeric form ``https://libris.kb.se/bib/<digits>`` (which is what the
    xsearch JSON exposes). Following the 302 redirect on the identifier URL
    yields the modern IRI, which the cite endpoint then accepts.
    """
    if not re.search(r"/bib/\d+(?:[/?#]|$)", identifier):
        return identifier
    req = urllib.request.Request(
        identifier, method="HEAD", headers={"User-Agent": LIBRIS_USER_AGENT}
    )
    # HEAD requests must not auto-follow redirects; inspect the Location.
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None
    no_redirect = urllib.request.build_opener(_NoRedirect)
    try:
        no_redirect.open(req, timeout=LIBRIS_REQUEST_TIMEOUT)
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location")
        if location:
            return location
    except Exception:
        pass
    return identifier


def fetch_bibtex_libris(pub):
    """Return the raw BibTeX string for a Libris record, or None.

    Tries the unAPI cite endpoint first (resolving legacy numeric bib
    identifiers to their modern IRIs); on failure falls back to building an
    entry from the search-result metadata so the user is never left empty.
    """
    identifier = pub.get("libris_identifier", "")
    resolved = _libris_resolve_identifier(identifier)
    if resolved:
        params = urllib.parse.urlencode({"id": resolved, "format": "bibtex"})
        req = urllib.request.Request(
            f"{LIBRIS_CITE_URL}?{params}", headers={"User-Agent": LIBRIS_USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=LIBRIS_REQUEST_TIMEOUT) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            print(
                f"Libris cite endpoint unavailable for {resolved}: {exc}",
                file=sys.stderr,
            )
    print("Falling back to building a BibTeX entry from Libris metadata.",
          file=sys.stderr)
    return _build_libris_bibtex(pub)


def display_menu(results, provider_label):
    """Print the numbered menu of search results."""
    print(f"\n{provider_label} results:\n")
    for i, pub in enumerate(results, start=1):
        bib = pub.get("bib", {})
        title = bib.get("title", "(no title)")
        author = bib.get("author", "(no author)")
        year = bib.get("pub_year", "?")
        print(f"  [{i}] {title}")
        print(f"      {author} ({year})")
    print(f"  [0] Abort")


def prompt_selection(count):
    """Prompt until the user enters a valid integer in [0, count]."""
    while True:
        try:
            choice = input("\nSelect an entry (0 to abort): ").strip()
        except EOFError:
            print("\nAborted.")
            sys.exit(0)
        if choice == "":
            continue
        try:
            idx = int(choice)
        except ValueError:
            print("Please enter a number.")
            continue
        if 0 <= idx <= count:
            return idx
        print(f"Enter a number between 0 and {count}.")


def first_author_surname(bibtex, pub):
    """Extract the surname of the first author from the BibTeX string or the
    publication metadata."""
    m = re.search(r"author\s*=\s*[\{\"]\s*(.+?)\s*[\}\"]", bibtex, re.IGNORECASE)
    if m:
        authors_field = m.group(1)
        first = re.split(r"\s+and\s+", authors_field)[0]
        first = re.split(r"\s*,\s*", first)[0]
        surname = first.strip().split()[-1]
        surname = re.sub(r"[^A-Za-z]", "", surname)
        if surname:
            return surname.lower()
    bib = pub.get("bib", {})
    author = bib.get("author", "")
    if author:
        first = author.split(" and ")[0] if " and " in author else author.split(",")[0]
        surname = first.strip().split()[-1]
        surname = re.sub(r"[^A-Za-z]", "", surname)
        if surname:
            return surname.lower()
    return None


def extract_year(bibtex, pub):
    """Extract the publication year from BibTeX or metadata."""
    m = re.search(r"year\s*=\s*[\{\"]?(\d{4})", bibtex, re.IGNORECASE)
    if m:
        return m.group(1)
    year = pub.get("bib", {}).get("pub_year")
    return year if year else None


def replace_key(bibtex, old_key, new_key):
    """Replace the cite key in a BibTeX string."""
    return re.sub(
        r"(@\w+\s*\{)" + re.escape(old_key) + r"\s*,",
        rf"\g<1>{new_key},",
        bibtex,
        count=1,
    )


def derive_key(bibtex, pub, override):
    """Build a cite key: firstAuthor + year, or the override if provided."""
    if override:
        return override
    surname = first_author_surname(bibtex, pub)
    year = extract_year(bibtex, pub)
    if surname and year:
        return f"{surname}{year}"
    return None


def existing_keys(bib_path):
    """Return the set of cite keys present in the target .bib file."""
    keys = set()
    if not os.path.exists(bib_path):
        return keys
    with open(bib_path, "r", encoding="utf-8") as fh:
        for m in re.finditer(r"@\w+\s*\{\s*([^,\s]+)\s*,", fh.read()):
            keys.add(m.group(1))
    return keys


def deduplicate_key(base_key, bib_path):
    """Ensure the key doesn't collide with existing keys; suffix b, c, ..."""
    keys = existing_keys(bib_path)
    if base_key not in keys:
        return base_key
    suffixes = list("bcdefghijklmnopqrstuvwxyz")
    for suffix in suffixes:
        candidate = f"{base_key}{suffix}"
        if candidate not in keys:
            return candidate
    # Fallback: numeric suffix
    i = 2
    while f"{base_key}{i}" in keys:
        i += 1
    return f"{base_key}{i}"


def append_bibtex(bib_path, bibtex):
    """Append a BibTeX entry to the file, ensuring spacing."""
    new_file = not os.path.exists(bib_path)
    with open(bib_path, "a", encoding="utf-8") as fh:
        if not new_file:
            fh.write("\n")
        if not bibtex.endswith("\n"):
            bibtex += "\n"
        fh.write(bibtex + "\n")


PROVIDERS = {
    "scholar": ("Google Scholar", fetch_results_scholar, fetch_bibtex_scholar),
    "libris": ("Libris", fetch_results_libris, fetch_bibtex_libris),
}


def main():
    parser = argparse.ArgumentParser(
        description="Search Google Scholar or Libris (libris.kb.se) and append "
        "a selected result's BibTeX entry to a .bib file."
    )
    parser.add_argument("query", help="Search term for the selected provider.")
    parser.add_argument(
        "--provider", choices=sorted(PROVIDERS), default="libris",
        help="Search provider to use (default: libris). "
        "'libris' searches libris.kb.se (books); 'scholar' searches Google Scholar.",
    )
    parser.add_argument(
        "--bib", default="refs.bib", help="Path to the .bib file (default: refs.bib)."
    )
    parser.add_argument(
        "--key", default=None, help="Override the generated cite key."
    )
    parser.add_argument(
        "--limit", type=int, default=SCHOLAR_RESULTS_LIMIT,
        help=f"Maximum number of results to show (default: {SCHOLAR_RESULTS_LIMIT}).",
    )
    args = parser.parse_args()

    provider_label, fetch_results, fetch_bibtex = PROVIDERS[args.provider]

    print(f"Searching {provider_label} for: {args.query!r}")
    results = fetch_results(args.query, limit=args.limit)

    if not results:
        print("No results found.")
        return

    display_menu(results, provider_label)
    choice = prompt_selection(len(results))
    if choice == 0:
        print("Aborted.")
        return

    selected = results[choice - 1]
    bibtex = fetch_bibtex(selected)
    if not bibtex:
        return

    # Find the original key in the fetched BibTeX
    m = re.match(r"@\w+\s*\{\s*([^,\s]+)\s*,", bibtex)
    original_key = m.group(1) if m else None

    new_key = derive_key(bibtex, selected, args.key)
    if new_key is None:
        # Fall back to the provider's original key
        new_key = original_key or "entry"
        print(
            f"Warning: could not derive firstAuthor+year key; "
            f"using the provider's key '{new_key}'",
            file=sys.stderr,
        )

    new_key = deduplicate_key(new_key, args.bib)

    if original_key and original_key != new_key:
        bibtex = replace_key(bibtex, original_key, new_key)

    append_bibtex(args.bib, bibtex)
    print(f"\nAppended entry with cite key '{new_key}' to {args.bib}")


if __name__ == "__main__":
    main()