#!/usr/bin/env python3

# Copyright 2020 Canonical Ltd.
# Licensed under the GPLv3, see LICENCE file for details.

from ops.main import main

from pgcharm import PostgreSQLCharm as Charm


if __name__ == "__main__":
    main(Charm, use_juju_for_storage=True)
