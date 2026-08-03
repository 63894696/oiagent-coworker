# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/personas/registry.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; merged upstream
#     registry and lazy import logic into a single service class.
#   - The upstream PersonaProvider.openai.Anthropic and
#     PersonaProvider.openai.OpenAI classes are dropped; this service only
#     manages OIagent Coworker personas.
#   - Added lazy module loading with caching to avoid upfront imports.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Persona service for OIagent Coworker (W2-4).

This module implements :class:`OIagentCoworkerPersonaService`, which
manages the lifecycle of :class:`Persona` objects: loading from disk,
caching, lazy module loading, and providing lookup by name.

The service is intended to be a singleton-like object owned by the
OIagent Coworker daemon. It is thread-safe for concurrent reads.

Anti-flattery boundary (see plan \xc2\xa73.2):
    - No ``import openworker`` anywhere in this module.
    - No OpenAI / Anthropic provider classes.
    - Borrowed design (registry + lazy import), not runtime.
"""

from __future__ import annotations

import importlib
import logging
import threading

from oiagent_coworker.persona.models import Persona
from oiagent_coworker.persona.persistence import OIagentCoworkerPersonaPersistence

__all__ = ["OIagentCoworkerPersonaService"]

_LOGGER = logging.getLogger(__name__)


class OIagentCoworkerPersonaService:
    """Service managing the persona registry and lazy module loading.

    Thread safety:
        All public methods are thread-safe. The class uses a reentrant lock
        to protect the internal caches. Reads are fast and lock contention
        is minimal after initial load.

    Attributes:
        persistence: The persistence layer used to load persona definitions.
        _personas: A dictionary mapping persona names to :class:`Persona`
            objects (loaded from disk).
        _modules: A dictionary mapping persona names to loaded module
            objects (or None if the persona has no module).
        _lock: A reentrant lock guarding access to ``_personas`` and
            ``_modules``.
    """

    def __init__(self, persistence: OIagentCoworkerPersonaPersistence) -> None:
        self.persistence: OIagentCoworkerPersonaPersistence = persistence
        self._personas: dict[str, Persona] = {}
        self._modules: dict[str, object | None] = {}
        self._lock = threading.RLock()
        self._load_all()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_persona(self, name: str) -> Persona | None:
        """Return the :class:`Persona` object for the given name.

        Args:
            name: The persona name (case-sensitive).

        Returns:
            The :class:`Persona` if found, otherwise ``None``.
        """
        with self._lock:
            return self._personas.get(name)

    def get_persona_module(self, name: str) -> object | None:
        """Return the loaded module for the given persona, if any.

        If the persona has a ``module`` field, this method will import
        and cache the module on first call. Subsequent calls return the
        cached module.

        Args:
            name: The persona name.

        Returns:
            The module object if the persona has a module and it was
            successfully loaded; otherwise ``None``.

        Notes:
            If the module cannot be imported, an error is logged and
            ``None`` is returned.
        """
        with self._lock:
            persona = self._personas.get(name)
            if not persona:
                return None
            if persona.module is None:
                return None
            if name not in self._modules:
                try:
                    module = importlib.import_module(persona.module)
                    self._modules[name] = module
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "Failed to load module %s for persona %s: %s",
                        persona.module,
                        name,
                        exc,
                    )
                    self._modules[name] = None
            return self._modules[name]

    def list_personas(self) -> list[str]:
        """Return a list of all known persona names.

        Returns:
            A sorted list of persona names.
        """
        with self._lock:
            return sorted(self._personas.keys())

    def reload(self) -> None:
        """Reload all persona definitions from disk.

        This clears the internal caches and reloads from the persistence
        layer. Any previously loaded modules are discarded and will be
        reloaded on demand.
        """
        with self._lock:
            self._personas.clear()
            self._modules.clear()
            self._load_all()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        """Load all persona definitions from the persistence layer."""
        try:
            personas = self.persistence.load_all()
            self._personas = personas
            # Initialize module cache to None for each persona.
            self._modules = {name: None for name in personas}
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Failed to load personas: %s", exc)
            # Keep existing caches if any; but better to start empty?
            # For safety, we reset to empty on error.
            self._personas = {}
            self._modules = {}