"""Example acceptance tests, not a universal Develop success policy."""
import unittest
from main import add


class Addition(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(add(2, 3), 5)

    def test_zero(self):
        self.assertEqual(add(0, 0), 0)

    def test_negative(self):
        self.assertEqual(add(-3, 1), -2)
