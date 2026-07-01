A script for pulling Bibtex data from different providers. Current list of supported providers:
- Google Scholar
- Libris

Google Scholar will tend to have a rate limit. Libris is more reliable but works only for books in the Libris database (libris.kb.se).

Use:

python bibtex_retriever.py "search query" --bib [bib file]

Select a search result, the selected result will be appended to the .bib file in Bibtex compatible format.
