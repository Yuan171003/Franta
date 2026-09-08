"""Compatibility facade for the Block 5 read-access search engine."""

from .read_access.search import SearchEngine, search_memory, tokenize

__all__ = ["SearchEngine", "search_memory", "tokenize"]
