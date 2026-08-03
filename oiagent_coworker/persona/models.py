# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/personas/models.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; reduced upstream
#     broad re-export surface to a single :class:`Persona` dataclass.
#   - The upstream PersonaProvider.openai.Anthropic and
#     PersonaProvider.openai.OpenAI classes are dropped; this model only
#     captures the markdown frontmatter fields plus an optional module hint.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Persona data model for OIagent Coworker (W2-4).

This module defines the :class:`Persona` frozen dataclass that represents
the deserialized markdown frontmatter of a persona definition file.

Anti-flattery boundary (see plan \xc2\xa73.2):
    - No ``import openworker`` anywhere in this module.
    - No OpenAI / Anthropic provider classes.
    - Borrowed design (markdown frontmatter shape), not runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Persona"]


@dataclass(frozen=True)
class Persona:
    """A persona definition loaded from a markdown file.

    Attributes:
        name: Unique identifier for the persona (used in
            ``oiagent.personas.*`` namespace).
        description: Human-readable summary of the persona's role.
        version: Semantic version string for the persona definition.
        author: Optional author name or handle.
        tags: Optional list of free-form tags for categorization.
        module: Optional Python module name (e.g., ``"my_persona_impl"``)
            that provides the persona's behavior. If set, the service
            will lazily import this module on first access.
    """

    name: str
    description: str
    version: str
    author: str | None = None
    tags: list[str] = field(default_factory=list)
    module: str | None = None