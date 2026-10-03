from __future__ import annotations

import re
import unittest

from python_packages.platform_infra import security


_PRODUCTION_ARGON2ID_HASH = re.compile(
    r"\A\$argon2id\$v=19\$m=65536,t=3,p=4\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+\Z"
)


class PlatformPasswordHashContractTests(unittest.TestCase):
    def test_production_hash_has_exact_argon2id_parameters_and_verifies(self) -> None:
        password = "production-password-contract"
        generated_hash = security.hash_password(password)

        self.assertRegex(generated_hash, _PRODUCTION_ARGON2ID_HASH)
        self.assertTrue(security.verify_password(password, generated_hash))
        self.assertFalse(
            security.verify_password("wrong-production-password", generated_hash)
        )


if __name__ == "__main__":
    unittest.main()
