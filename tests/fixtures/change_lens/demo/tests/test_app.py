__test__ = False  # Input fixture for static analysis, not a Harness test module.

def test_answer():
    from pkg.app import run
    assert run() == 42
