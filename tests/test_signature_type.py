"""Verify signature_type=3 (POLY_1271) is used consistently after deposit wallet migration."""

import ast
from pathlib import Path

import pytest


POLY_1271 = 3
REPO_ROOT = Path(__file__).parent.parent


class TestSignatureTypeConsistency:
    def test_no_signature_type_2_in_codebase(self):
        """AST guard: no hardcoded signature_type=2 in any Polymarket code path."""
        targets = [
            REPO_ROOT / "auramaur" / "exchange" / "client.py",
            REPO_ROOT / "auramaur" / "bot.py",
            REPO_ROOT / "auramaur" / "broker" / "sync.py",
            REPO_ROOT / "observability" / "dashboard.py",
        ]
        for path in targets:
            assert path.exists(), f"Expected source file not found: {path}"
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == "signature_type":
                    if isinstance(node.value, ast.Constant) and node.value.value == 2:
                        pytest.fail(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                            " still uses signature_type=2"
                        )

    def test_client_py_uses_poly_1271(self):
        """Verify client.py uses signature_type=3 in all ClobClient/BalanceAllowanceParams calls."""
        path = REPO_ROOT / "auramaur" / "exchange" / "client.py"
        tree = ast.parse(path.read_text())
        sig_type_values = []
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "signature_type":
                if isinstance(node.value, ast.Constant):
                    sig_type_values.append((node.lineno, node.value.value))

        assert len(sig_type_values) >= 3, (
            f"Expected at least 3 signature_type assignments in client.py,"
            f" found {len(sig_type_values)}"
        )
        for lineno, value in sig_type_values:
            assert (
                value == POLY_1271
            ), f"client.py:{lineno} uses signature_type={value}, expected {POLY_1271}"
