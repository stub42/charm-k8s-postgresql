# This file is part of the PostgreSQL k8s Charm for Juju.
# Copyright 2020 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3, as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranties of
# MERCHANTABILITY, SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR
# PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import unittest

from ops.testing import Harness
from charm import PostgreSQLCharm


CONFIG_NO_IMAGE = {
    "image": "",
    "image_username": "",
    "image_password": "",
}


class TestCharm(unittest.TestCase):
    def setUp(self):
        """Setup the test harness."""
        self.harness = Harness(PostgreSQLCharm)
        self.harness.begin()
        self.harness.disable_hooks()
        self.maxDiff = None

    def test_check_for_empty_config_no_image(self):
        """Check for correctly reported empty required image."""
        self.harness.update_config(CONFIG_NO_IMAGE)
        expected = "required setting(s) empty: image"
        self.assertEqual(self.harness.charm._check_for_config_problems(), expected)
