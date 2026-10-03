import tempfile
import unittest

from app import RequestError, Service, Store, parse_delta, parse_identifier


class StoreContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(f"{self.directory.name}/benchmark.sqlite", 100)
        self.service = Service(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_seed_and_atomic_stock_update(self) -> None:
        response = self.service.detail(1)
        self.assertIn(b'"name":"Product00001"', response.body)
        self.service.update_stock(1, 1)
        self.service.update_stock(1, 1)
        self.assertIn(b'"revision":2', self.service.detail(1).body)
        self.assertEqual(self.store.integrity()[0], 100)

    def test_validation(self) -> None:
        with self.assertRaises(RequestError):
            parse_identifier("0")
        self.assertEqual(parse_delta("application/json; charset=utf-8", b'{"delta":-100}'), -100)
        with self.assertRaises(RequestError):
            parse_delta("application/json", b'{"delta":true}')


if __name__ == "__main__":
    unittest.main()
