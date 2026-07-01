#!/usr/bin/env python3
"""Search Google Scholar, Libris (libris.kb.se), or Crossref for publications,
let the user pick a result, and append its BibTeX entry to a .bib file with a
normalized cite key (firstAuthor + year)."""

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

CROSSREF_API_URL = "https://api.crossref.org/works"
CROSSREF_DOI_URL = "https://doi.org"
CROSSREF_REQUEST_TIMEOUT = 30
CROSSREF_USER_AGENT = "bibtex_retriever/1.0"


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


def _crossref_year(item):
    """Extract the publication year from a Crossref work item.

    Crossref may expose the year under 'published-print', 'published-online',
    or 'issued'; each carries a 'date-parts' list-of-lists whose first
    sublist holds [year, month?, day?].
    """
    for key in ("published-print", "published-online", "issued"):
        block = item.get(key) or {}
        parts = block.get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            return str(parts[0][0])
    return "?"


def _crossref_authors(item):
    """Build a BibTeX-style 'family, given and family, given ...' author list."""
    authors = item.get("author") or []
    names = []
    for a in authors:
        family = a.get("family", "")
        given = a.get("given", "")
        if family and given:
            names.append(f"{family}, {given}")
        elif family:
            names.append(family)
        elif given:
            names.append(given)
    return " and ".join(names) if names else "(no author)"


def fetch_results_crossref(query, limit=SCHOLAR_RESULTS_LIMIT):
    """Return up to `limit` publication-like dicts from the Crossref REST API.

    Each result is normalized to the ``{"bib": {...}}`` shape used by the rest
    of the pipeline. A ``"crossref_doi"`` field carries the DOI used later to
    fetch the BibTeX entry via content negotiation.
    """
    params = urllib.parse.urlencode({"query": query, "rows": limit})
    url = f"{CROSSREF_API_URL}?{params}"
    req = urllib.request.Request(
        url, headers={"User-Agent": CROSSREF_USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=CROSSREF_REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"Error querying Crossref: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        print(f"Could not parse Crossref response: {exc}", file=sys.stderr)
        sys.exit(1)

    items = data.get("message", {}).get("items", [])
    if not items:
        return []

    results = []
    for item in items:
        titles = item.get("title") or []
        title = titles[0] if titles else "(no title)"
        bib = {
            "title": title,
            "author": _crossref_authors(item),
            "pub_year": _crossref_year(item) or "?",
            "publisher": item.get("publisher", ""),
            "type": item.get("type", ""),
            "doi": item.get("DOI", ""),
        }
        results.append({"bib": bib, "crossref_doi": item.get("DOI", "")})
    return results


def _build_crossref_bibtex(pub):
    """Construct a minimal BibTeX entry from normalized Crossref metadata.

    Used as a fallback when content negotiation cannot produce an entry.
    Picks '@article' for journal-like types and '@book' otherwise, falling
    back to '@misc' for anything unrecognized.
    """
    bib = pub.get("bib", {})
    title = bib.get("title", "")
    author = bib.get("author", "")
    year = bib.get("pub_year", "")
    publisher = bib.get("publisher", "")
    doi = bib.get("doi", "")
    wtype = (bib.get("type") or "").lower()

    if "journal" in wtype or "article" in wtype:
        entry_type = "article"
    elif "book" in wtype:
        entry_type = "book"
    else:
        entry_type = "misc"

    lines = [f"@{entry_type}{{crossrefplaceholder,"]
    if author:
        lines.append(f"\tauthor = {{{author}}},")
    if title:
        lines.append(f"\ttitle = {{{title}}},")
    if year:
        lines.append(f"\tyear = {{{year}}},")
    if publisher:
        lines.append(f"\tpublisher = {{{publisher}}},")
    if doi:
        lines.append(f"\tdoi = {{{doi}}},")
    if lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines.append("}")
    return "\n".join(lines) + "\n"


def fetch_bibtex_crossref(pub):
    """Return the raw BibTeX string for a Crossref record, or None.

    Tries content negotiation against doi.org with an
    ``Accept: application/x-bibtex`` header; on failure falls back to
    building an entry from the search-result metadata so the user is
    never left empty.
    """
    doi = pub.get("crossref_doi", "")
    if doi:
        url = f"{CROSSREF_DOI_URL}/{urllib.parse.quote(doi, safe='')}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": CROSSREF_USER_AGENT,
                "Accept": "application/x-bibtex",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=CROSSREF_REQUEST_TIMEOUT) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            print(
                f"Crossref content negotiation unavailable for {doi}: {exc}",
                file=sys.stderr,
            )
    print("Falling back to building a BibTeX entry from Crossref metadata.",
          file=sys.stderr)
    return _build_crossref_bibtex(pub)


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
    "crossref": ("Crossref", fetch_results_crossref, fetch_bibtex_crossref),
}

# Display order for the interactive provider menu. Crossref and Libris are
# listed before Google Scholar because they are more reliable (no CAPTCHAs).
PROVIDER_ORDER = ["crossref", "libris", "scholar"]


def prompt_provider():
    """Interactively ask the user to choose a provider; return its key."""
    print("\nSelect a search provider:\n")
    for i, key in enumerate(PROVIDER_ORDER, start=1):
        label = PROVIDERS[key][0]
        print(f"  [{i}] {label}")
    print(f"  [0] Abort")
    while True:
        try:
            choice = input("\nProvider (0 to abort): ").strip()
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
        if idx == 0:
            print("Aborted.")
            sys.exit(0)
        if 1 <= idx <= len(PROVIDER_ORDER):
            return PROVIDER_ORDER[idx - 1]
        print(f"Enter a number between 0 and {len(PROVIDER_ORDER)}.")


def prompt_query():
    """Interactively ask the user for a search query; return a non-empty str."""
    while True:
        try:
            query = input("\nEnter search query: ").strip()
        except EOFError:
            print("\nAborted.")
            sys.exit(0)
        if query:
            return query
        print("Query cannot be empty.")


def main():
    parser = argparse.ArgumentParser(
        description="Search Google Scholar, Libris (libris.kb.se), or Crossref "
        "and append a selected result's BibTeX entry to a .bib file."
    )
    parser.add_argument(
        "query", nargs="?", default=None,
        help="Search term for the selected provider. "
        "If omitted, the script prompts interactively.",
    )
    parser.add_argument(
        "--provider", choices=sorted(PROVIDERS), default=None,
        help="Search provider to use. 'crossref' queries the Crossref REST API; "
        "'libris' searches libris.kb.se (books); 'scholar' searches Google Scholar. "
        "If omitted, the script prompts interactively.",
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

    if args.provider is None:
        provider_key = prompt_provider()
    else:
        provider_key = args.provider

    if args.query is None:
        query = prompt_query()
    else:
        query = args.query

    provider_label, fetch_results, fetch_bibtex = PROVIDERS[provider_key]

    print(f"\nSearching {provider_label} for: {query!r}")
    results = fetch_results(query, limit=args.limit)

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