# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Planted control for issue 319: one test outlasts the series.yml check job limit; never merge."""

import os
import time
import unittest


class PlantedSleepCase(unittest.TestCase):
    """Sleeps past timeout-minutes 10 on GitHub Actions so the check job must be cancelled."""

    @unittest.skipUnless(os.environ.get("GITHUB_ACTIONS") == "true", "planted CI timeout control")
    def test_sleep_past_job_limit(self) -> None:
        """Sleep 660 s, longer than the 600 s job limit, so only the job timeout can end it."""
        time.sleep(660)
