import unittest
import json
from app import app

class TestResolverAPI(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.client.testing = True

    def test_root(self):
        """Test that the index endpoint returns service status."""
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data["service"], "WFO Plant Name Synonym Resolver API")
        self.assertEqual(data["status"], "online")

    def test_resolve_by_id_path(self):
        """Test resolving name by WFO ID using path parameter."""
        response = self.client.get('/api/resolve/wfo-0001302011')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertEqual(data["record"]["id"], "wfo-0001302011")
        self.assertEqual(data["record"]["scientificname"], "Cyperus violifolia")

    def test_resolve_by_id_query(self):
        """Test resolving name by WFO ID using query parameter."""
        response = self.client.get('/api/resolve?id=wfo-0001302011')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertEqual(data["record"]["id"], "wfo-0001302011")

    def test_resolve_accepted_name_query(self):
        """Test lookup of an accepted name using query parameters."""
        response = self.client.get('/api/resolve?name=Cyperus%20violifolia&author=Rodriguez%20%26%20Alfonso')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertFalse(data["is_synonym"])
        self.assertIsNone(data["accepted_name"])

    def test_resolve_synonym_name_query(self):
        """Test lookup of a synonym name using query parameters."""
        response = self.client.get('/api/resolve?name=Trichophorum%20bracteatum&author=V.Krecz.%20ex%20Czernov')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertTrue(data["is_synonym"])
        self.assertIsNotNone(data["accepted_name"])
        self.assertEqual(data["accepted_name"]["scientificname"], "Trichophorum cespitosum")

    def test_special_characters_preserved(self):
        """Test lookup of Conophytum swanepoelianum subsp. rubrolineatum and verify special characters are preserved."""
        response = self.client.get('/api/resolve?name=Conophytum%20swanepoelianum%20subsp.%20rubrolineatum')
        self.assertEqual(response.status_code, 200)
        
        # Verify raw response bytes do not contain escaped unicode values like \u00e9 or similar
        raw_text = response.data.decode('utf-8')
        self.assertNotIn('\\u', raw_text)
        
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertEqual(data["record"]["authorship"], "(Rawé) S.A.Hammer")

    def test_resolve_not_found(self):
        """Test response when a name does not exist in the database."""
        response = self.client.get('/api/resolve?name=NonExistentPlantName')
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.data)
        self.assertFalse(data["match_found"])

    def test_resolve_fuzzy_echinocereus(self):
        """Test fuzzy matching resolves Echinocereus barthelowanus to Echinocereus barthelowianus."""
        response = self.client.get('/api/resolve/fuzzy?name=Echinocereus%20barthelowanus')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertTrue(len(data["matches"]) > 0)
        
        # Check that Echinocereus barthelowianus is in the matches
        match_names = [m["record"]["scientificname"] for m in data["matches"]]
        self.assertIn("Echinocereus barthelowianus", match_names)
        
        # Check that matches are sorted by similarity_score descending
        scores = [m["similarity_score"] for m in data["matches"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_resolve_fuzzy_cleistocactus(self):
        """Test fuzzy matching resolves Cleistocactus varispinus to Cleistocactus variispinus."""
        response = self.client.get('/api/resolve/fuzzy?name=Cleistocactus%20varispinus')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        
        match_names = [m["record"]["scientificname"] for m in data["matches"]]
        self.assertIn("Cleistocactus variispinus", match_names)

    def test_resolve_fuzzy_limit_parameter(self):
        """Test that the limit parameter restricts the number of results returned."""
        response = self.client.get('/api/resolve/fuzzy?name=Echinocereus%20barthelowanus&limit=2')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["match_found"])
        self.assertTrue(len(data["matches"]) <= 2)

if __name__ == '__main__':
    unittest.main()
