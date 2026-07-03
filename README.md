A script for pulling Bibtex data from different providers. Current list of supported providers:
- Crossref
- Google Scholar
- Libris

Google Scholar will tend to have a prohibitive rate limit. Libris is more reliable but works only for books in the Libris database (libris.kb.se). Crossref works for articles and book chapters, has a relatively generous rate limit.

Use:

python bibtex_retriever.py --bib [bib file]

The script will ask you to select a provider, then to enter a search query. Select the appropriate search result, it will be appended to the selected bib file in Bibtex compatible format.

For Google Scholar queries, scholarly is required (see requirements.txt)

Code by GLM-5
