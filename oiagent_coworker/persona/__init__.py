# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/personas/__init__.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; replaced upstream
#     broad re-export surface with a curated public API for the persona
#     subsystem.
#   - The upstream PersonaProvider.openai.Anthropic and
#     PersonaProvider.openai.OpenAI classes are dropped; this __init__ only
#     exposes the OIagent Coworker persona building blocks: models,
#     persistence, and service.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Persona package for OIagent Coworker (W2-4).

Public API:

    * :class:`Persona` -- the persona dataclass.
    * :class:`OIagentCoworkerPersonaPersistence` -- persistence layer for
      loading persona definitions from markdown files.
    * :class:`OIagentCoworkerPersonaService` -- business core; owns the
      persona registry, lazy loading, and switching mechanism.

Anti-flattery boundary (see plan \xc2\xa73.2):
    - No ``import openworker`` anywhere in this package.
    - No OpenAI / Anthropic provider classes.
    - Borrowed design (markdown frontmatter + lazy import), not runtime.
"""

from oiagent_coworker.persona.models import Persona
from oiagent_coworker.persona.persistence import OIagentCoworkerPersonaPersistence
from oiagent_coworker.persona.service import OIagentCoworkerPersonaService

__all__ = [
    "OIagentCoworkerPersonaPersistence",
    "OIagentCoworkerPersonaService",
    "Persona",
]