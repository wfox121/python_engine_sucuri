from sucuri.parser import parse_sucuri
from sucuri.compiler import SucuriCompiler
from collections import OrderedDict
import os
import re

# Default number of parsed templates kept in memory.
_DEFAULT_CACHE_SIZE = 128
_DEFAULT_COMPILED_CACHE_SIZE = 64


class _TemplateCache:
    """Bounded, mtime-aware cache of parsed template ASTs.

    A parsed tree depends only on file content, so entries are shared across
    Environments. They are keyed by real path and revalidated against the file's
    mtime, which means a template edited on disk is re-parsed without any external
    invalidation hook. Capacity is bounded so a long-running process cannot grow
    without limit when template paths are generated dynamically.
    """

    def __init__(self, maxsize=_DEFAULT_CACHE_SIZE):
        self.maxsize = maxsize
        self._entries = OrderedDict()

    @staticmethod
    def _key(filepath):
        # Use absolute path instead of realpath to avoid filesystem calls
        return os.path.abspath(filepath)

    def get(self, filepath, mtime):
        key = self._key(filepath)
        entry = self._entries.get(key)
        if entry is None:
            return None
        cached_mtime, tree = entry
        if cached_mtime != mtime:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return tree

    def set(self, filepath, mtime, tree):
        key = self._key(filepath)
        self._entries[key] = (mtime, tree)
        self._entries.move_to_end(key)
        while len(self._entries) > self.maxsize:
            self._entries.popitem(last=False)

    def clear(self):
        self._entries.clear()

    def __contains__(self, filepath):
        return self._key(filepath) in self._entries

    def __getitem__(self, filepath):
        return self._entries[self._key(filepath)][1]

    def __delitem__(self, filepath):
        del self._entries[self._key(filepath)]

    def __len__(self):
        return len(self._entries)


_AST_CACHE = _TemplateCache()


class _CompiledCache:
    """Bounded cache of compiled template output.

    Cache keyed by (filepath, context_hash, filters_hash, watch_enabled).
    This allows us to skip compilation when the same template is rendered
    with the same context structure.
    """

    def __init__(self, maxsize=_DEFAULT_COMPILED_CACHE_SIZE):
        self.maxsize = maxsize
        self._entries = OrderedDict()

    @staticmethod
    def _key(filepath, context, filters, watch_enabled):
        # Compute hash of context keys (not values) for cache key
        # This allows reusing compiled output for same structure
        context_key = tuple(sorted(context.keys())) if context else ()
        filters_key = tuple(sorted(filters.keys())) if filters else ()
        return (os.path.abspath(filepath), context_key, filters_key, watch_enabled)

    def get(self, filepath, context, filters, watch_enabled):
        key = self._key(filepath, context, filters, watch_enabled)
        entry = self._entries.get(key)
        if entry is None:
            return None
        result = entry
        self._entries.move_to_end(key)
        return result

    def set(self, filepath, context, filters, watch_enabled, result):
        key = self._key(filepath, context, filters, watch_enabled)
        self._entries[key] = result
        self._entries.move_to_end(key)
        while len(self._entries) > self.maxsize:
            self._entries.popitem(last=False)

    def clear(self):
        self._entries.clear()

    def __len__(self):
        return len(self._entries)


_COMPILER_CACHE = _CompiledCache()


class Environment:
    """
    Represents a templating environment that can hold custom plugins, filters,
    and configurations.
    """
    def __init__(self, base_dir=".", watch_enabled=False):
        self.base_dir = base_dir
        self.filters = {}
        self.watch_enabled = watch_enabled

    def register_filter(self, name, filter_func=None):
        if filter_func is None:
            def decorator(f):
                self.filters[name] = f
                return f
            return decorator
        self.filters[name] = filter_func
        return filter_func

    def template(self, filepath, context=None):
        if context is None:
            context = {}

        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Template file '{filepath}' not found.")

        # Fast path: try to get from compiled cache first
        cached = _COMPILER_CACHE.get(filepath, context, self.filters, self.watch_enabled)
        if cached is not None:
            return cached

        mtime = os.path.getmtime(filepath)
        tree = _AST_CACHE.get(filepath, mtime)
        if tree is None:
            with open(filepath, 'r', encoding='utf-8') as f:
                sucuri_text = f.read()
            tree = parse_sucuri(sucuri_text)
            _AST_CACHE.set(filepath, mtime, tree)

        base_dir = os.path.dirname(filepath) if filepath else self.base_dir
        compiler = SucuriCompiler(context, base_dir=base_dir, filters=self.filters, watch_enabled=self.watch_enabled)
        result = compiler.compile(tree)
        
        # Cache the compiled output
        _COMPILER_CACHE.set(filepath, context, self.filters, self.watch_enabled, result)
        
        return result

    def clear_cache(self):
        """Clear both AST and compiled caches."""
        _AST_CACHE.clear()
        _COMPILER_CACHE.clear()

    def clear_compiled_cache(self):
        """Clear only the compiled output cache."""
        _COMPILER_CACHE.clear()

    def clear_ast_cache(self):
        """Clear only the AST cache."""
        _AST_CACHE.clear()

    @property
    def cache_info(self):
        """Return cache statistics."""
        return {
            'ast_cache_size': len(_AST_CACHE),
            'compiled_cache_size': len(_COMPILER_CACHE),
            'ast_cache_maxsize': _AST_CACHE.maxsize,
            'compiled_cache_maxsize': _COMPILER_CACHE.maxsize,
        }


def template(filepath, context=None, base_dir="."):
    """
    Main function of the Sucuri engine, compatible with backward definitions.
    Uses a global default environment for convenience.
    """
    from sucuri.rendering import default_env
    return default_env.template(filepath, context)
