# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class StoredVersion:
    image: str
    version: str
    timestamp: datetime


@dataclass(frozen=True)
class RegisterResult:
    image: str
    version: str
    timestamp: datetime
    created: bool


@dataclass(frozen=True)
class LatestVersionResult:
    image: str
    version: Optional[str]
